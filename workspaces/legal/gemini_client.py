"""
Resilient Google Gemini multimodal API client for SARFAESI fallback OCR.

Used as circuit-breaker failover when primary Medha VLM is unavailable.
Supports image+text input, structured JSON output.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import time
import threading
from typing import Any, Dict, Optional, Tuple

from config import settings


_genai = None
_genai_lock = threading.Lock()
_genai_available: Optional[bool] = None


def _ensure_genai():
    """Lazy-load google.genai SDK."""
    global _genai, _genai_available
    if _genai_available is not None:
        return _genai_available
    with _genai_lock:
        if _genai_available is not None:
            return _genai_available
        try:
            from google import genai
            _genai = genai
            _genai_available = True
        except ImportError:
            _genai_available = False
    return _genai_available


def _parse_json(raw: str) -> Dict[str, Any]:
    """Parse JSON from model response, handling markdown fences."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"_parse_error": True, "_raw_text": raw[:500]}


def call_gemini(
    prompt: str,
    image_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg",
    model: str = "",
    timeout: int = 0,
    max_retries: int = 2,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Call Gemini multimodal API with image + text prompt.

    Returns:
        (parsed_json, meta_dict) where meta contains ms, model, route, error.
    """
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        return {}, {"error": "GEMINI_API_KEY not configured", "route": "gemini_fallback"}

    model = model or settings.GEMINI_MODEL
    timeout = timeout or settings.GEMINI_TIMEOUT
    t0 = time.perf_counter()
    meta = {"model": model, "route": "gemini_fallback", "cached": False}

    if not _ensure_genai():
        return _call_gemini_rest(prompt, image_bytes, mime_type, api_key,
                                 model, timeout, max_retries)

    # Use google-genai SDK
    client = _genai.Client(api_key=api_key)
    contents = []
    if image_bytes:
        contents.append(_genai.types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
    contents.append(prompt)

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=_genai.types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=4096,
                ),
            )
            raw = resp.text or ""
            ms = round((time.perf_counter() - t0) * 1000, 1)
            meta["ms"] = ms
            parsed = _parse_json(raw)
            parsed["_meta"] = meta
            return parsed, meta
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 4))

    ms = round((time.perf_counter() - t0) * 1000, 1)
    meta["ms"] = ms
    meta["error"] = str(last_err)
    return {"_error": str(last_err)}, meta


def _call_gemini_rest(
    prompt: str,
    image_bytes: Optional[bytes],
    mime_type: str,
    api_key: str,
    model: str,
    timeout: int,
    max_retries: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """REST fallback when google-genai SDK is not installed."""
    import httpx
    t0 = time.perf_counter()
    meta = {"model": model, "route": "gemini_fallback", "cached": False}

    parts = []
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode()
        parts.append({"inline_data": {"mime_type": mime_type, "data": b64}})
    parts.append({"text": prompt})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
    }

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(url, json=body, timeout=float(timeout))
            resp.raise_for_status()
            data = resp.json()
            raw = (data.get("candidates", [{}])[0]
                   .get("content", {}).get("parts", [{}])[0].get("text", ""))
            ms = round((time.perf_counter() - t0) * 1000, 1)
            meta["ms"] = ms
            parsed = _parse_json(raw)
            parsed["_meta"] = meta
            return parsed, meta
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 4))

    ms = round((time.perf_counter() - t0) * 1000, 1)
    meta["ms"] = ms
    meta["error"] = str(last_err)
    return {"_error": str(last_err)}, meta
