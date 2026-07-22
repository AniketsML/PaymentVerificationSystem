# Automated Ingestion — Operator Guide

Pull new leads from the production database behind Metabase, verify them hands-free, and
enqueue into the same durable queue the console uses. **Implemented and tested** (commit
after `5006011`). Inert until configured — with no `SOURCE_MODE` the ingester does nothing.

---

## 1. What it does, in one cycle

```
 source DB / Metabase                 this system (Postgres)
 ────────────────────                 ──────────────────────
   new rows since the        ┌─────────────┐   map + validate    ┌──────────────┐
   watermark ───────────────►│  INGESTER   │──── ok ────────────►│  jobs queue  │──► workers
   (created_at, id)          │ (one poll   │       bad            │  (idempotent)│
                             │  cycle)     │──── quarantine ─────►│ ingest_      │
   image URL (fresh) ◄───────┤ prefetch    │                     │ quarantine   │
                             └─────────────┘                     └──────────────┘
                                    │  prefetched bytes
                                    ▼
                             blob store (content-addressed) ── workers load from here
```

Guarantees (the "unbreakable" part):

- **No lead lost / no double effect** — at-least-once fetch + idempotent enqueue
  (`ON CONFLICT (job_id) DO NOTHING`) = exactly-once *effect*. Re-running is always safe.
- **Degrade, never corrupt** — source DB down → the cycle logs the error to `ingest_runs`,
  the watermark does **not** advance, and it retries next interval. Malformed rows are
  quarantined, never fed to the pipeline.
- **One ingester only** — a Postgres advisory lock; a second instance detects it and exits.
- **Graceful shutdown** — SIGTERM finishes the current cycle and exits.
- **Expired-link class killed** — images are fetched at ingest time (while the signed URL
  is fresh) into a content-addressed blob store; workers load from the blob.

---

## 2. Configure

### 2.1 The mapping contract — `config/source_mapping.json`

This is the **only** schema-specific file. Point it at the source table, the cursor
columns, the lead-id column, and map each canonical pipeline field to its source column.
Everything else is generic code. Reviewed like lender config; when the source schema
changes, only this file changes.

```jsonc
{
  "table": "payment_leads",
  "cursor": { "created_at_column": "created_at", "id_column": "id" },
  "lead_id": { "column": "id", "prefix": "LEAD-" },
  "fields": {
    "institute_name": "lender_name",
    "lead_code": "lead_code",
    "loan_account_number": "loan_account_number",
    "payment_amount": "amount",
    "payment_date": "payment_date",
    "image": "payment_document_url"
  },
  "required": ["institute_name", "payment_amount", "payment_date"],
  "validate": { "amount_numeric": true, "date_parseable": true },
  "extra_columns_passthrough": true
}
```

> If rows are **UPDATEd** after creation, point `created_at_column` at an `updated_at`
> column so edits are re-ingested. If the source stores **local** (not UTC) timestamps,
> widen `INGEST_OVERLAP_MIN` to cover the offset (over-reads are free).

### 2.2 Environment (`.env`)

```bash
SOURCE_MODE=db
SOURCE_DATABASE_URL=postgresql+psycopg://pv_reader:pw@source-host:5432/theirdb
# MySQL source:  mysql+pymysql://pv_reader:pw@source-host:3306/theirdb   (pip install pymysql)
INGEST_INTERVAL=120
IMAGE_PREFETCH=1
IMAGE_STORE_PATH=/data/blobs        # mount an encrypted volume in prod
IMAGE_RETENTION_DAYS=90
```

Ask the source DBA for a **read-only** user (`SELECT` on the lead table only), connecting
over TLS from this host's IP. If only Metabase access is granted, set `SOURCE_MODE=metabase`
and `METABASE_URL / METABASE_USER / METABASE_PASS / METABASE_CARD_ID` (the card must be
parameterised on a `since` template tag and order by `(created_at, id)`).

---

## 3. Run

```bash
python ingester.py                          # live poll loop (the production process)
python ingester.py --once                   # a single cycle (cron / smoke test)
python ingester.py --backfill --from 2025-01-01   # rate-capped historical load
python ingester.py --source secondary       # a second named source (its own watermark)
```

In Docker Compose, add a fourth service alongside web/worker/postgres:

```yaml
  ingester:
    build: .
    command: python ingester.py
    env_file: .env
    depends_on: { postgres: { condition: service_healthy } }
    stop_grace_period: 60s
```

**First run is safe:** with no watermark yet, live mode seeds the cursor to *now* and only
picks up NEW leads — it will not accidentally ingest all history. Use `--backfill --from`
to load history deliberately.

---

## 4. Rollout (do not skip shadow mode)

1. **Shadow** — set `SOURCE_MODE=db` and `IMAGE_PREFETCH=1`, but run only
   `python ingester.py --once` and inspect: `SELECT * FROM ingest_runs ORDER BY id DESC`
   and `SELECT reason, count(*) FROM ingest_quarantine GROUP BY reason`. Quarantine ≈ 0 and
   prefetch success high means the mapping is correct. **Nothing is verified yet if you
   leave workers off**, so review the enqueued `jobs` before turning workers on.
2. **Live, capped** — run the loop; keep the CSV upload as a manual override.
3. **Backfill** — `--backfill --from <date>` once the tail is healthy.
4. **Publish** — expose results to Metabase via `analytics.verification_results_v1`
   (see `docs/system-documentation` §11).

---

## 5. Monitor

| Signal | Query / place | Healthy |
|---|---|---|
| Ingestion alive | `SELECT max(started_at) FROM ingest_runs` | recent (< 2× interval) |
| Cycle health | `SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 20` | `ok = true`, `error` null |
| Schema drift | `SELECT reason, count(*) FROM ingest_quarantine WHERE NOT resolved GROUP BY reason` | 0 — a spike = source changed |
| Prefetch health | `ingest_runs.images_prefetched` vs `prefetch_failed` | failures low |
| Watermark | `SELECT * FROM ingest_watermarks` | advancing |
| Blob store size | `SELECT count(*), pg_size_pretty(sum(bytes)) FROM image_blobs` | within volume budget |

**Fixing quarantine after a schema change:** update `source_mapping.json`, then let the
overlap window re-read (or `--backfill --from` the affected window). Corrected rows enqueue;
old quarantine rows can be marked `resolved = true`.

---

## 6. How it stays correct (for reviewers)

- **Watermark** is a `(created_at, id)` cursor advanced only *after* a batch is safely
  enqueued. The fetch uses `created_at >= (watermark − overlap)`; the overlap re-reads are
  absorbed by idempotent enqueue, so a row is never missed regardless of id type.
- **Identifiers** in the mapping are validated as plain identifiers and quoted; all values
  are bound parameters — no SQL injection surface even though the table/columns are config.
- **Blob store** is content-addressed (`sha256`), atomic (temp + rename), and self-healing
  (an evicted blob is re-fetched from its recorded provenance URL on reprocess).
- **Tests**: `tests/test_ingest.py` locks mapping/validation, quarantine, the blob store,
  the full cycle (enqueue valid / quarantine bad), idempotency of the overlap re-read, and
  "source down → watermark unchanged." Run: `pytest tests/test_ingest.py -q`.

---

*Schema: `ingest_watermarks`, `ingest_runs`, `ingest_quarantine`, `image_blobs`, plus
`jobs.image_ref` / `jobs.priority` — all additive, created idempotently on boot. Code:
`ingest/` package + `ingester.py` entry point.*
