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
    def log(self, lead_id, stage, status, reason="", metrics=None, data=None, ms=None,
            is_test=False):
        with pg.pool().connection() as c:
            c.execute(
                "INSERT INTO lead_events(lead_id,stage,status,reason,ms,metrics,data,is_test) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (lead_id, stage, status, reason, ms,
                 Jsonb(_safe(metrics)), Jsonb(_safe(data)), is_test),
            )

    def save_result(self, lead_id, lender, verification_status, payment_method, outcome,
                    extracted, is_test=False):
        with pg.pool().connection() as c:
            c.execute(
                "INSERT INTO lead_results(lead_id,lender,verification_status,payment_method,"
                "outcome,extracted,is_test,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT (lead_id) DO UPDATE SET lender=EXCLUDED.lender,"
                "verification_status=EXCLUDED.verification_status,"
                "payment_method=EXCLUDED.payment_method,outcome=EXCLUDED.outcome,"
                "extracted=EXCLUDED.extracted,is_test=EXCLUDED.is_test,updated_at=now()",
                (lead_id, lender, verification_status, payment_method,
                 Jsonb(_safe(outcome)), Jsonb(_safe(extracted)), is_test),
            )

    # ── human-review loop ─────────────────────────────────────────────────────
    def save_review(self, lead_id, system_status, decision, corrected_status=None,
                    reviewer="", note="", is_test=False) -> dict:
        """Record a reviewer's action. `decision` is 'confirmed' or 'overturned';
        `corrected_status` is the reviewer's verdict when overturned. Append-only —
        the full history is kept; the latest row is the current state. is_test keeps a
        sandbox lead's review out of the REAL accuracy ground truth."""
        decision = "overturned" if decision == "overturned" else "confirmed"
        if decision == "confirmed":
            corrected_status = system_status
        with pg.pool().connection() as c:
            row = c.execute(
                "INSERT INTO lead_reviews(lead_id,system_status,decision,corrected_status,"
                "reviewer,note,is_test) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,ts",
                (lead_id, system_status, decision, corrected_status, reviewer, note, is_test),
            ).fetchone()
        return {"id": row["id"], "ts": _iso(row["ts"]), "lead_id": lead_id,
                "system_status": system_status, "decision": decision,
                "corrected_status": corrected_status, "reviewer": reviewer, "note": note}

    def latest_review(self, lead_id) -> dict | None:
        """The current review state for one lead (most recent action), or None."""
        with pg.pool().connection() as c:
            r = c.execute(
                "SELECT * FROM lead_reviews WHERE lead_id=%s ORDER BY id DESC LIMIT 1",
                (lead_id,)).fetchone()
        if r:
            r["ts"] = _iso(r["ts"])
        return r

    def review_history(self, lead_id) -> list:
        with pg.pool().connection() as c:
            rows = c.execute(
                "SELECT * FROM lead_reviews WHERE lead_id=%s ORDER BY id", (lead_id,)).fetchall()
        for r in rows:
            r["ts"] = _iso(r["ts"])
        return rows

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

    # scope: 'real' (default, excludes sandbox/test), 'test' (only test), 'all'
    @staticmethod
    def _scope_sql(scope):
        if scope == "test":
            return "is_test"
        if scope == "all":
            return "TRUE"
        return "NOT is_test"

    def all_results(self) -> list:
        return self._rows("SELECT * FROM lead_results_real ORDER BY updated_at DESC")

    def recent_results(self, limit=200) -> list:
        return self._rows("SELECT * FROM lead_results_real "
                          "ORDER BY updated_at DESC LIMIT %s", (limit,))

    def query_results(self, status=None, q=None, limit=300, offset=0, scope="real") -> list:
        sql = "SELECT * FROM lead_results"
        where, params = [self._scope_sql(scope)], []
        if status and status not in ("all", ""):
            where.append("verification_status = %s")
            params.append(status)
        if q:
            where.append("(lead_id ILIKE %s OR lender ILIKE %s OR payment_method ILIKE %s)")
            like = f"%{q}%"
            params += [like, like, like]
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        params += [int(limit), int(offset)]
        return self._rows(sql, tuple(params))

    def status_counts(self, scope="real") -> dict:
        with pg.pool().connection() as c:
            rows = c.execute(
                f"SELECT verification_status AS s, COUNT(*) AS n FROM lead_results "
                f"WHERE {self._scope_sql(scope)} GROUP BY s").fetchall()
        counts = {r["s"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    def test_count(self) -> int:
        with pg.pool().connection() as c:
            return c.execute("SELECT COUNT(*) AS n FROM lead_results WHERE is_test").fetchone()["n"]

    def method_counts(self, scope="real") -> list:
        rel = "lead_results_test" if scope == "test" else "lead_results_real"
        with pg.pool().connection() as c:
            rows = c.execute(
                "SELECT COALESCE(NULLIF(payment_method,''),'Non-document') AS m, COUNT(*) AS n "
                f"FROM {rel} GROUP BY m ORDER BY n DESC").fetchall()
        return [{"method": r["m"], "n": r["n"]} for r in rows]
