"""
Image loading - the ONE place that turns a "source" into pixels.

A source can be:
  - an http(s) URL          (e.g. the S3 links in the CSV) -> downloaded
  - a local file path       -> read from disk
  - a data: URI             -> decoded inline

We deliberately do NOT enhance/preprocess here. We just fetch the bytes and
decode them into a PIL image. Anything that cannot be fetched or decoded is a
technically-invalid image and is reported as such (with a precise reason).
"""
from __future__ import annotations

import base64
import io
import os
from typing import Optional

import requests
from PIL import Image

from config import settings

_UA = {"User-Agent": "Mozilla/5.0 (PaymentVerification/1.0)"}


def is_url(source: str) -> bool:
    s = (source or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _diagnose_non_image(raw: bytes, content_type: str) -> str:
    """A URL that fetched OK (200) but doesn't decode as an image is almost always a
    private/expired link whose body is an access-denied XML/HTML page — not a corrupt
    image. Name that precisely. Returns "" when it looks like a genuinely bad image, so
    the original reason is used unchanged."""
    ct = (content_type or "").lower()
    head = raw[:800].decode("utf-8", "ignore").lower() if raw else ""
    private = ("accessdenied", "access denied", "signaturedoesnotmatch", "invalidaccesskeyid",
               "expiredtoken", "request has expired", "requesttimetooskewed",
               "authenticationrequired", "not authorized", "forbidden")
    if any(m in head for m in private):
        return "image URL is private / access denied — the link needs a valid signature or permission"
    if "html" in ct or "<html" in head or head.lstrip().startswith("<!doctype html"):
        return "image URL returned a web page, not an image (likely a private/expired/login link)"
    if "xml" in ct or "<?xml" in head or "<error" in head:
        return "image URL returned an error response, not an image (likely a private/expired link)"
    return ""


def load(source: str, timeout: Optional[int] = None) -> tuple[Optional[Image.Image], bytes, str]:
    """
    Returns (pil_image, raw_bytes, error).
    On success error is "". On failure pil_image is None and error explains why.
    """
    timeout = timeout or settings.IMAGE_FETCH_TIMEOUT
    if not source:
        return None, b"", "no image source provided"

    src = source.strip()
    content_type = ""
    try:
        if src.startswith("data:"):
            raw = base64.b64decode(src.split(",", 1)[1])
            origin = "data-uri"
        elif is_url(src):
            r = requests.get(src, headers=_UA, timeout=timeout)
            r.raise_for_status()
            raw = r.content
            content_type = r.headers.get("Content-Type", "")
            origin = "url"
        else:
            if not os.path.exists(src):
                return None, b"", f"image file not found ({src})"
            with open(src, "rb") as f:
                raw = f.read()
            origin = "file"
    except requests.exceptions.RequestException as e:
        return None, b"", f"could not download image ({type(e).__name__})"
    except Exception as e:  # noqa: BLE001 - report any fetch/decoding failure verbatim
        return None, b"", f"could not read image source ({type(e).__name__}: {e})"

    if not raw:
        return None, b"", f"empty image payload from {origin}"

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()                       # force decode now so we fail here, not later
    except Exception as e:  # noqa: BLE001
        # for a URL, a non-image body is usually a private/expired link — say so precisely;
        # otherwise keep the original corrupt-image reason unchanged.
        reason = _diagnose_non_image(raw, content_type) if origin == "url" else ""
        return None, raw, reason or f"unreadable/corrupt image ({type(e).__name__})"

    return img, raw, ""


def to_jpeg_b64(img: Image.Image, quality: int = 92) -> str:
    """Encode a PIL image as base64 JPEG for the vision API (transport only, not enhancement)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()
