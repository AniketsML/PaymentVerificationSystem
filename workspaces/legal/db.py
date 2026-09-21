"""
Database schema & persistence for the SARFAESI Legal workspace.

Tables:
  - legal_leads: each lead (folder) uploaded for processing
  - legal_lead_documents: individual documents within a lead folder
  - legal_lead_results: 31-column SARFAESI extraction output per lead
  - legal_processing_events: per-stage audit trail
  - legal_reviews: human verification/correction ledger
  - legal_ocr_cache: VLM extraction cache
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from db import pg

_schema_lock = threading.Lock()
_schema_ready = False

LEGAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS legal_leads (
    lead_id             TEXT PRIMARY KEY,
    batch_id            TEXT NOT NULL,
    batch_name          TEXT,
    folder_name         TEXT NOT NULL,
    lead_name           TEXT,
    folder_path         TEXT,
    account_lan         TEXT,
    dossier_type        TEXT,
    total_documents     INT NOT NULL DEFAULT 0,
    processed_documents INT NOT NULL DEFAULT 0,
    failed_documents    INT NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending',
    is_test             BOOLEAN NOT NULL DEFAULT FALSE,
    extraction_prompt   TEXT,
    extracted_data      JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_legal_leads_batch ON legal_leads(batch_id);
CREATE INDEX IF NOT EXISTS idx_legal_leads_status ON legal_leads(status);

-- Auto-migrations
DO $$ 
BEGIN 
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='legal_leads' AND column_name='extraction_prompt') THEN 
        ALTER TABLE legal_leads ADD COLUMN extraction_prompt TEXT; 
    END IF; 
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='legal_leads' AND column_name='extracted_data') THEN 
        ALTER TABLE legal_leads ADD COLUMN extracted_data JSONB; 
    END IF; 
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='legal_leads' AND column_name='batch_name') THEN 
        ALTER TABLE legal_leads ADD COLUMN batch_name TEXT; 
    END IF; 
END $$;

CREATE TABLE IF NOT EXISTS legal_lead_documents (
    document_id         TEXT PRIMARY KEY,
    lead_id             TEXT NOT NULL,
    filename            TEXT NOT NULL,
    file_path           TEXT,
    file_type           TEXT,
    document_type       TEXT,
    page_count          INT DEFAULT 0,
    file_size_bytes     BIGINT DEFAULT 0,
    ocr_text            TEXT,
    extracted_data      JSONB DEFAULT '{}'::jsonb,
    document_priority   SMALLINT DEFAULT 5,
    property_tier       SMALLINT,
    ocr_route           TEXT,
    phase               TEXT,
    processing_status   TEXT NOT NULL DEFAULT 'pending',
    confidence_score    DOUBLE PRECISION DEFAULT 0.0,
    error_message       TEXT,
    metadata            JSONB DEFAULT '{}'::jsonb,
    is_test             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_legal_docs_lead ON legal_lead_documents(lead_id);
CREATE INDEX IF NOT EXISTS idx_legal_docs_status ON legal_lead_documents(processing_status);
CREATE INDEX IF NOT EXISTS idx_legal_docs_type ON legal_lead_documents(document_type);

CREATE TABLE IF NOT EXISTS legal_lead_results (
    lead_id                      TEXT PRIMARY KEY,
    account_no_lan               TEXT,
    property_owner_mortgagor     TEXT,
    applicant_name               TEXT,
    applicant_address            TEXT,
    co_applicant_1               TEXT,
    co_applicant_address_1       TEXT,
    co_applicant_2               TEXT,
    co_applicant_address_2       TEXT,
    co_applicant_3               TEXT,
    co_applicant_address_3       TEXT,
    guarantor_1                  TEXT,
    guarantor_1_add              TEXT,
    guarantor_2                  TEXT,
    guarantor_2_add              TEXT,
    sanction_amount              NUMERIC(14,2),
    sanction_amount_in_words     TEXT,
    roi_in_number                TEXT,
    sanction_date                TEXT,
    disbursal_date               TEXT,
    npa_date                     TEXT,
    future_principal             NUMERIC(14,2),
    principal_overdue            NUMERIC(14,2),
    interest_overdue             NUMERIC(14,2),
    interest_on_termination      NUMERIC(14,2),
    late_payment_penal           NUMERIC(14,2),
    cheque_bounce_inc_gst        NUMERIC(14,2),
    other_charges_inc_gst        NUMERIC(14,2),
    foreclosure_charges          NUMERIC(14,2),
    litigation_charges           NUMERIC(14,2),
    excess_amount                NUMERIC(14,2),
    tos                          NUMERIC(14,2),
    mortgaged_property_detail_1  TEXT,
    directions                   TEXT,
    mortgaged_property_detail_2  TEXT,
    directions_2                 TEXT,
    property_source_doc          TEXT,
    property_source_priority     SMALLINT,
    property_cross_verified_with TEXT,
    property_verification_status TEXT,
    property_verification_details TEXT,
    third_party_mortgagor_flag   TEXT,
    processing_status            TEXT DEFAULT 'pending',
    confidence_score             DOUBLE PRECISION DEFAULT 0.0,
    summary                      TEXT,
    flags                        JSONB DEFAULT '[]'::jsonb,
    raw_extractions              JSONB DEFAULT '{}'::jsonb,
    phase_timings                JSONB DEFAULT '{}'::jsonb,
    ocr_routes_used              JSONB DEFAULT '[]'::jsonb,
    is_test                      BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_legal_results_status ON legal_lead_results(processing_status);
CREATE INDEX IF NOT EXISTS idx_legal_results_updated ON legal_lead_results(updated_at DESC);

CREATE TABLE IF NOT EXISTS legal_processing_events (
    id                  BIGSERIAL PRIMARY KEY,
    lead_id             TEXT NOT NULL,
    document_id         TEXT,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    stage               TEXT NOT NULL,
    status              TEXT NOT NULL,
    reason              TEXT,
    ms                  DOUBLE PRECISION,
    metrics             JSONB DEFAULT '{}'::jsonb,
    data                JSONB DEFAULT '{}'::jsonb,
    is_test             BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_legal_events_lead ON legal_processing_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_legal_events_doc ON legal_processing_events(document_id);
CREATE INDEX IF NOT EXISTS idx_legal_events_stage_ts ON legal_processing_events(stage, ts DESC);

CREATE TABLE IF NOT EXISTS legal_reviews (
    id                  BIGSERIAL PRIMARY KEY,
    lead_id             TEXT NOT NULL,
    document_id         TEXT,
    system_status       TEXT,
    decision            TEXT NOT NULL,
    corrected_data      JSONB DEFAULT '{}'::jsonb,
    reviewer            TEXT,
    note                TEXT,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_test             BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_legal_reviews_lead ON legal_reviews(lead_id);

CREATE TABLE IF NOT EXISTS legal_ocr_cache (
    cache_key   TEXT PRIMARY KEY,
    extraction  JSONB NOT NULL,
    model       TEXT,
    ocr_route   TEXT,
    hits        INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS legal_prompt_history (
    id          SERIAL PRIMARY KEY,
    prompt      TEXT NOT NULL UNIQUE,
    last_used   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- typed / handwritten provenance per extracted value (see workspaces/legal/provenance.py).
-- Derived, never authoritative: `fingerprint` ties a verdict to the extraction it was computed
-- from, so re-extracting a lead invalidates it. Safe to delete at any time; it recomputes.
CREATE TABLE IF NOT EXISTS legal_field_provenance (
    lead_id     TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    tag         TEXT NOT NULL,                    -- handwritten | scanned | typed | unknown
    fields      JSONB NOT NULL DEFAULT '{}'::jsonb,
    counts      JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- the tagging rules that produced this row. Readers ignore rows from older rules, which is
    -- what makes a verdict computed before (say) blur existed get recomputed instead of shown.
    version     TEXT NOT NULL DEFAULT '',
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_legal_provenance_tag ON legal_field_provenance(tag);

-- CSV manifest intake: one row per upload of a link sheet, so the fetch phase is visible
-- (and a link that could not be downloaded is never silently missing).
CREATE TABLE IF NOT EXISTS legal_manifest_runs (
    batch_id    TEXT PRIMARY KEY,
    batch_name  TEXT,
    total_rows  INT NOT NULL DEFAULT 0,
    total_lans  INT NOT NULL DEFAULT 0,
    fetched     INT NOT NULL DEFAULT 0,
    failed      INT NOT NULL DEFAULT 0,
    leads       INT NOT NULL DEFAULT 0,
    failures    JSONB NOT NULL DEFAULT '[]'::jsonb,
    error       TEXT,
    is_test     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
"""


