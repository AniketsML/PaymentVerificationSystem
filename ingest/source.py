"""
Source clients — where new leads are read FROM. One interface, two backends chosen by
SOURCE_MODE:

  db        DBSourceClient       direct read-only SQL to the production DB (recommended).
                                 Engine-agnostic via SQLAlchemy Core (Postgres, MySQL, …).
  metabase  MetabaseSourceClient a saved question parameterised on the watermark. Strictly
                                 worse (session tokens, row caps) but works when only
                                 Metabase access is granted.

Fetch semantics: return raw rows with `created_at >= since`, ordered by (created_at, id).
The ingester walks `since` forward and relies on idempotent enqueue to absorb the overlap
re-reads — so a row is never MISSED regardless of the source id type (int / uuid / text).
Identifiers come from the trusted mapping config and are validated as plain identifiers
before interpolation; all values are bound parameters.
"""
from __future__ import annotations

import re
from typing import Protocol

from config import settings

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _ident(name: str, what: str) -> str:
    if not name or not _IDENT.match(name):
        raise ValueError(f"unsafe/empty {what} in source_mapping.json: {name!r}")
    return name


class SourceClient(Protocol):
    def fetch_since(self, since, limit: int) -> list: ...
    def close(self) -> None: ...


# ── direct DB backend ─────────────────────────────────────────────────────────
def _normalize_url(url: str) -> str:
    """This project standardizes on psycopg v3, but SQLAlchemy still defaults a bare
    `postgresql://` URL to the (uninstalled) psycopg2 driver. Pin it to psycopg v3 so an
    operator never has to remember the `+psycopg` suffix. Other engines pass through — a
    MySQL source just uses `mysql+pymysql://…` as written."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgresql+asyncpg://"):
        if url.startswith(prefix):
            return url                              # explicit driver — respect it
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


class DBSourceClient:
    def __init__(self, mapping, url: str | None = None):
        from sqlalchemy import create_engine
        self.m = mapping
        url = url or settings.SOURCE_DATABASE_URL
        if not url:
            raise ValueError("SOURCE_DATABASE_URL is not set (SOURCE_MODE=db)")
        # pool_pre_ping recycles stale connections transparently; small pool — one ingester.
        self.engine = create_engine(_normalize_url(url), pool_pre_ping=True,
                                    pool_size=2, max_overflow=2, pool_recycle=1800)
        self.table = _ident(mapping.table, "table")
        self.created = _ident(mapping.created_at_col, "created_at_column")
        self.idc = _ident(mapping.id_col, "id_column")

    def fetch_since(self, since, limit: int) -> list:
        from sqlalchemy import text
        sql = (f"SELECT * FROM {self.table} "
               f"WHERE {self.created} >= :since "
               f"ORDER BY {self.created}, {self.idc} LIMIT :lim")
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), {"since": since, "lim": int(limit)}).mappings().all()
        return [dict(r) for r in rows]

    def close(self) -> None:
        try:
            self.engine.dispose()
        except Exception:
            pass


# ── Metabase backend ──────────────────────────────────────────────────────────
class MetabaseSourceClient:
    """Reads via a saved Metabase question (card) parameterised on a `since` date/datetime.
    The card MUST order by (created_at, id) and expose a template parameter named `since`.
    Session token is fetched on demand and refreshed on 401."""

    def __init__(self, mapping):
        import requests
        self.m = mapping
        self.base = (settings.METABASE_URL or "").rstrip("/")
        self.card = settings.METABASE_CARD_ID
        if not (self.base and self.card):
            raise ValueError("METABASE_URL and METABASE_CARD_ID are required (SOURCE_MODE=metabase)")
        self._s = requests.Session()
        self._token = None

    def _login(self):
        r = self._s.post(f"{self.base}/api/session",
                         json={"username": settings.METABASE_USER, "password": settings.METABASE_PASS},
                         timeout=30)
        r.raise_for_status()
        self._token = r.json()["id"]
        self._s.headers.update({"X-Metabase-Session": self._token})

    def fetch_since(self, since, limit: int) -> list:
        if not self._token:
            self._login()
        payload = {"parameters": [
            {"type": "date/all-options", "target": ["variable", ["template-tag", "since"]],
             "value": str(since)}]}
        url = f"{self.base}/api/card/{self.card}/query/json"
        r = self._s.post(url, json=payload, timeout=120)
        if r.status_code in (401, 403):                     # token expired -> re-login once
            self._login()
            r = self._s.post(url, json=payload, timeout=120)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):                      # Metabase returns a list of row dicts
            raise ValueError(f"unexpected Metabase response shape: {type(rows).__name__}")
        return rows[:limit] if limit else rows

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass


def build_source(mapping) -> SourceClient:
    mode = settings.SOURCE_MODE
    if mode == "db":
        return DBSourceClient(mapping)
    if mode == "metabase":
        return MetabaseSourceClient(mapping)
    raise ValueError(f"SOURCE_MODE must be 'db' or 'metabase' to ingest (got {mode!r})")
