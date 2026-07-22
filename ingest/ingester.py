"""
The ingester poll loop — the orchestrator that turns "rows appearing in the source DB" into
"leads in the durable queue," safely and idempotently.

One cycle:
  1. read the watermark (seed to now() on first run so live mode starts fresh, not from
     the beginning of history)
  2. fetch rows with created_at >= (watermark − overlap), paged
  3. map + validate each row → quarantine the bad ones (never the pipeline)
  4. resolve + prefetch the image while its URL is fresh
  5. idempotently enqueue into `jobs`
  6. advance the watermark ONLY after the batch is safely enqueued

Invariants that make it unbreakable:
  · at-least-once fetch + idempotent enqueue = exactly-once effect
  · the watermark never advances on a failed cycle (source down → retry next interval)
  · a single Postgres advisory lock guarantees only one ingester runs
  · SIGTERM drains the current cycle and exits cleanly
"""
from __future__ import annotations

import hashlib
import json
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from config import settings
from db import pg
from ingest import images, watermark
from ingest.mapping import load_mapping
from ingest.source import build_source
from pipeline import jobs

_stop = threading.Event()


# ── time helpers ──────────────────────────────────────────────────────────────
def _as_dt(v) -> datetime:
    """Normalise any cursor value to tz-aware UTC so all comparisons are safe and
    consistent (naive values are treated as UTC)."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    from dateutil import parser as dp
    d = dp.parse(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _since_str(dt: datetime) -> str:
    """A portable UTC string the DB casts to its own created_at type (avoids Python
    tz-vs-column mismatches). If the source stores LOCAL time, widen INGEST_OVERLAP_MIN."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── ingest_runs bookkeeping ───────────────────────────────────────────────────
def _start_run(source: str, mode: str) -> int:
    with pg.pool().connection() as c:
        return c.execute("INSERT INTO ingest_runs(source, mode) VALUES(%s,%s) RETURNING id",
                         (source, mode)).fetchone()["id"]


def _finish_run(run_id: int, s: dict, ok: bool, error: str = None) -> None:
    with pg.pool().connection() as c:
        c.execute(
            "UPDATE ingest_runs SET finished_at=now(), rows_seen=%s, enqueued=%s, duplicates=%s, "
            "quarantined=%s, images_prefetched=%s, prefetch_failed=%s, ok=%s, error=%s WHERE id=%s",
            (s["rows_seen"], s["enqueued"], s["duplicates"], s["quarantined"],
             s["images_prefetched"], s["prefetch_failed"], ok, (error or None)[:1000] if error else None, run_id))


def _quarantine(source: str, lead_id: str, raw: dict, reason: str) -> None:
    key = lead_id or ("h:" + hashlib.sha1(
        json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16])
    with pg.pool().connection() as c:
        # the WHERE predicate matches the PARTIAL unique index (idx_quarantine_key). The
        # ingester always sets a non-null key, so this upserts cleanly (no duplicate spam
        # from the overlap re-read).
        c.execute(
            "INSERT INTO ingest_quarantine(source, source_key, source_row, reason) "
            "VALUES(%s,%s,%s,%s) ON CONFLICT (source, source_key) WHERE source_key IS NOT NULL "
            "DO UPDATE SET source_row=EXCLUDED.source_row, reason=EXCLUDED.reason, resolved=FALSE",
            (source, key, Jsonb(_json_safe(raw)), reason[:500]))


def _json_safe(obj):
    return json.loads(json.dumps(obj or {}, ensure_ascii=False, default=str))


