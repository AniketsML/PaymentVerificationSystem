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
# legal extraction's model choice (workspaces/legal/llm.py): Gemini credentials, and which model
# reads what. Kept apart from _KEYS so model_config() — shared with the payment console — is
# exactly what it always was.
_EXTRACTION_KEYS = ("gemini_api_key", "gemini_model", "llm_routing", "llm_main",
                    "llm_multilingual", "llm_scanned", "llm_fallback")
ROUTINGS = ("single", "hybrid")
PROVIDERS = ("medha", "gemini")
SCANNED = ("english_first", "multilingual")

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
        # the .env values stay the fallback, so an empty table behaves exactly as before
        "gemini_api_key": settings.GEMINI_API_KEY,
        "gemini_model": settings.GEMINI_MODEL,
        "llm_routing": "single",
        "llm_main": "medha",
        "llm_multilingual": "gemini",
        "llm_scanned": "english_first",
        "llm_fallback": "1",
    }


def _load() -> dict:
    d = _defaults()
    try:
        with pg.pool().connection() as c:
            for r in c.execute("SELECT key, value FROM runtime_config WHERE key = ANY(%s)",
                               (list(_KEYS + _EXTRACTION_KEYS),)).fetchall():
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


# ── legal extraction: which model reads what ─────────────────────────────────
def _raw(fresh: bool = False) -> dict:
    global _cache, _cache_ts
    now = time.time()
    with _lock:
        if fresh or not _cache or (now - _cache_ts) >= _TTL:
            _cache = _load()
            _cache_ts = now
        return dict(_cache)


def _pick(value: str, allowed: tuple, default: str) -> str:
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def extraction_config(fresh: bool = False) -> dict:
    """Both providers' credentials and the routing, as workspaces/legal/llm.py uses them:
    {routing, main, multilingual, scanned, fallback, medha: {url, key, model}, gemini: {key, model}}."""
    d = _raw(fresh)
    medha = model_config(fresh=False)
    return {
        "routing": _pick(d.get("llm_routing"), ROUTINGS, "single"),
        "main": _pick(d.get("llm_main"), PROVIDERS, "medha"),
        "multilingual": _pick(d.get("llm_multilingual"), PROVIDERS, "gemini"),
        "scanned": _pick(d.get("llm_scanned"), SCANNED, "english_first"),
        "fallback": str(d.get("llm_fallback", "1")).lower() not in ("0", "false", "no", "off", ""),
        "medha": {"url": medha["url"], "key": medha["key"], "model": medha["model"]},
        "gemini": {"key": d.get("gemini_api_key") or "",
                   "model": str(d.get("gemini_model") or "").strip().removeprefix("models/")},
    }


class ConfigError(ValueError):
    """A combination that must not be saved, with the reason in plain words."""


def set_extraction_config(routing=None, main=None, multilingual=None, scanned=None, fallback=None,
                          gemini_key=None, gemini_model=None, medha_url=None, medha_key=None,
                          medha_model=None, clear_gemini_key: bool = False) -> dict:
    """Validate the whole resulting setup, then persist it. Nothing is saved if the result would
    route pages to a model that cannot be called — Gemini chosen with no key, or a "hybrid" that
    uses the same model for both languages."""
    cur = extraction_config(fresh=True)
    if medha_url is not None and not str(medha_url).strip():
        medha_url = None       # the payment console runs on this endpoint — never blanked from here
    new_gemini_key = "" if clear_gemini_key else (str(gemini_key) if gemini_key not in (None, "") else cur["gemini"]["key"])
    new = {
        "routing": _pick(routing, ROUTINGS, cur["routing"]) if routing is not None else cur["routing"],
        "main": _pick(main, PROVIDERS, cur["main"]) if main is not None else cur["main"],
        "multilingual": _pick(multilingual, PROVIDERS, cur["multilingual"]) if multilingual is not None else cur["multilingual"],
        "scanned": _pick(scanned, SCANNED, cur["scanned"]) if scanned is not None else cur["scanned"],
        "fallback": (fallback in (True, 1, "1", "true", "on")) if fallback is not None else cur["fallback"],
        "gemini_key": new_gemini_key,
        "gemini_model": (str(gemini_model).strip().removeprefix("models/") if gemini_model is not None
                         else cur["gemini"]["model"]),
        "medha_url": (str(medha_url).strip().rstrip("/") if medha_url is not None else cur["medha"]["url"]),
    }
    configured = {"medha": bool(new["medha_url"]), "gemini": bool(new["gemini_key"] and new["gemini_model"])}
    used = [new["main"]] + ([new["multilingual"]] if new["routing"] == "hybrid" else [])
    for p in used:
        if not configured[p]:
            raise ConfigError("Gemini is selected but has no API key and model" if p == "gemini"
                              else "Medha is selected but has no endpoint URL")
    if new["routing"] == "hybrid" and new["main"] == new["multilingual"]:
        raise ConfigError("Hybrid uses two different models — pick another model for one of the "
                          "languages, or choose “One model for everything”")

    if medha_url is not None or medha_key not in (None, "") or medha_model is not None:
        set_model_config(url=medha_url, key=medha_key, model=medha_model)
    updates = {"llm_routing": new["routing"], "llm_main": new["main"], "llm_multilingual": new["multilingual"],
               "llm_scanned": new["scanned"], "llm_fallback": "1" if new["fallback"] else "0",
               "gemini_model": new["gemini_model"]}
    if gemini_key not in (None, "") or clear_gemini_key:
        updates["gemini_api_key"] = new["gemini_key"]
    with pg.pool().connection() as c:
        for k, v in updates.items():
            c.execute("INSERT INTO runtime_config(key, value) VALUES(%s, %s) "
                      "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", (k, v))
    global _cache_ts
    with _lock:
        _cache_ts = 0.0
    return extraction_masked(fresh=True)


def _mask(k: str) -> str:
    return ("•" * max(0, len(k) - 4) + k[-4:]) if k else ""


def extraction_masked(fresh: bool = False) -> dict:
    """The extraction setup, safe for the browser: keys redacted to their last 4 characters."""
    c = extraction_config(fresh)
    return {
        "routing": c["routing"], "main": c["main"], "multilingual": c["multilingual"],
        "scanned": c["scanned"], "fallback": c["fallback"],
        "medha": {"url": c["medha"]["url"], "model": c["medha"]["model"],
                  "has_key": bool(c["medha"]["key"]), "key_masked": _mask(c["medha"]["key"]),
                  "configured": bool(c["medha"]["url"])},
        "gemini": {"model": c["gemini"]["model"], "has_key": bool(c["gemini"]["key"]),
                   "key_masked": _mask(c["gemini"]["key"]),
                   "configured": bool(c["gemini"]["key"] and c["gemini"]["model"])},
    }
