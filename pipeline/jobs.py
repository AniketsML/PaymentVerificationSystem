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

import hashlib
import json
import os
import re
import time

from psycopg.types.json import Jsonb

from config import settings
from db import pg


def _default_lead_id(row: dict) -> str:
    """Stable content-hash id used when no id column is chosen. The SAME row re-uploaded
    yields the SAME id (so resume/idempotency still works), but rows from a DIFFERENT
    file no longer collide on LEAD-0..N and get silently skipped as 'already done'."""
    blob = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    return "LEAD-" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _safe(obj):
    return json.loads(json.dumps(obj or {}, ensure_ascii=False, default=str))


# Columns we'll look in for the image link, in priority order. The user-selected column
# is tried first; these are fallbacks so a link is found even if the wrong column (or a
# plural/singular variant) was chosen — maximising image visibility.
_IMG_COL_FALLBACKS = ("payment_documents", "payment_document", "payment_proof",
                      "payment_receipt", "document_url", "image_url", "image", "document")

_URL_RE = re.compile(r'https?://[^\s"\'\\\]}<>]+')
_IMG_EXT_RE = re.compile(r'\.(jpe?g|png|webp|gif|bmp|tiff?|heic|heif)(\?|#|$)', re.I)


def _looks_like_image(link: str) -> bool:
    """A link that is clearly an image — by data-URI type or a file extension. Used to
    prefer the right URL when scanning unknown columns."""
    s = (link or "").lower()
    return s.startswith("data:image/") or bool(_IMG_EXT_RE.search(s))


def _clean_source(value) -> str:
    """Pull a loadable image source out of a raw cell value.

    Handles plain URLs / local paths AND the JSON-array-encoded form some exports use,
    e.g.  ["https://.../img.jpeg"]  or the double-escaped  "[\\"https://...\\"]"  — a
    URL regex lifts the real link straight out of that garbage. Only the FIRST source is
    used (one image per lead)."""
    s = str(value or "").strip()
    if not s:
        return ""
    m = _URL_RE.search(s)
    if m:
        return m.group(0).rstrip('.,;')          # first http(s) URL, even inside ["..."]
    # no URL — maybe a JSON array/string of local paths, else the raw value
    if s[0] in '["':
        try:
            v = json.loads(s)
            if isinstance(v, str) and v[:1] in '["':   # double-encoded
                v = json.loads(v)
            if isinstance(v, list):
                return str(next((x for x in v if str(x).strip()), "")).strip()
            if isinstance(v, str):
                return v.strip()
        except Exception:
            return s.strip('[]"\\ ')
    return s


def _resolve_image(row: dict, image_col: str, image_root: str) -> str:
    # 1) the selected column, then 2) the known fallback names — in priority order.
    cols = [image_col] + [c for c in _IMG_COL_FALLBACKS if c != image_col]
    img = ""
    for col in cols:
        img = _clean_source(row.get(col, ""))
        if img:
            break

    # 3) dynamic fallback: no named column had a link -> scan EVERY remaining column for a
    # URL/data-URI, so link-finding works regardless of how the column is named. Prefer an
    # image-looking URL so an unrelated link (profile/source/callback) isn't grabbed by
    # mistake; only fall back to a non-image URL when that's the sole candidate.
    if not img:
        seen = set(cols)
        urls = []
        for key, val in row.items():
            if key in seen:
                continue
            c = _clean_source(val)
            if c and (c.lower().startswith("http") or c.lower().startswith("data:")):
                urls.append(c)
        img = next((u for u in urls if _looks_like_image(u)), "")
        if not img and len(urls) == 1:          # unambiguous single URL -> use it
            img = urls[0]

    if image_root and img and not os.path.isabs(img) and not img.lower().startswith("http"):
        img = os.path.join(image_root, img)
    return img


def enqueue_rows(rows: list, image_col="payment_document", id_col=None,
                 image_root="", precomputed=False, is_test=False) -> dict:
    prefix = "test" if is_test else "batch"
    batch_id = f"{prefix}-{int(time.time())}"
    enq = skipped = 0
    with pg.pool().connection() as c:
        for row in rows:
            chosen = str(row.get(id_col, "")).strip() if id_col else ""
            lead_id = chosen or _default_lead_id(row)   # content-hash when no usable id
            r = c.execute(
                "INSERT INTO jobs(job_id,batch_id,lead_id,lender,image_url,row_json,"
                "precomputed,max_attempts,is_test) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (job_id) DO NOTHING",
                (lead_id, batch_id, lead_id, row.get("institute_name", ""),
                 _resolve_image(row, image_col, image_root), Jsonb(_safe(row)),
                 precomputed, settings.JOB_MAX_ATTEMPTS, is_test))
            enq += r.rowcount
            skipped += (1 - r.rowcount)
    return {"batch_id": batch_id, "total": len(rows), "enqueued": enq, "skipped": skipped,
            "is_test": is_test}


def enqueue_ingested(items: list, batch_id: str) -> tuple[int, int]:
    """Enqueue rows already mapped + validated + image-resolved by the ingester. Each item:
    {lead_id, lender, image_ref, row, priority}. Returns (enqueued, duplicates). Idempotent —
    a re-read of the same lead (overlap window / replay) is a free ON CONFLICT no-op.

    image_url carries the loadable source (a `blob:<sha>` ref when prefetched, else the URL);
    image_ref keeps the same value as provenance. Real data only (is_test always FALSE)."""
    enq = dup = 0
    with pg.pool().connection() as c:
        for it in items:
            ref = it.get("image_ref") or ""
            r = c.execute(
                "INSERT INTO jobs(job_id,batch_id,lead_id,lender,image_url,image_ref,priority,"
                "row_json,precomputed,max_attempts,is_test) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,FALSE) "
                "ON CONFLICT (job_id) DO NOTHING",
                (it["lead_id"], batch_id, it["lead_id"], it.get("lender", ""), ref, ref,
                 int(it.get("priority", 5)), Jsonb(_safe(it.get("row", {}))),
                 settings.JOB_MAX_ATTEMPTS))
            if r.rowcount:
                enq += 1
            else:
                dup += 1
    return enq, dup


def claim_one(lease_seconds: int | None = None):
    lease = lease_seconds or settings.JOB_LEASE_SECONDS
    with pg.pool().connection() as c:
        # identical claim (pending OR expired-lease), but also returns the row's PRIOR
        # status/attempts so the worker can log a lease-reclaim (crash-recovery) event.
        return c.execute(
            "WITH picked AS ("
            "  SELECT job_id, status AS prev_status, attempts AS prev_attempts FROM jobs "
            "  WHERE status='pending' OR (status='in_progress' AND lease_until < now()) "
            "  ORDER BY priority, created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE jobs j SET status='in_progress', attempts=j.attempts+1, "
            "  lease_until = now() + make_interval(secs => %s), updated_at=now() "
            "FROM picked WHERE j.job_id = picked.job_id "
            "RETURNING j.*, picked.prev_status, picked.prev_attempts", (lease,)).fetchone()


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