# ── one poll cycle ────────────────────────────────────────────────────────────
def run_cycle(mapping, client, source: str, mode: str = "live") -> dict:
    s = {"rows_seen": 0, "enqueued": 0, "duplicates": 0, "quarantined": 0,
         "images_prefetched": 0, "prefetch_failed": 0, "error": None}
    run_id = _start_run(source, mode)
    try:
        wm_ts, wm_id = watermark.read(source)
        if wm_ts is None:
            # first run: seed so LIVE picks up new leads from now (backfill resets before calling)
            watermark.seed_if_absent(source, watermark.now_utc())
            _finish_run(run_id, s, ok=True)
            return s

        base = _as_dt(wm_ts)
        since_dt = base - timedelta(minutes=settings.INGEST_OVERLAP_MIN)
        max_tuple = (base, str(wm_id))
        prev_page_max = None
        pages = 0
        rpm = max(1, settings.BACKFILL_ROWS_PER_MIN)

        while pages < settings.INGEST_MAX_CYCLE_PAGES and not _stop.is_set():
            t_page = time.time()
            rows = client.fetch_since(_since_str(since_dt), settings.INGEST_BATCH)
            if not rows:
                break
            s["rows_seen"] += len(rows)

            items, page_max = [], None
            for raw in rows:
                cts, cid = mapping.cursor_of(raw)
                try:
                    cdt = _as_dt(cts) if cts is not None else base
                except Exception:
                    cdt = base
                tup = (cdt, str(cid))
                if page_max is None or tup > page_max:
                    page_max = tup

                prow, lead_id, reason = mapping.map_row(raw)
                if reason:
                    _quarantine(source, lead_id, raw, reason)
                    s["quarantined"] += 1
                    continue
                img = jobs._resolve_image(prow, "payment_document", "")
                ref, ok, _why = images.prefetch(img)
                if ok:
                    s["images_prefetched"] += 1
                elif settings.IMAGE_PREFETCH and img and img.lower().startswith("http"):
                    s["prefetch_failed"] += 1          # a real prefetch attempt that failed
                items.append({"lead_id": lead_id, "lender": prow.get("institute_name", ""),
                              "image_ref": ref, "row": prow, "priority": 5 if mode == "live" else 8})

            if items:
                enq, dup = jobs.enqueue_ingested(items, batch_id=f"ingest-{source}-{run_id}")
                s["enqueued"] += enq
                s["duplicates"] += dup

            if page_max and page_max > max_tuple:
                max_tuple = page_max

            if len(rows) < settings.INGEST_BATCH:
                break                                    # last page of the tail
            if prev_page_max is not None and page_max is not None and page_max <= prev_page_max:
                break                                    # no forward progress — resume next cycle
            prev_page_max = page_max
            since_dt = page_max[0]                        # keyset-continue from this page's max
            pages += 1
            if mode == "backfill":                       # rate-cap history so it never starves live
                want = len(rows) * 60.0 / rpm
                time.sleep(max(0.0, want - (time.time() - t_page)))

        # advance the durable cursor ONLY after everything above succeeded
        if max_tuple > (base, str(wm_id)):
            watermark.advance(source, max_tuple[0], max_tuple[1],
                              allow_backward=(mode == "backfill"))
        _finish_run(run_id, s, ok=True)
    except Exception as e:  # noqa: BLE001 — degrade, never corrupt: log + retry next cycle
        s["error"] = f"{type(e).__name__}: {e}"
        _finish_run(run_id, s, ok=False, error=s["error"])
    return s


# ── advisory lock (only one ingester) ─────────────────────────────────────────
def _lock_key(source: str) -> int:
    h = hashlib.sha1(("pv_ingest_" + source).encode()).digest()[:8]
    return int.from_bytes(h, "big", signed=True)


def _acquire_singleton(source: str):
    """Hold a dedicated connection with a session advisory lock for the process lifetime.
    Returns the connection if we won, else None (another ingester owns it)."""
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", (_lock_key(source),)).fetchone()[0]
    if not got:
        conn.close()
        return None
    return conn


# ── run loop + entry ──────────────────────────────────────────────────────────
def _install_signals():
    def _handle(_sig, _frm):
        _stop.set()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _handle)
        except Exception:
            pass


def run(once: bool = False, backfill: bool = False, from_ts: str = None,
        source: str = None) -> None:
    pg.init_schema()
    source = source or settings.SOURCE_NAME
    if not settings.SOURCE_MODE:
        print("[ingester] SOURCE_MODE not set - ingestion is disabled. Nothing to do.")
        return

    mapping = load_mapping(settings.SOURCE_MAPPING_PATH)
    client = build_source(mapping)
    lock = _acquire_singleton(source)
    if lock is None:
        print(f"[ingester] another ingester already owns source '{source}' - exiting.")
        client.close()
        return

    _install_signals()
    mode = "backfill" if backfill else "live"
    if backfill and from_ts:
        watermark.reset(source, _as_dt(from_ts))
        print(f"[ingester] backfill: watermark reset to {from_ts}")

    print(f"[ingester] source='{source}' mode={mode} interval={settings.INGEST_INTERVAL}s "
          f"batch={settings.INGEST_BATCH} prefetch={'on' if settings.IMAGE_PREFETCH else 'off'}")
    try:
        while not _stop.is_set():
            t0 = time.time()
            st = run_cycle(mapping, client, source, mode)
            tag = "ERROR " + st["error"] if st["error"] else "ok"
            print(f"[ingester] cycle: seen={st['rows_seen']} enq={st['enqueued']} "
                  f"dup={st['duplicates']} quarantined={st['quarantined']} "
                  f"img_ok={st['images_prefetched']} img_fail={st['prefetch_failed']} [{tag}]")
            if once:
                break
            if backfill and st["rows_seen"] == 0 and not st["error"]:
                print("[ingester] backfill complete (source tail reached).")
                break
            _stop.wait(max(1.0, settings.INGEST_INTERVAL - (time.time() - t0)))
    finally:
        try:
            lock.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(source),))
            lock.close()
        except Exception:
            pass
        client.close()
        print("[ingester] stopped.")
