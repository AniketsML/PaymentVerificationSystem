"""
PostgreSQL-backed lead logger. Same interface as the old SQLite LeadLogger, so
the pipeline (orchestrator.process_lead) uses it unchanged - only the storage
engine changed. JSON columns are real JSONB, so Metabase/SQL can query inside
metrics/outcome/extracted directly.
"""
from __future__ import annotations

import json

from psycopg.types.json import Jsonb

from db import pg


def _safe(obj):
    """Guarantee JSON-serialisable (mirrors the old json.dumps(default=str))."""
    return json.loads(json.dumps(obj or {}, ensure_ascii=False, default=str))


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


class PgLeadLogger:
    def __init__(self, *_args, **_kwargs):
        pg.init_schema()

    # ── event logging ─────────────────────────────────────────────────────────
    def log(self, lead_id, stage, status, reason="", metrics=None, data=None, ms=None):
        with pg.pool().connection() as c:
            c.execute(
                "INSERT INTO lead_events(lead_id,stage,status,reason,ms,metrics,data) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (lead_id, stage, status, reason, ms,
                 Jsonb(_safe(metrics)), Jsonb(_safe(data))),
            )

    def save_result(self, lead_id, lender, verification_status, payment_method, outcome, extracted):
        with pg.pool().connection() as c:
            c.execute(
                "INSERT INTO lead_results(lead_id,lender,verification_status,payment_method,"
                "outcome,extracted,updated_at) VALUES(%s,%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT (lead_id) DO UPDATE SET lender=EXCLUDED.lender,"
                "verification_status=EXCLUDED.verification_status,"
                "payment_method=EXCLUDED.payment_method,outcome=EXCLUDED.outcome,"
                "extracted=EXCLUDED.extracted,updated_at=now()",
                (lead_id, lender, verification_status, payment_method,
                 Jsonb(_safe(outcome)), Jsonb(_safe(extracted))),
            )

    # ── retrieval ─────────────────────────────────────────────────────────────
    def get_lead_journey(self, lead_id) -> dict:
        with pg.pool().connection() as c:
            events = c.execute(
                "SELECT ts,stage,status,reason,ms,metrics,data FROM lead_events "
                "WHERE lead_id=%s ORDER BY id", (lead_id,)).fetchall()
            res = c.execute("SELECT * FROM lead_results WHERE lead_id=%s",
                            (lead_id,)).fetchone()
        for e in events:
            e["ts"] = _iso(e["ts"])
        if res:
            res["updated_at"] = _iso(res["updated_at"])
        return {"lead_id": lead_id, "final": res, "journey": events}

    def _rows(self, sql, params=()) -> list:
        with pg.pool().connection() as c:
            rows = c.execute(sql, params).fetchall()
        for r in rows:
            if "updated_at" in r:
                r["updated_at"] = _iso(r["updated_at"])
        return rows

    def all_results(self) -> list:
        return self._rows("SELECT * FROM lead_results ORDER BY updated_at DESC")

    def recent_results(self, limit=200) -> list:
        return self._rows("SELECT * FROM lead_results ORDER BY updated_at DESC LIMIT %s", (limit,))

    def query_results(self, status=None, q=None, limit=300, offset=0) -> list:
        sql = "SELECT * FROM lead_results"
        where, params = [], []
        if status and status not in ("all", ""):
            where.append("verification_status = %s")
            params.append(status)
        if q:
            where.append("(lead_id ILIKE %s OR lender ILIKE %s OR payment_method ILIKE %s)")
            like = f"%{q}%"
            params += [like, like, like]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        params += [int(limit), int(offset)]
        return self._rows(sql, tuple(params))

    def status_counts(self) -> dict:
        with pg.pool().connection() as c:
            rows = c.execute(
                "SELECT verification_status AS s, COUNT(*) AS n FROM lead_results GROUP BY s"
            ).fetchall()
        counts = {r["s"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    def method_counts(self) -> list:
        with pg.pool().connection() as c:
            rows = c.execute(
                "SELECT COALESCE(NULLIF(payment_method,''),'Non-document') AS m, COUNT(*) AS n "
                "FROM lead_results GROUP BY m ORDER BY n DESC").fetchall()
        return [{"method": r["m"], "n": r["n"]} for r in rows]
