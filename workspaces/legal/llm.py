"""
Which model reads a page — and the clients that call them.

Two providers: Medha (an OpenAI-compatible endpoint) and Google Gemini. Both are configured from
the console (Model settings), not from .env, and extraction uses them in one of two ways:

  single    one model reads everything
  hybrid    one model for English text, another for text in other languages

Hybrid needs to know a page's language before choosing a model. When the page has a text layer,
that is known up front and the right model reads it directly. A scanned page has no text to look
at until something reads it, so the Model settings name what happens then:

  english_first   the English model reads it; if what came back shows the source was not in
                  English (the model reported an original-script value, or returned non-Latin
                  text), the other-language model re-reads it and its reading is used
  multilingual    scans always go straight to the other-language model

With "retry on the other model" on, a failed call (network, HTTP error, unparseable reply) is
retried once on the other provider if that provider is configured.

Every call made — including a reading that was superseded or failed — is returned in `_calls`
with its provider, model and tokens, so cost per model stays measurable.

Gemini is called over its REST API with httpx rather than a Google SDK: requirements pinned one SDK
while the code imported another, neither was installed, and every Gemini call used to fail with
ModuleNotFoundError. REST has no such dependency and exposes token usage directly.
"""
from __future__ import annotations

import base64
import sys
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

PROVIDERS = ("medha", "gemini")
LABEL = {"medha": "Medha", "gemini": "Gemini"}
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
VISION_TIMEOUT = 180.0
TEXT_TIMEOUT = 60.0


class ModelError(RuntimeError):
    """A call that did not produce a usable reply, with a message a person can act on."""


# ── configuration ────────────────────────────────────────────────────────────
def settings() -> Dict[str, Any]:
    from config import runtime
    return runtime.extraction_config()


def is_configured(provider: str, s: Dict[str, Any]) -> Tuple[bool, str]:
    if provider == "medha":
        m = s.get("medha") or {}
        return (bool(m.get("url")), "" if m.get("url") else "Medha has no endpoint URL")
    if provider == "gemini":
        g = s.get("gemini") or {}
        if not g.get("key"):
            return False, "Gemini has no API key — add one in Model settings"
        return (bool(g.get("model")), "" if g.get("model") else "Gemini has no model selected")
    return False, f"unknown model provider {provider!r}"


def model_name(provider: str, s: Dict[str, Any]) -> str:
    return str(((s.get(provider) or {}).get("model")) or LABEL.get(provider, provider))


def _other(provider: str) -> str:
    return "gemini" if provider == "medha" else "medha"


# ── the calls ────────────────────────────────────────────────────────────────
def _medha_url(url: str) -> str:
    url = (url or "").rstrip("/")
    return url if url.endswith("/chat/completions") else url + "/chat/completions"


def _call_medha(s: Dict[str, Any], prompt: str, images: List[bytes], max_tokens: int, timeout: float
                ) -> Tuple[str, Dict[str, Any]]:
    import httpx
    m = s["medha"]
    content: Any = prompt
    if images:
        content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}}
                   for b in images] + [{"type": "text", "text": prompt}]
    headers = {"Content-Type": "application/json"}
    if m.get("key"):
        headers["Authorization"] = f"Bearer {m['key']}"
    t0 = time.perf_counter()
    resp = httpx.post(_medha_url(m["url"]), headers=headers, timeout=timeout,
                      json={"model": m.get("model"), "messages": [{"role": "user", "content": content}],
                            "max_tokens": max_tokens, "temperature": 0.1})
    ms = round((time.perf_counter() - t0) * 1000, 1)
    if resp.status_code != 200:
        raise ModelError(f"Medha HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return raw, {"provider": "medha", "model": m.get("model") or "Medha", "route": "medha_vlm", "ms": ms,
                 "prompt_tokens": pt, "completion_tokens": ct,
                 "total_tokens": int(usage.get("total_tokens") or pt + ct)}


def _gemini_model_id(model: str) -> str:
    return str(model or "").strip().removeprefix("models/")


