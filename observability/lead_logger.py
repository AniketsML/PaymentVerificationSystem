"""
Lead-cycle logging.

Every stage of every lead writes a structured event here. You can then pull the
COMPLETE journey of any lead by its id:

    from observability.lead_logger import LeadLogger
    LeadLogger().get_lead_journey("LEAD-123")

Storage (why SQLite here, what to use in production - see README):
  - `lead_events`  : append-only event log (stage, status, reason, metrics)
  - `lead_results` : the final verification row (what Metabase reads)
Both live in one SQLite file so the app, the batch runner and Metabase can all
read a single source of truth. Swap the connection string for Postgres in prod
with no code change to the callers.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

_LOCK = threading.Lock()


class LeadLogger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with _LOCK, self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS lead_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id    TEXT NOT NULL,
                    ts         TEXT NOT NULL,
                    stage      TEXT NOT NULL,
                    status     TEXT NOT NULL,
                    reason     TEXT,
                    ms         REAL,
                    metrics    TEXT,
                    data       TEXT
                )""")
            # tolerate an older DB created before the ms column existed
            cols = {r[1] for r in c.execute("PRAGMA table_info(lead_events)")}
            if "ms" not in cols:
                c.execute("ALTER TABLE lead_events ADD COLUMN ms REAL")
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_lead ON lead_events(lead_id)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS lead_results (
                    lead_id             TEXT PRIMARY KEY,
                    lender              TEXT,
                    verification_status TEXT,
                    payment_method      TEXT,
                    outcome             TEXT,
                    extracted           TEXT,
                    updated_at          TEXT
                )""")

    # ── event logging ─────────────────────────────────────────────────────────
    def log(self, lead_id: str, stage: str, status: str,
            reason: str = "", metrics: dict | None = None, data: dict | None = None,
            ms: float | None = None):
        with _LOCK, self._conn() as c:
            c.execute(
                "INSERT INTO lead_events(lead_id,ts,stage,status,reason,ms,metrics,data) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (lead_id, datetime.now(timezone.utc).isoformat(), stage, status, reason, ms,
                 json.dumps(metrics or {}, ensure_ascii=False),
                 json.dumps(data or {}, ensure_ascii=False, default=str)),
            )

    def save_result(self, lead_id: str, lender: str, verification_status: str,
                    payment_method: str, outcome: dict, extracted: dict):
        with _LOCK, self._conn() as c:
            c.execute(
                "REPLACE INTO lead_results(lead_id,lender,verification_status,payment_method,"
                "outcome,extracted,updated_at) VALUES(?,?,?,?,?,?,?)",
                (lead_id, lender, verification_status, payment_method,
                 json.dumps(outcome, ensure_ascii=False),
                 json.dumps(extracted, ensure_ascii=False, default=str),
                 datetime.now(timezone.utc).isoformat()),
            )

    # ── retrieval (debugging) ─────────────────────────────────────────────────
    def get_lead_journey(self, lead_id: str) -> dict:
        with self._conn() as c:
            events = [dict(r) for r in c.execute(
                "SELECT ts,stage,status,reason,ms,metrics,data FROM lead_events "
                "WHERE lead_id=? ORDER BY id", (lead_id,))]
            res = c.execute("SELECT * FROM lead_results WHERE lead_id=?", (lead_id,)).fetchone()
        for e in events:
            e["metrics"] = json.loads(e["metrics"] or "{}")
            e["data"] = json.loads(e["data"] or "{}")
        return {
            "lead_id": lead_id,
            "final": dict(res) if res else None,
            "journey": events,
        }

    def all_results(self) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM lead_results ORDER BY updated_at DESC")]

    def recent_results(self, limit: int = 200) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM lead_results ORDER BY updated_at DESC LIMIT ?", (limit,))]

    def status_counts(self) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT verification_status AS s, COUNT(*) AS n FROM lead_results GROUP BY s")
            counts = {r["s"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    def method_counts(self) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT COALESCE(NULLIF(payment_method,''),'Non-document') AS m, COUNT(*) AS n "
                "FROM lead_results GROUP BY m ORDER BY n DESC")
            return [{"method": r["m"], "n": r["n"]} for r in rows]

    def query_results(self, status: str | None = None, q: str | None = None,
                      limit: int = 200, offset: int = 0) -> list:
        sql = "SELECT * FROM lead_results"
        where, params = [], []
        if status and status not in ("all", ""):
            where.append("verification_status = ?")
            params.append(status)
        if q:
            where.append("(lead_id LIKE ? OR lender LIKE ? OR payment_method LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, params)]
