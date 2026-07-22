"""
Ingester entry point — the production process that pulls new leads from the source DB and
enqueues them for verification. Run alongside the web + workers.

    python ingester.py                          # live poll loop (default)
    python ingester.py --once                   # a single cycle (cron / smoke test)
    python ingester.py --backfill --from 2025-01-01
    python ingester.py --source secondary       # a named source (own watermark)

Inert until configured: with no SOURCE_MODE / SOURCE_DATABASE_URL it exits immediately.
Only one ingester per source runs at a time (Postgres advisory lock); starting a second is
safe — it detects the lock and exits.
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")        # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ingest import ingester


def main():
    ap = argparse.ArgumentParser(description="Pull leads from the source DB into the queue.")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--backfill", action="store_true", help="rate-capped historical backfill")
    ap.add_argument("--from", dest="from_ts", default=None,
                    help="backfill start (e.g. 2025-01-01); resets the watermark")
    ap.add_argument("--source", default=None, help="logical source name (own watermark)")
    args = ap.parse_args()
    ingester.run(once=args.once, backfill=args.backfill, from_ts=args.from_ts, source=args.source)


if __name__ == "__main__":
    main()
