"""
Image prefetch — the highest-leverage piece. Download the document at INGEST time, while
the signed URL is still fresh, and store it content-addressed in the blob store. Workers
then load from the blob, so link expiry can no longer cause an unprocessed lead.

Only http(s) URLs are prefetched. data: URIs and local paths are already stable and pass
through unchanged. A body that fails to decode as an image (an access-denied HTML/XML page
from an already-expired link) is NOT stored — the lead enqueues with its original URL and
the pipeline classifies it honestly. A short inline retry ladder covers transient blips
without blocking the poll loop.
"""
from __future__ import annotations

import io
import time

import requests
from PIL import Image

from config import settings
from ingest import blobstore

_UA = {"User-Agent": "Mozilla/5.0 (PaymentVerification-Ingester/1.0)"}


def _is_http(src: str) -> bool:
    s = (src or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _download(url: str) -> tuple[bytes, str]:
    """Stream with a hard size cap so a hostile/huge body can't blow memory."""
    cap = settings.IMAGE_PREFETCH_MAX_MB * 1024 * 1024
    with requests.get(url, headers=_UA, timeout=settings.IMAGE_FETCH_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf.extend(chunk)
            if len(buf) > cap:
                raise ValueError(f"image exceeds {settings.IMAGE_PREFETCH_MAX_MB}MB cap")
        return bytes(buf), ctype


def prefetch(source: str) -> tuple[str, bool, str]:
    """Returns (image_ref, prefetched, reason).
      image_ref  — 'blob:<sha256>' when stored, else the original source (loaded live later).
      prefetched — True only when bytes were validated and stored.
      reason     — why prefetch was skipped/failed (for the ingest_runs log), else ''.
    """
    if not settings.IMAGE_PREFETCH:
        return source, False, "prefetch disabled"
    if not source:
        return source, False, "no image source"
    if not _is_http(source):
        return source, False, "non-http source (already stable)"

    attempts = max(1, settings.IMAGE_PREFETCH_ATTEMPTS)
    last = ""
    for i in range(attempts):
        try:
            data, ctype = _download(source)
            if not data:
                last = "empty body"
                continue
            # validate it really decodes as an image before storing — an access-denied
            # HTML/XML page must never masquerade as a cached document.
            try:
                Image.open(io.BytesIO(data)).verify()
            except Exception:
                return source, False, "url did not return a decodable image (likely private/expired)"
            sha = blobstore.store().put(data, content_type=ctype, source_url=source)
            return blobstore.BLOB_PREFIX + sha, True, ""
        except requests.exceptions.RequestException as e:
            last = f"download error ({type(e).__name__})"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if i + 1 < attempts:
            time.sleep(0.5 * (i + 1))          # brief backoff, never blocks the loop long
    return source, False, last or "prefetch failed"
