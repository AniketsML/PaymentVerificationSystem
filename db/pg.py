"""
PostgreSQL connection pool + schema.

One shared connection pool for the whole process (web app AND worker threads).
`init_schema()` is idempotent - safe to call on every startup. Everything the
system persists lives here:

  lead_events   - append-only per-stage event log (with processing time)
  lead_results  - the final row per lead (what the dashboard/Metabase read)
  seen_payments - business duplicate-payment ledger (keyed by payment reference)
  jobs          - the DURABLE work queue (one row per lead, survives restarts)

Point DATABASE_URL at any Postgres (local or managed) - no code change.
"""
from __future__ import annotations

import threading

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from config import settings

# opened lazily so importing this module never requires a live DB
_pool: ConnectionPool | None = None

# schema is set up exactly once per process; the lock + flag stop the 8 worker threads
# from running the (now lock-heavy) migrations concurrently, and a Postgres advisory lock
# serializes it across processes too — otherwise concurrent DDL deadlocks.
_schema_lock = threading.Lock()
_schema_ready = False


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.DATABASE_URL,
            min_size=1,
            max_size=max(4, settings.WORKER_COUNT + 4),
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
    return _pool


SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_events (
    id       BIGSERIAL PRIMARY KEY,
    lead_id  TEXT NOT NULL,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    stage    TEXT NOT NULL,
    status   TEXT NOT NULL,
    reason   TEXT,
    ms       DOUBLE PRECISION,
    metrics  JSONB DEFAULT '{}'::jsonb,
    data     JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON lead_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_stage_ts ON lead_events(stage, ts DESC);

CREATE TABLE IF NOT EXISTS lead_results (
    lead_id             TEXT PRIMARY KEY,
    lender              TEXT,
    verification_status TEXT,
    payment_method      TEXT,
    outcome             JSONB DEFAULT '{}'::jsonb,
    extracted           JSONB DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_results_status ON lead_results(verification_status);
CREATE INDEX IF NOT EXISTS idx_results_updated ON lead_results(updated_at DESC);

-- payment duplicate ledger (identity = lead_code + loan a/c + amount + month)
CREATE TABLE IF NOT EXISTS processed_payments (
    lead_code TEXT    NOT NULL,
    loan_acct TEXT    NOT NULL,     -- normalized (uppercase alphanumeric)
    amount    NUMERIC(14,2) NOT NULL,
    pay_ym    TEXT    NOT NULL,     -- 'YYYY-MM'
    lead_id   TEXT,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lead_code, loan_acct, amount, pay_ym)
);
CREATE INDEX IF NOT EXISTS idx_pp_leadcode ON processed_payments(lead_code);

-- human-review ledger (append-only audit trail of every reviewer action).
-- Closes the review loop AND is the ground truth behind accuracy telemetry:
-- a reviewer either CONFIRMS the system verdict or OVERTURNS it to a corrected one.
CREATE TABLE IF NOT EXISTS lead_reviews (
    id              BIGSERIAL PRIMARY KEY,
    lead_id         TEXT NOT NULL,
    system_status   TEXT,                 -- what the pipeline decided (at review time)
    decision        TEXT NOT NULL,        -- 'confirmed' | 'overturned'
    corrected_status TEXT,                -- reviewer's verdict when overturned (else = system_status)
    reviewer        TEXT,                 -- who reviewed (free text / email for now)
    note            TEXT,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reviews_lead ON lead_reviews(lead_id);
CREATE INDEX IF NOT EXISTS idx_reviews_ts   ON lead_reviews(ts DESC);

-- editable-at-runtime settings (currently the Medha model endpoint, which moves between
-- hosts/ports). Persisted here + shared by the web app and workers, so a change takes
-- effect on the next lead with NO restart. Seeded from env on first read.
CREATE TABLE IF NOT EXISTS runtime_config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- human decisions on receiver names (the Receiver-Approval queue). Keyed by
-- (lender, normalized receiver), NOT by lead — one decision covers every lead that
-- carries that payee, and persists across data wipes: an approval already lives on in
-- lender_receivers.json, a rejection suppresses the pair from ever re-entering the queue.
CREATE TABLE IF NOT EXISTS receiver_approvals (
    id            BIGSERIAL PRIMARY KEY,
    lender        TEXT NOT NULL,
    receiver_norm TEXT NOT NULL,          -- normalized identity for matching
    receiver_name TEXT NOT NULL,          -- as printed on the document (what config gets)
    decision      TEXT NOT NULL,          -- 'approved' | 'rejected'
    decided_by    TEXT,
    note          TEXT,
    affected      INT NOT NULL DEFAULT 0, -- leads re-verified at decision time
    flipped       INT NOT NULL DEFAULT 0, -- of those, how many became verified
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lender, receiver_norm)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,          -- idempotency key (= lead_id)
    batch_id     TEXT NOT NULL,
    lead_id      TEXT NOT NULL,
    lender       TEXT,
    image_url    TEXT,
    row_json     JSONB NOT NULL,
    precomputed  BOOLEAN NOT NULL DEFAULT FALSE,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending|in_progress|done|failed
    attempts     INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    last_error   TEXT,
    lease_until  TIMESTAMPTZ,
    verification_status TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);

-- extraction cache: the vision-model call is the whole bottleneck, and the SAME image
-- (re-runs, resubmissions) shouldn't be paid for twice. Keyed on image content + model
-- + prompt version, so it only ever returns an extraction produced by the current prompt.
CREATE TABLE IF NOT EXISTS ocr_cache (
    cache_key   TEXT PRIMARY KEY,   -- sha256(jpeg bytes) : model : prompt_version
    extraction  JSONB NOT NULL,
    model       TEXT,
    hits        INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# additive migrations for existing databases (idempotent — safe on every boot)
_MIGRATIONS = """
-- drop the orphaned SQLite-era dedup ledger (replaced by processed_payments; dead data)
DROP TABLE IF EXISTS seen_payments;
ALTER TABLE jobs         ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE lead_results ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE lead_events  ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_results_istest ON lead_results(is_test);
CREATE INDEX IF NOT EXISTS idx_events_istest  ON lead_events(is_test);
CREATE INDEX IF NOT EXISTS idx_jobs_istest    ON jobs(is_test);

-- dedup ledger is scope-aware too: test runs dedup against their OWN payments (the Test
-- workspace mirrors duplicate detection) without ever touching the real ledger. Replace
-- the 4-col PK with a unique index that includes is_test so real+test identities coexist.
ALTER TABLE processed_payments ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE processed_payments DROP CONSTRAINT IF EXISTS processed_payments_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pp_identity
    ON processed_payments(lead_code, loan_acct, amount, pay_ym, is_test);

-- reviews are ground truth for ACCURACY — they must be workspace-scoped or sandbox
-- reviews poison the real precision/agreement numbers. Backfill from the lead's flag.
ALTER TABLE lead_reviews ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE lead_reviews rv SET is_test = r.is_test
    FROM lead_results r WHERE r.lead_id = rv.lead_id AND rv.is_test <> r.is_test;
CREATE OR REPLACE VIEW lead_reviews_real AS SELECT * FROM lead_reviews WHERE NOT is_test;
CREATE OR REPLACE VIEW lead_reviews_test AS SELECT * FROM lead_reviews WHERE is_test;

-- unprocessed-image bifurcation: images that never reached OCR (URL missing / private /
-- download failure / QC discard) get their OWN status, so non_document only ever means
-- "the model saw it and it is not a payment document". Idempotent backfill of old rows.
UPDATE lead_results SET verification_status = 'unprocessed'
 WHERE verification_status = 'non_document'
   AND COALESCE(outcome->>'stage_failed','') IN ('load_image','image_qc');

-- Real-data views: the ONE place test-exclusion lives. All real-scope reads (dashboard,
-- observability) query these, so a new query physically cannot leak sandbox rows into
-- real metrics — the guarantee is structural, not "remember to add WHERE NOT is_test".
CREATE OR REPLACE VIEW lead_results_real AS SELECT * FROM lead_results WHERE NOT is_test;
CREATE OR REPLACE VIEW lead_events_real  AS SELECT * FROM lead_events  WHERE NOT is_test;
CREATE OR REPLACE VIEW jobs_real         AS SELECT * FROM jobs         WHERE NOT is_test;
-- test-only mirrors, so the dashboard + observability can be viewed scoped to sandbox data
CREATE OR REPLACE VIEW lead_results_test AS SELECT * FROM lead_results WHERE is_test;
CREATE OR REPLACE VIEW lead_events_test  AS SELECT * FROM lead_events  WHERE is_test;
CREATE OR REPLACE VIEW jobs_test         AS SELECT * FROM jobs         WHERE is_test;

-- ══ AUTOMATED INGESTION (pull leads from the source DB behind Metabase) ══════════
-- All additive + idempotent. The ingester enqueues into the SAME jobs table, so the
-- whole existing pipeline/dedup/dashboard consume ingested leads with no change.

-- image provenance + priority on the durable queue
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS image_ref TEXT;                       -- 'blob:<sha256>' or the original URL
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority  SMALLINT NOT NULL DEFAULT 5; -- lower = sooner; live tail beats backfill
CREATE INDEX IF NOT EXISTS idx_jobs_claim_prio ON jobs(status, priority, created_at);

-- one durable cursor per source. Advanced ONLY after a batch is safely enqueued, so a
-- crash between fetch and enqueue re-reads (idempotent enqueue makes the replay free).
CREATE TABLE IF NOT EXISTS ingest_watermarks (
    source      TEXT PRIMARY KEY,
    wm_ts       TIMESTAMPTZ NOT NULL,
    wm_id       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- one row per poll cycle — the ingestion audit + the "is it stalled?" signal.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id                BIGSERIAL PRIMARY KEY,
    source            TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'live',   -- live | backfill
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    rows_seen         INT NOT NULL DEFAULT 0,
    enqueued          INT NOT NULL DEFAULT 0,
    duplicates        INT NOT NULL DEFAULT 0,          -- already in jobs (idempotent skip)
    quarantined       INT NOT NULL DEFAULT 0,
    images_prefetched INT NOT NULL DEFAULT 0,
    prefetch_failed   INT NOT NULL DEFAULT 0,
    ok                BOOLEAN NOT NULL DEFAULT TRUE,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_started ON ingest_runs(started_at DESC);

-- rows that failed the mapping/validation contract NEVER enter the pipeline — they land
-- here with the raw source row + reason. 0 pending is normal; a spike = source schema drift.
CREATE TABLE IF NOT EXISTS ingest_quarantine (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    source_key  TEXT,                               -- source row id if known (for dedup of quarantine)
    source_row  JSONB NOT NULL,
    reason      TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_quarantine_key
    ON ingest_quarantine(source, source_key) WHERE source_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_quarantine_unresolved ON ingest_quarantine(resolved) WHERE NOT resolved;

-- content-addressed blob-store INDEX (the bytes live on disk / object storage). Fetching
-- the image at ingest time — while the signed URL is still fresh — is what kills the
-- expired-link failure class. sha256 dedupes resubmissions and pairs with the OCR cache.
CREATE TABLE IF NOT EXISTS image_blobs (
    sha256       TEXT PRIMARY KEY,
    bytes        BIGINT NOT NULL,
    content_type TEXT,
    source_url   TEXT,                              -- provenance only; never re-fetched from here
    stored_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_blobs_lastused ON image_blobs(last_used);
"""


def init_schema() -> None:
    """Idempotent, concurrency-safe. Runs the DDL once per process; a threading lock
    (intra-process) plus a Postgres advisory lock (inter-process) serialize it so the
    ALTER/CREATE VIEW statements can't deadlock against each other."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with pool().connection() as c:
            c.execute("SELECT pg_advisory_lock(727274)")     # cross-process serialize
            try:
                c.execute(SCHEMA)
                c.execute(_MIGRATIONS)
            finally:
                c.execute("SELECT pg_advisory_unlock(727274)")
        _schema_ready = True


def truncate_all() -> None:
    """Wipe every table - used to reset for a fresh run."""
    with pool().connection() as c:
        c.execute("TRUNCATE lead_events, lead_results, processed_payments, jobs, lead_reviews")


def clear_test_data(batch_id: str | None = None) -> dict:
    """Delete sandbox/test rows (is_test) — leaves real data and the OCR cache intact.
    Pass a batch_id to purge only that test batch; otherwise all test data."""
    with pool().connection() as c:
        if batch_id:
            leads = [r["lead_id"] for r in c.execute(
                "SELECT lead_id FROM jobs WHERE is_test AND batch_id=%s", (batch_id,)).fetchall()]
            if not leads:
                return {"events": 0, "results": 0, "jobs": 0}
            ev = c.execute("DELETE FROM lead_events  WHERE is_test AND lead_id = ANY(%s)", (leads,)).rowcount
            lr = c.execute("DELETE FROM lead_results WHERE is_test AND lead_id = ANY(%s)", (leads,)).rowcount
            jb = c.execute("DELETE FROM jobs         WHERE is_test AND batch_id=%s", (batch_id,)).rowcount
            c.execute("DELETE FROM processed_payments WHERE is_test AND lead_id = ANY(%s)", (leads,))
            c.execute("DELETE FROM lead_reviews       WHERE is_test AND lead_id = ANY(%s)", (leads,))
        else:
            ev = c.execute("DELETE FROM lead_events  WHERE is_test").rowcount
            lr = c.execute("DELETE FROM lead_results WHERE is_test").rowcount
            jb = c.execute("DELETE FROM jobs         WHERE is_test").rowcount
            c.execute("DELETE FROM processed_payments WHERE is_test")
            c.execute("DELETE FROM lead_reviews       WHERE is_test")
    return {"events": ev, "results": lr, "jobs": jb}


def purge_expired_test_data(days: float) -> dict:
    """Delete test data older than `days` — TTL so sandbox rows don't accumulate forever.
    Called on startup. days<=0 disables."""
    if days is None or days <= 0:
        return {"purged": 0}
    with pool().connection() as c:
        old = [r["lead_id"] for r in c.execute(
            "SELECT lead_id FROM jobs WHERE is_test AND created_at < now() - %s * interval '1 day'",
            (float(days),)).fetchall()]
        if not old:
            return {"purged": 0}
        c.execute("DELETE FROM lead_events       WHERE is_test AND lead_id = ANY(%s)", (old,))
        c.execute("DELETE FROM lead_results      WHERE is_test AND lead_id = ANY(%s)", (old,))
        c.execute("DELETE FROM processed_payments WHERE is_test AND lead_id = ANY(%s)", (old,))
        c.execute("DELETE FROM lead_reviews       WHERE is_test AND lead_id = ANY(%s)", (old,))
        n = c.execute("DELETE FROM jobs WHERE is_test AND lead_id = ANY(%s)", (old,)).rowcount
    return {"purged": n}


def purge_expired_events(days: int) -> int:
    """Retention for the heavy operational log. Deletes lead_events older than `days`;
    verdicts in lead_results are untouched. This is the one table that grows unbounded
    under continuous ingestion. 0 disables (keep forever)."""
    if not days or days <= 0:
        return 0
    with pool().connection() as c:
        return c.execute(
            "DELETE FROM lead_events WHERE ts < now() - %s * interval '1 day'",
            (float(days),)).rowcount


def purge_expired_cache(days: int) -> int:
    """TTL cap on the OCR cache so it doesn't grow forever. Only stale-and-unused entries
    are dropped; anything read recently keeps a fresh created_at is NOT tracked, so this
    uses age since first cached. 0 disables."""
    if not days or days <= 0:
        return 0
    with pool().connection() as c:
        return c.execute(
            "DELETE FROM ocr_cache WHERE created_at < now() - %s * interval '1 day'",
            (float(days),)).rowcount


def sweep_orphan_reviews() -> int:
    """Delete reviews whose lead no longer exists (left behind by wipes/clears that
    predate review-cascading). Orphaned ground truth silently skews accuracy — and worse,
    re-running the same lead_id would attach a STALE review to the new verdict."""
    with pool().connection() as c:
        return c.execute(
            "DELETE FROM lead_reviews rv WHERE NOT EXISTS "
            "(SELECT 1 FROM lead_results r WHERE r.lead_id = rv.lead_id)").rowcount
