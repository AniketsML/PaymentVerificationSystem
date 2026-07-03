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

import json
import re
import time
from typing import Optional, Protocol

import requests
from PIL import Image

from config import settings
from pipeline import image_source

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
        self.api_url = (api_url or settings.VISION_API_URL).rstrip("/")
        self.api_key = api_key or settings.VISION_API_KEY
        self.model = model or settings.VISION_MODEL
        self.stream = settings.VISION_STREAM if stream is None else stream

    def extract(self, image: Optional[Image.Image], row: dict) -> dict:
        if image is None:
            return {"is_payment_document": False, "describes": "no image supplied",
                    "document_type": "other", "full_text": "",
                    "_meta": {"error": "no image"}, "_raw": ""}

        b64 = image_source.to_jpeg_b64(image)
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

        t0 = time.time()
        try:
            content = (self._call_stream(url, headers, payload) if self.stream
                       else self._call_once(url, headers, payload))
            err = ""
        except Exception as e:  # noqa: BLE001 - surface any transport error into the log
            content, err = "", f"{type(e).__name__}: {e}"
        elapsed_ms = round((time.time() - t0) * 1000, 1)

        meta = {"model": self.model, "url": url, "stream": self.stream,
                "elapsed_ms": elapsed_ms, "error": err}
        if err:
            out = {"is_payment_document": False,
                   "describes": f"vision model call failed ({err})",
                   "document_type": "other", "full_text": ""}
        else:
            out = _coerce(content)
        out["_meta"] = meta
        out["_raw"] = content[:8000]
        return out

    def _call_once(self, url, headers, payload) -> str:
        r = requests.post(url, headers=headers, json=payload,
                          timeout=settings.OCR_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _call_stream(self, url, headers, payload) -> str:
        chunks: list[str] = []
        with requests.post(url, headers=headers, json=payload, stream=True,
                           timeout=settings.OCR_TIMEOUT_SECONDS) as r:
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
                    delta = obj["choices"][0].get("delta", {})
                    piece = delta.get("content") or ""
                    if piece:
                        chunks.append(piece)
                except Exception:
                    continue
        return "".join(chunks)


# ── testing / offline backend ─────────────────────────────────────────────────
class PrecomputedOCR:
    """Uses ex_* columns already on the row (from a prior extraction run)."""
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
