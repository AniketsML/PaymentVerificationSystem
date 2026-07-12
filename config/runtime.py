"""
Runtime-editable configuration — the Medha model endpoint, changeable from the portal
without editing .env or restarting.

Stored in the `runtime_config` table (shared by the web app + worker threads), read
through a short TTL cache so a change propagates to every worker within a few seconds.
Defaults come from the environment / settings, so with an empty table the behaviour is
exactly what .env dictates.
"""
from __future__ import annotations

import threading
import time

from config import settings
from db import pg

# the model-endpoint keys this module manages
_KEYS = ("medha_api_url", "medha_api_key", "medha_model", "medha_stream")

_lock = threading.Lock()
_cache: dict = {}
_cache_ts = 0.0
_TTL = 5.0


def _defaults() -> dict:
    return {
        "medha_api_url": settings.VISION_API_URL,
        "medha_api_key": settings.VISION_API_KEY,
        "medha_model": settings.VISION_MODEL,
        "medha_stream": "1" if settings.VISION_STREAM else "0",
    }


def _load() -> dict:
    d = _defaults()
    try:
        with pg.pool().connection() as c:
            for r in c.execute("SELECT key, value FROM runtime_config WHERE key = ANY(%s)",
                               (list(_KEYS),)).fetchall():
                if r["value"] is not None and r["value"] != "":
                    d[r["key"]] = r["value"]
    except Exception:
        pass          # DB not ready / table absent -> pure env defaults
    return d


def model_config(fresh: bool = False) -> dict:
    """Current Medha endpoint config: {url, key, model, stream(bool)}. TTL-cached."""
    global _cache, _cache_ts
    now = time.time()
    with _lock:
        if fresh or not _cache or (now - _cache_ts) >= _TTL:
            _cache = _load()
            _cache_ts = now
        d = dict(_cache)
    return {
        "url": (d["medha_api_url"] or "").rstrip("/"),
        "key": d["medha_api_key"] or "",
        "model": d["medha_model"] or "",
        "stream": str(d["medha_stream"]).lower() not in ("0", "false", "no", ""),
    }


def set_model_config(url: str = None, key: str = None, model: str = None,
                     stream=None) -> dict:
    """Persist the given fields (None = leave unchanged) and return the new config."""
    updates = {}
    if url is not None:
        updates["medha_api_url"] = str(url).strip()
    if key is not None and str(key) != "":        # empty key = keep the existing one
        updates["medha_api_key"] = str(key)
    if model is not None:
        updates["medha_model"] = str(model).strip()
    if stream is not None:
        updates["medha_stream"] = "1" if (stream in (True, 1, "1", "true", "on")) else "0"
    if updates:
        with pg.pool().connection() as c:
            for k, v in updates.items():
                c.execute("INSERT INTO runtime_config(key, value) VALUES(%s, %s) "
                          "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                          (k, v))
        global _cache_ts
        with _lock:
            _cache_ts = 0.0        # invalidate so the next read (any worker) reloads
    return model_config(fresh=True)


def masked() -> dict:
    """Config safe to send to the browser — the key is redacted to its last 4 chars."""
    c = model_config()
    k = c["key"]
    c["key_masked"] = ("•" * max(0, len(k) - 4) + k[-4:]) if k else ""
    c["has_key"] = bool(k)
    del c["key"]
    return c
