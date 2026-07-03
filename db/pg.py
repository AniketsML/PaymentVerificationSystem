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

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from config import settings

# opened lazily so importing this module never requires a live DB
_pool: ConnectionPool | None = None


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
"""


def init_schema() -> None:
    with pool().connection() as c:
        c.execute(SCHEMA)


def truncate_all() -> None:
    """Wipe every table - used to reset for a fresh run."""
    with pool().connection() as c:
        c.execute("TRUNCATE lead_events, lead_results, processed_payments, jobs")
