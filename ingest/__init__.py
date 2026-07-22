"""
Automated ingestion — pull leads from the source DB behind Metabase, map + validate
them, prefetch their document images while the signed URLs are fresh, and enqueue into
the SAME durable jobs table the CSV upload uses. Everything downstream (pipeline, dedup,
dashboard, Metabase publishing) consumes ingested leads with no change.

Inert until configured: with no SOURCE_MODE / SOURCE_DATABASE_URL, nothing runs.

Entry point:  python ingester.py            (live poll loop)
              python ingester.py --backfill --from 2025-01-01
              python ingester.py --once      (one cycle, for cron/testing)
"""