def init_schema() -> None:
    """Idempotently initialize all legal workspace tables and indexes."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with pg.pool().connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(982341203)")
            # Detect old notice-era schema
            col = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='legal_reviews' AND column_name='lead_id'"
            ).fetchone()
            rev_exists = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='legal_reviews'"
            ).fetchone()
            if rev_exists and not col:
                conn.execute(
                    "DROP TABLE IF EXISTS legal_reviews, legal_jobs, "
                    "legal_notice_events, legal_notice_results, "
                    "legal_ocr_cache CASCADE;"
                )
            # Detect old generic lead_results schema (has loan_amount column)
            old_col = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='legal_lead_results' AND column_name='loan_amount'"
            ).fetchone()
            if old_col:
                conn.execute("DROP TABLE IF EXISTS legal_lead_results CASCADE;")
            conn.execute(LEGAL_SCHEMA)

            # Auto-migrate existing tables if created prior to SARFAESI schema additions
            conn.execute("""
                ALTER TABLE legal_leads ADD COLUMN IF NOT EXISTS account_lan TEXT;
                ALTER TABLE legal_leads ADD COLUMN IF NOT EXISTS dossier_type TEXT;
                ALTER TABLE legal_leads ADD COLUMN IF NOT EXISTS extraction_prompt TEXT;

                ALTER TABLE legal_lead_documents ADD COLUMN IF NOT EXISTS document_priority SMALLINT DEFAULT 5;
                ALTER TABLE legal_lead_documents ADD COLUMN IF NOT EXISTS property_tier SMALLINT;
                ALTER TABLE legal_lead_documents ADD COLUMN IF NOT EXISTS ocr_route TEXT;
                ALTER TABLE legal_lead_documents ADD COLUMN IF NOT EXISTS phase TEXT;
                ALTER TABLE legal_lead_documents ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;
                ALTER TABLE legal_lead_documents ADD COLUMN IF NOT EXISTS file_size_bytes BIGINT DEFAULT 0;
                ALTER TABLE legal_lead_documents ADD COLUMN IF NOT EXISTS page_count INT DEFAULT 0;

                ALTER TABLE legal_field_provenance ADD COLUMN IF NOT EXISTS version TEXT NOT NULL DEFAULT '';

                CREATE INDEX IF NOT EXISTS idx_legal_docs_lead_status
                    ON legal_lead_documents(lead_id, processing_status);
            """)

            # Documents left 'pending' under a dossier that has already finished were skipped by
            # the old shortlist and will never be picked up again — recording that is what stops
            # them reading as "still queued" forever. Idempotent, and it never touches a dossier
            # that is still being worked on.
            conn.execute("""
                UPDATE legal_lead_documents d SET processing_status = 'skipped',
                       error_message = COALESCE(NULLIF(d.error_message, ''),
                                                'not read — the dossier finished without it'),
                       updated_at = now()
                  FROM legal_leads l
                 WHERE l.lead_id = d.lead_id
                   AND d.processing_status = 'pending'
                   AND l.status IN ('completed', 'partial', 'missing_documents', 'failed');
            """)
        _schema_ready = True


def purge_expired_legal_test_data(ttl_days: int = 7) -> Dict[str, int]:
    """Purge sandbox/test leads older than TTL."""
    with pg.pool().connection() as c:
        r = c.execute(
            "DELETE FROM legal_leads WHERE is_test=true "
            "AND created_at < now() - make_interval(days => %s) "
            "RETURNING lead_id", (ttl_days,)
        ).fetchall()
        c.execute(
            "DELETE FROM legal_processing_events WHERE is_test=true "
            "AND ts < now() - make_interval(days => %s)", (ttl_days,)
        )
    return {"purged": len(r)}


def clear_legal_test_data(batch_id: Optional[str] = None) -> Dict[str, Any]:
    """Manually clear sandbox/test data for the legal workspace."""
    with pg.pool().connection() as c:
        if batch_id:
            sub = "(SELECT lead_id FROM legal_leads WHERE batch_id=%s AND is_test=true)"
            c.execute(f"DELETE FROM legal_processing_events WHERE is_test=true AND lead_id IN {sub}", (batch_id,))
            c.execute(f"DELETE FROM legal_reviews WHERE is_test=true AND lead_id IN {sub}", (batch_id,))
            c.execute(f"DELETE FROM legal_lead_documents WHERE is_test=true AND lead_id IN {sub}", (batch_id,))
            c.execute(f"DELETE FROM legal_lead_results WHERE lead_id IN {sub}", (batch_id,))
            r = c.execute("DELETE FROM legal_leads WHERE batch_id=%s AND is_test=true RETURNING lead_id", (batch_id,)).fetchall()
        else:
            c.execute("DELETE FROM legal_processing_events WHERE is_test=true")
            c.execute("DELETE FROM legal_reviews WHERE is_test=true")
            c.execute("DELETE FROM legal_lead_documents WHERE is_test=true")
            c.execute("DELETE FROM legal_lead_results WHERE is_test=true")
            r = c.execute("DELETE FROM legal_leads WHERE is_test=true RETURNING lead_id").fetchall()
    return {"cleared": len(r), "scope": "test", "workspace": "legal"}
