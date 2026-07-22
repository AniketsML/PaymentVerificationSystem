"""
The incremental cursor. One durable `(created_at, id)` watermark per source, advanced
ONLY after a batch is safely enqueued — so a crash between fetch and enqueue re-reads, and
idempotent enqueue makes the replay free. The cursor never moves backward in live mode
(a backfill reset is the one deliberate exception).
"""
from __future__ import annotations

from datetime import datetime, timezone

from db import pg


def read(source: str):
    """Return (wm_ts, wm_id) or (None, '') if this source has never been ingested."""
    with pg.pool().connection() as c:
        r = c.execute("SELECT wm_ts, wm_id FROM ingest_watermarks WHERE source=%s",
                      (source,)).fetchone()
    return (r["wm_ts"], r["wm_id"]) if r else (None, "")


def seed_if_absent(source: str, ts: datetime) -> bool:
    """First run only: seed the cursor so LIVE mode picks up NEW leads from `ts` onward
    (default now) instead of accidentally ingesting all history. Returns True if seeded."""
    with pg.pool().connection() as c:
        r = c.execute(
            "INSERT INTO ingest_watermarks(source, wm_ts, wm_id) VALUES(%s,%s,'') "
            "ON CONFLICT (source) DO NOTHING RETURNING source", (source, ts)).fetchone()
    return bool(r)


def advance(source: str, wm_ts: datetime, wm_id: str, allow_backward: bool = False) -> None:
    """Move the cursor to (wm_ts, wm_id). In live mode the guard refuses to move backward
    (a stale/late row can never rewind progress); backfill passes allow_backward=True."""
    guard = "" if allow_backward else \
        "AND (%(ts)s, %(id)s) > (ingest_watermarks.wm_ts, ingest_watermarks.wm_id)"
    with pg.pool().connection() as c:
        c.execute(
            "INSERT INTO ingest_watermarks(source, wm_ts, wm_id, updated_at) "
            "VALUES(%(src)s, %(ts)s, %(id)s, now()) "
            "ON CONFLICT (source) DO UPDATE SET wm_ts=EXCLUDED.wm_ts, wm_id=EXCLUDED.wm_id, "
            f"updated_at=now() WHERE TRUE {guard}",
            {"src": source, "ts": wm_ts, "id": str(wm_id)})


def reset(source: str, to_ts: datetime) -> None:
    """Backfill: rewind the cursor to `to_ts` (id cleared) so the next cycle re-reads from
    there. Deliberate and explicit — only the --backfill path calls this."""
    with pg.pool().connection() as c:
        c.execute(
            "INSERT INTO ingest_watermarks(source, wm_ts, wm_id, updated_at) "
            "VALUES(%s,%s,'',now()) ON CONFLICT (source) DO UPDATE SET "
            "wm_ts=EXCLUDED.wm_ts, wm_id='', updated_at=now()", (source, to_ts))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