def _call_gemini(s: Dict[str, Any], prompt: str, images: List[bytes], max_tokens: int, timeout: float,
                 json_reply: bool = True) -> Tuple[str, Dict[str, Any]]:
    import httpx
    g = s["gemini"]
    model = _gemini_model_id(g["model"])
    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(b).decode()}} for b in images]
    parts.append({"text": prompt})
    config: Dict[str, Any] = {"temperature": 0.1, "maxOutputTokens": max_tokens}
    if json_reply:
        config["responseMimeType"] = "application/json"
    t0 = time.perf_counter()
    # the key travels in a header, never in the URL, so it can't end up in a proxy or access log
    resp = httpx.post(f"{GEMINI_BASE}/models/{model}:generateContent", timeout=timeout,
                      headers={"x-goog-api-key": g["key"], "Content-Type": "application/json"},
                      json={"contents": [{"role": "user", "parts": parts}], "generationConfig": config})
    ms = round((time.perf_counter() - t0) * 1000, 1)
    if resp.status_code != 200:
        try:
            msg = (resp.json().get("error") or {}).get("message") or resp.text
        except Exception:  # noqa: BLE001
            msg = resp.text
        raise ModelError(f"Gemini HTTP {resp.status_code}: {str(msg)[:200]}")
    data = resp.json()
    cands = data.get("candidates") or []
    if not cands:
        block = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
        raise ModelError(f"Gemini returned no answer ({block})")
    cand = cands[0]
    # thinking models return their reasoning as parts marked "thought" — never part of the answer
    raw = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [] if not p.get("thought"))
    usage = data.get("usageMetadata") or {}
    pt = int(usage.get("promptTokenCount") or 0)
    # thinking tokens are billed as output, so they are counted as output here too
    ct = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
    meta = {"provider": "gemini", "model": model, "route": "gemini_vlm", "ms": ms,
            "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": int(usage.get("totalTokenCount") or pt + ct)}
    if cand.get("finishReason") == "MAX_TOKENS":
        meta["truncated"] = True
    if not raw.strip():
        raise ModelError(f"Gemini returned an empty answer (finish: {cand.get('finishReason')})")
    return raw, meta


def _call(provider: str, s: Dict[str, Any], prompt: str, images: List[bytes], max_tokens: int, timeout: float
          ) -> Tuple[str, Dict[str, Any]]:
    ok, why = is_configured(provider, s)
    if not ok:
        raise ModelError(why)
    if provider == "medha":
        return _call_medha(s, prompt, images, max_tokens, timeout)
    return _call_gemini(s, prompt, images, max_tokens, timeout)


def call_text(prompt: str, s: Optional[Dict[str, Any]] = None, max_tokens: int = 2048) -> Optional[str]:
    """A text-only call (the prompt planner). Uses the main model — prompts are written in English —
    retrying on the other model if that is allowed and configured."""
    s = s or settings()
    order = [s["main"]] + ([_other(s["main"])] if s.get("fallback") else [])
    for provider in order:
        try:
            raw, _ = _call(provider, s, prompt, [], max_tokens, TEXT_TIMEOUT)
            if raw:
                return raw
        except Exception as e:  # noqa: BLE001 — the planner has a deterministic floor under it
            sys.stderr.write(f"[llm] {provider} text call failed: {e}\n")
    return None


# ── language ─────────────────────────────────────────────────────────────────
def text_script(text: str, min_letters: int = 40) -> str:
    """'english' | 'multilingual' | 'unknown' from a page's own text layer. Latin letters (with
    accents) count as English here; anything else — Devanagari, Tamil, Urdu … — as other."""
    latin = other = 0
    for ch in text or "":
        # letters and combining marks — Indic vowel signs (ा ि ी …) are marks, not letters
        if unicodedata.category(ch)[0] not in "LM":
            continue
        o = ord(ch)
        if o < 0x0250 or 0x0300 <= o < 0x0370:  # Latin through Extended-B, and Latin accents
            latin += 1
        else:
            other += 1
    if latin + other < min_letters:
        return "unknown"                        # a scan, or too little text to judge
    return "multilingual" if other / (latin + other) >= 0.15 else "english"


def reply_not_english(parsed: Dict[str, Any]) -> bool:
    """Did the reading show the source was not in English? The prompt asks the model to report the
    original-script text of any value it transliterated or translated; a value still in a
    non-Latin script says the same thing."""
    evidence = parsed.get("_field_evidence") or {}
    if isinstance(evidence, dict) and any(isinstance(v, dict) and v.get("original") for v in evidence.values()):
        return True
    for k, v in parsed.items():
        if not str(k).startswith("_") and isinstance(v, str):
            if any(ch.isalpha() and ord(ch) >= 0x0250 for ch in v):
                return True
    return False


