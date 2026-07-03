"""
Durable job queue (Postgres). One row per lead in `jobs`.

  - enqueue_rows : INSERT ... ON CONFLICT (job_id) DO NOTHING  -> re-uploading a
                   file only adds leads not already queued/done. job_id = lead_id,
                   so "already processed?" is answered by the job's presence. This
                   is your resume/idempotency, keyed on the id you choose.
  - claim_one    : atomic claim with FOR UPDATE SKIP LOCKED + a lease, so many
                   workers pull different jobs and a crashed worker's job (expired
                   lease) is automatically re-claimable.
  - complete/fail: terminal transitions; fail() retries up to max_attempts, then
                   parks the job as 'failed' for a human.

Job identity (idempotency) is deliberately SEPARATE from the payment-reference
duplicate ledger - re-running a lead never looks like a duplicate payment.
"""
from __future__ import annotations

import json
import os
import time

from psycopg.types.json import Jsonb

from config import settings
from db import pg


def _safe(obj):
    return json.loads(json.dumps(obj or {}, ensure_ascii=False, default=str))


def _resolve_image(row: dict, image_col: str, image_root: str) -> str:
    img = str(row.get(image_col, "") or "")
    if image_root and img and not os.path.isabs(img) and not img.lower().startswith("http"):
        img = os.path.join(image_root, img)
    return img


def enqueue_rows(rows: list, image_col="payment_document", id_col=None,
                 image_root="", precomputed=False) -> dict:
    batch_id = f"batch-{int(time.time())}"
    enq = skipped = 0
    with pg.pool().connection() as c:
        for idx, row in enumerate(rows):
            lead_id = str(row.get(id_col) if id_col else f"LEAD-{idx}")
            r = c.execute(
                "INSERT INTO jobs(job_id,batch_id,lead_id,lender,image_url,row_json,"
                "precomputed,max_attempts) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (job_id) DO NOTHING",
                (lead_id, batch_id, lead_id, row.get("institute_name", ""),
                 _resolve_image(row, image_col, image_root), Jsonb(_safe(row)),
                 precomputed, settings.JOB_MAX_ATTEMPTS))
            enq += r.rowcount
            skipped += (1 - r.rowcount)
    return {"batch_id": batch_id, "total": len(rows), "enqueued": enq, "skipped": skipped}


def claim_one(lease_seconds: int | None = None):
    lease = lease_seconds or settings.JOB_LEASE_SECONDS
    with pg.pool().connection() as c:
        return c.execute(
            "UPDATE jobs SET status='in_progress', attempts=attempts+1, "
            "lease_until = now() + make_interval(secs => %s), updated_at=now() "
            "WHERE job_id = (SELECT job_id FROM jobs "
            "  WHERE status='pending' OR (status='in_progress' AND lease_until < now()) "
            "  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING *", (lease,)).fetchone()


def complete(job_id: str, verification_status: str):
    with pg.pool().connection() as c:
        c.execute("UPDATE jobs SET status='done', verification_status=%s, last_error=NULL, "
                  "updated_at=now() WHERE job_id=%s", (verification_status, job_id))


def fail(job_id: str, err: str):
    with pg.pool().connection() as c:
        c.execute(
            "UPDATE jobs SET status = CASE WHEN attempts >= max_attempts THEN 'failed' "
            "ELSE 'pending' END, last_error=%s, lease_until=NULL, updated_at=now() "
            "WHERE job_id=%s", (str(err)[:1000], job_id))


def open_work_count() -> int:
    with pg.pool().connection() as c:
        return c.execute("SELECT COUNT(*) AS n FROM jobs "
                         "WHERE status IN ('pending','in_progress')").fetchone()["n"]


def batch_progress(batch_id: str) -> dict:
    with pg.pool().connection() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM jobs WHERE batch_id=%s "
                         "GROUP BY status", (batch_id,)).fetchall()
        vc = c.execute("SELECT verification_status AS s, COUNT(*) AS n FROM jobs "
                       "WHERE batch_id=%s AND status='done' GROUP BY s", (batch_id,)).fetchall()
    by = {r["status"]: r["n"] for r in rows}
    return {
        "batch_id": batch_id, "total": sum(by.values()),
        "pending": by.get("pending", 0), "in_progress": by.get("in_progress", 0),
        "done": by.get("done", 0), "failed": by.get("failed", 0),
        "verdicts": {r["s"]: r["n"] for r in vc},
    }


def get_row(job_id: str):
    """The exact input row (CSV/Excel) that produced this lead, for side-by-side review."""
    with pg.pool().connection() as c:
        r = c.execute("SELECT row_json FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
    return r["row_json"] if r else None


def batch_leads(batch_id: str, limit=500) -> list:
    with pg.pool().connection() as c:
        rows = c.execute(
            "SELECT j.lead_id, j.lender, j.status AS job_status, j.verification_status, "
            "j.last_error, r.payment_method, r.outcome "
            "FROM jobs j LEFT JOIN lead_results r ON r.lead_id = j.lead_id "
            "WHERE j.batch_id=%s ORDER BY j.updated_at DESC LIMIT %s",
            (batch_id, limit)).fetchall()
    return rows
