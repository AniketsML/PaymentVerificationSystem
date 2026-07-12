"""
Vision client - reads a payment image into structured JSON via the Medha VLM.

Two interchangeable backends behind one interface (`.extract`):
  - MedhaVisionOCR : live Medha vision model (OpenAI-compatible /chat/completions,
                     streaming). This is production.
  - PrecomputedOCR : returns extraction already present on the CSV row (ex_* cols)
                     - lets you run/test the whole pipeline deterministically with
                     no API calls.

Both return the SAME dict schema so nothing downstream changes. For maximum
visibility, the live backend also attaches `_meta` (timing, model, url) and
`_raw` (the verbatim model response) so the orchestrator can log the full,
un-parsed model output for every lead.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from typing import Optional, Protocol

import requests
from PIL import Image

from config import settings
from pipeline import image_source

# Bump when _SCHEMA_PROMPT changes so cached extractions from an old prompt are ignored.
PROMPT_VERSION = "1"

# fields worth persisting in the extraction cache (everything except per-call meta)
_CACHE_FIELDS = ("is_payment_document", "describes", "document_type", "loan_account_number",
                 "amount", "date", "time", "reference_id", "receiver_name", "payer_name",
                 "full_text")


# ── extraction cache (image content -> parsed extraction) ─────────────────────
def _cache_get(key: str):
    if not settings.OCR_CACHE:
        return None
    try:
        from db import pg
        with pg.pool().connection() as c:
            r = c.execute("UPDATE ocr_cache SET hits = hits + 1 WHERE cache_key=%s "
                          "RETURNING extraction", (key,)).fetchone()
        return r["extraction"] if r else None
    except Exception:
        return None            # cache must never break extraction


def _cache_put(key: str, extraction: dict, model: str):
    if not settings.OCR_CACHE:
        return
    try:
        from db import pg
        from psycopg.types.json import Jsonb
        slim = {k: extraction.get(k) for k in _CACHE_FIELDS}
        with pg.pool().connection() as c:
            c.execute("INSERT INTO ocr_cache(cache_key, extraction, model) VALUES(%s,%s,%s) "
                      "ON CONFLICT (cache_key) DO NOTHING", (key, Jsonb(slim), model))
    except Exception:
        pass


# ── circuit breaker (shared across worker threads) ────────────────────────────
class _Breaker:
    """When Medha keeps failing, open the circuit and make callers wait a growing
    cooldown — backpressure instead of a retry-storm. Thread-safe (workers share one)."""
    def __init__(self):
        self._lock = threading.Lock()
        self._consec = 0
        self._open_until = 0.0

    def wait_if_open(self):
        with self._lock:
            remaining = self._open_until - time.time()
        if remaining > 0:
            time.sleep(min(remaining, settings.OCR_BREAKER_MAX_WAIT) + random.uniform(0, 0.5))

    def record(self, ok: bool):
        with self._lock:
            if ok:
                self._consec = 0
                self._open_until = 0.0
                return
            self._consec += 1
            if self._consec >= settings.OCR_BREAKER_THRESHOLD:
                # exponential cooldown past the threshold, capped
                over = self._consec - settings.OCR_BREAKER_THRESHOLD
                wait = min(settings.OCR_BREAKER_COOLDOWN * (2 ** over), settings.OCR_BREAKER_MAX_WAIT)
                self._open_until = time.time() + wait


_BREAKER = _Breaker()

_SCHEMA_PROMPT = """You are reading an Indian loan-repayment proof image.
Return ONLY a JSON object, no prose, with exactly these keys:
{
 "is_payment_document": true/false,   // true if this is a payment receipt/confirmation OR a loan No-Dues / NOC / closure certificate (all count as valid payment proof)
 "describes": "<if NOT a payment document, 3-6 words on what the image shows, else empty>",
 "document_type": "<upi_screenshot_phonepe|upi_screenshot_gpay|upi_screenshot_paytm|bank_neft_receipt|bank_imps_receipt|cash_receipt|cheque_image|no_dues_certificate|noc|loan_closure_letter|loan_statement|other>",
 "loan_account_number": "<LAN / loan a/c number exactly as printed, or null>",
 "amount": "<numeric amount without symbols/commas, or null>",
 "date": "<payment date as printed, or null>",
 "time": "<payment time as printed, or null>",
 "reference_id": "<UTR/RRN/Txn id/Ref no exactly as printed, or null>",
 "receiver_name": "<who received the money - payee/beneficiary exactly as printed, or null>",
 "payer_name": "<who paid, or null>",
 "full_text": "<verbatim transcription of ALL text on the image>"
}
A loan "No Dues Certificate" / "No Objection Certificate (NOC)" / loan-closure letter IS valid payment proof: set is_payment_document=true and pick the matching document_type. It may not show an amount - that is fine; still extract loan_account_number, date and the issuer/receiver name.
Never guess a value; use null if it is not clearly visible."""


class OCRClient(Protocol):
    def extract(self, image: Optional[Image.Image], row: dict) -> dict: ...


# ── production backend ────────────────────────────────────────────────────────
class MedhaVisionOCR:
    def __init__(self, api_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, stream: bool | None = None):
        # read the LIVE runtime config (portal-editable) so a moved endpoint/key/model
        # takes effect on the next lead with no restart; explicit args still override.
        from config import runtime
        cfg = runtime.model_config()
        self.api_url = (api_url or cfg["url"]).rstrip("/")
        self.api_key = api_key or cfg["key"]
        self.model = model or cfg["model"]
        self.stream = cfg["stream"] if stream is None else stream

    def extract(self, image: Optional[Image.Image], row: dict) -> dict:
        if image is None:
            return {"is_payment_document": False, "describes": "no image supplied",
                    "document_type": "other", "full_text": "",
                    "_meta": {"error": "no image"}, "_raw": ""}

        b64 = image_source.to_jpeg_b64(image)

        # ── cache hit? the same image + model + prompt returns the same extraction ──
        cache_key = (hashlib.sha256(b64.encode("ascii")).hexdigest()
                     + f":{self.model}:v{PROMPT_VERSION}")
        cached = _cache_get(cache_key)
        if cached is not None:
            out = dict(cached)
            out["_meta"] = {"model": self.model, "elapsed_ms": 0.0, "error": "",
                            "cache": "hit"}
            out["_raw"] = ""
            return out

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a precise OCR and document-understanding assistant. You only output JSON."},
                {"role": "user", "content": [
                    {"type": "text", "text": _SCHEMA_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]},
            ],
            "max_tokens": 1024,
            "temperature": 0.0,          # deterministic
            "stream": self.stream,
        }
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        url = f"{self.api_url}/chat/completions"

        _BREAKER.wait_if_open()          # backpressure while Medha is failing
        t0 = time.time()
        call_meta: dict = {}
        try:
            content = (self._call_stream(url, headers, payload, call_meta) if self.stream
                       else self._call_once(url, headers, payload, call_meta))
            err = ""
        except Exception as e:  # noqa: BLE001 - surface any transport error into the log
            content, err = "", f"{type(e).__name__}: {e}"
        elapsed_ms = round((time.time() - t0) * 1000, 1)
        _BREAKER.record(ok=not err)

        meta = {"model": self.model, "url": url, "stream": self.stream,
                "elapsed_ms": elapsed_ms, "error": err, "cache": "miss",
                "http_status": call_meta.get("http_status"),
                "finish_reason": call_meta.get("finish_reason"),   # 'length' = truncated output
                "prompt_tokens": call_meta.get("prompt_tokens"),
                "completion_tokens": call_meta.get("completion_tokens"),
                "response_chars": len(content or "")}
        if err:
            out = {"is_payment_document": False,
                   "describes": f"vision model call failed ({err})",
                   "document_type": "other", "full_text": ""}
        else:
            out = _coerce(content)
            _cache_put(cache_key, out, self.model)     # cache only successful reads
        out["_meta"] = meta
        out["_raw"] = content[:8000]
        return out

    def _call_once(self, url, headers, payload, meta=None) -> str:
        r = requests.post(url, headers=headers, json=payload,
                          timeout=settings.OCR_TIMEOUT_SECONDS)
        if meta is not None:
            meta["http_status"] = r.status_code       # captured before raise so 4xx/5xx are logged
        r.raise_for_status()
        j = r.json()
        if meta is not None:
            ch = (j.get("choices") or [{}])[0]
            meta["finish_reason"] = ch.get("finish_reason")
            u = j.get("usage") or {}
            meta["prompt_tokens"] = u.get("prompt_tokens")
            meta["completion_tokens"] = u.get("completion_tokens")
        return j["choices"][0]["message"]["content"]

    def _call_stream(self, url, headers, payload, meta=None) -> str:
        chunks: list[str] = []
        finish, usage = None, None
        with requests.post(url, headers=headers, json=payload, stream=True,
                           timeout=settings.OCR_TIMEOUT_SECONDS) as r:
            if meta is not None:
                meta["http_status"] = r.status_code
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[len("data:"):].strip()
                if line == "[DONE]":
                    break
                try:
                    obj = json.loads(line)
                    ch = (obj.get("choices") or [{}])[0]
                    piece = ch.get("delta", {}).get("content") or ""
                    if piece:
                        chunks.append(piece)
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    if obj.get("usage"):
                        usage = obj["usage"]
                except Exception:
                    continue
        if meta is not None:
            meta["finish_reason"] = finish
            if usage:
                meta["prompt_tokens"] = usage.get("prompt_tokens")
                meta["completion_tokens"] = usage.get("completion_tokens")
        return "".join(chunks)


# ── testing / offline backend ─────────────────────────────────────────────────
class PrecomputedOCR:
    """Offline backend: uses `ex_*` columns already on the row instead of calling the
    vision model. This is NOT the dashboard "Test run" sandbox (that runs the REAL model
    on flagged is_test data). It exists only for the CLI (`run_batch.py --precomputed`)
    and the `/api/verify` integration, to replay a pre-extracted dataset with no model.
    If a row has no `ex_*` columns, every field is empty — so only use it on data that
    carries them."""
    def extract(self, image: Optional[Image.Image], row: dict) -> dict:
        def g(k):
            v = row.get(k, "")
            return None if v in ("", "None", "nan") else str(v)
        is_rec = str(row.get("ex_is_receipt", "")).lower() in ("true", "1")
        return {
            "is_payment_document": is_rec,
            "describes": "" if is_rec else (g("ex_doc_type") or "non-payment image"),
            "document_type": g("ex_doc_type") or "other",
            "loan_account_number": g("ex_loan_id"),
            "amount": g("ex_amount"),
            "date": g("ex_date"),
            "time": None,
            "reference_id": g("ex_ref_id"),
            "receiver_name": g("ex_receiver_name"),
            "payer_name": g("ex_payer_name"),
            "full_text": g("ex_full_text") or "",
            "_meta": {"model": "precomputed", "elapsed_ms": 0.0, "error": ""},
            "_raw": g("ex_raw_json") or "",
        }


def _coerce(content: str) -> dict:
    """Pull the JSON object out of a model response robustly."""
    m = re.search(r"\{.*\}", content or "", re.DOTALL)
    if not m:
        return {"is_payment_document": False, "describes": "unreadable model output",
                "document_type": "other", "full_text": (content or "")[:500]}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"is_payment_document": False, "describes": "unparseable model output",
                "document_type": "other", "full_text": (content or "")[:500]}
    d.setdefault("is_payment_document", False)
    for k in ("loan_account_number", "amount", "date", "time", "reference_id",
              "receiver_name", "payer_name"):
        if d.get(k) in ("null", "", "None"):
            d[k] = None
    d.setdefault("full_text", "")
    return d