# ── routing ──────────────────────────────────────────────────────────────────
def plan(s: Dict[str, Any], language: str) -> Tuple[str, Optional[str], str]:
    """(first provider, provider to re-read with if the reading turns out not to be English, why)."""
    if s.get("routing") != "hybrid":
        return s["main"], None, f"{LABEL[s['main']]} reads everything"
    eng, oth = s["main"], s["multilingual"]
    if language == "multilingual":
        return oth, None, f"text layer is not in English — {LABEL[oth]}"
    if language == "english":
        return eng, None, f"text layer is in English — {LABEL[eng]}"
    if s.get("scanned") == "multilingual":
        return oth, None, f"scanned page — {LABEL[oth]} reads scans"
    return eng, oth, f"scanned page — {LABEL[eng]} first"


def extract(prompt: str, images: List[bytes], language: str = "unknown", s: Optional[Dict[str, Any]] = None,
            max_tokens: int = 8192) -> Dict[str, Any]:
    """Read one batch of page images. Returns the parsed reply with `_meta` (the call whose reading
    is used), `_calls` (every call, for cost), `_routing` (why this model), `_ocr_route`,
    `_raw_response`, and `_error` / `_medha_error` when nothing usable came back."""
    from workspaces.legal.ocr import _parse_json
    s = s or settings()
    first, escalate, why = plan(s, language)
    calls: List[Dict[str, Any]] = []

    def attempt(provider: str) -> Optional[Dict[str, Any]]:
        try:
            raw, meta = _call(provider, s, prompt, images, max_tokens, VISION_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — recorded, then maybe retried elsewhere
            calls.append({"provider": provider, "model": model_name(provider, s), "status": "FAIL",
                          "error": f"{type(e).__name__}: {e}"[:400], "prompt_tokens": 0, "completion_tokens": 0})
            return None
        parsed = _parse_json(raw)
        if not isinstance(parsed, dict):           # valid JSON, but a list or a bare value
            parsed = {"_parse_error": True}
        parsed["_raw_response"] = raw
        entry = dict(meta, status="OK")
        if parsed.get("_parse_error"):
            entry.update(status="FAIL", error=("the reply was cut off at the output limit" if meta.get("truncated")
                                               else f"{LABEL[provider]} returned a reply that is not valid JSON"))
            calls.append(entry)
            return None
        calls.append(entry)
        parsed["_meta"] = meta
        return parsed

    parsed = attempt(first)
    used = first
    if parsed is None and s.get("fallback") and is_configured(_other(first), s)[0]:
        why += f"; {LABEL[first]} failed, retried on {LABEL[_other(first)]}"
        parsed, used = attempt(_other(first)), _other(first)
    elif parsed is not None and escalate and escalate != first and reply_not_english(parsed):
        reread = attempt(escalate)
        if reread is not None:
            calls[-2]["status"] = "SUPERSEDED"
            why += f"; the text was not in English, re-read by {LABEL[escalate]}"
            parsed, used = reread, escalate
        else:
            why += f"; the text was not in English but {LABEL[escalate]} failed — kept {LABEL[first]}'s reading"

    if parsed is None:
        errors = "; ".join(f"{LABEL.get(c['provider'], c['provider'])}: {c.get('error', '')}" for c in calls)
        return {"_error": errors or "no model call succeeded", "_medha_error": errors, "_raw_response": "",
                "_ocr_route": "failed", "_calls": calls, "_routing": why}
    parsed["_ocr_route"] = parsed["_meta"].get("route", used)
    parsed["_calls"] = calls
    parsed["_routing"] = why
    return parsed


# ── connection tests ─────────────────────────────────────────────────────────
def test_gemini(key: str, model: str) -> Dict[str, Any]:
    """Is the key accepted, and does it see this model? Lists models — costs no tokens."""
    import httpx
    t0 = time.perf_counter()
    try:
        resp = httpx.get(f"{GEMINI_BASE}/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": key}, timeout=15.0)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": round((time.perf_counter() - t0) * 1000), "error": f"{type(e).__name__}: {e}"[:200]}
    ms = round((time.perf_counter() - t0) * 1000)
    if resp.status_code != 200:
        try:
            msg = (resp.json().get("error") or {}).get("message") or resp.text
        except Exception:  # noqa: BLE001
            msg = resp.text
        return {"ok": False, "status": resp.status_code, "ms": ms, "error": str(msg)[:200]}
    names = [str(m.get("name", "")).removeprefix("models/") for m in resp.json().get("models") or []
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    want = _gemini_model_id(model)
    return {"ok": True, "status": 200, "ms": ms, "models": sorted(n for n in names if n.startswith("gemini"))[:60],
            "model_present": (want in names) if want else None}
