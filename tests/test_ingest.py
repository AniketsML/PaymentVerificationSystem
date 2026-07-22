"""
Automated-ingestion regression. Locks the production invariants of the ingester:

  · mapping/validation quarantines bad rows and never feeds them to the pipeline
  · a full cycle fetches from the source, enqueues valid rows, quarantines the rest,
    and advances the watermark
  · re-running is idempotent — the overlap re-read enqueues NOTHING new (exactly-once
    effect) — and quarantine upserts rather than duplicating
  · the blob store is content-addressed (same bytes → one object) and round-trips

DB-backed; skipped automatically if Postgres isn't reachable. Uses a throwaway mock source
table in the SAME Postgres (so no external source DB is needed) and cleans up after itself.
"""
import pytest

try:
    from db import pg
    pg.init_schema()
    with pg.pool().connection() as c:
        c.execute("SELECT 1")
    HAVE_DB = True
except Exception:
    HAVE_DB = False

pytestmark = pytest.mark.skipif(not HAVE_DB, reason="Postgres not available")

if HAVE_DB:
    from datetime import datetime, timezone
    from config import settings
    from ingest.mapping import Mapping
    from ingest.source import DBSourceClient
    from ingest import ingester, watermark, blobstore

MOCK_TABLE = "mock_source_leads_test"
SOURCE = "test_src"
PREFIX = "INGTEST-"

_MAP = {
    "table": MOCK_TABLE,
    "cursor": {"created_at_column": "created_at", "id_column": "id"},
    "lead_id": {"column": "id", "prefix": PREFIX},
    "fields": {
        "institute_name": "lender_name",
        "lead_code": "lead_code",
        "loan_account_number": "loan_account_number",
        "payment_amount": "amount",
        "payment_date": "payment_date",
        "image": "payment_document_url",
    },
    "required": ["institute_name", "payment_amount", "payment_date"],
    "validate": {"amount_numeric": True, "date_parseable": True},
    "extra_columns_passthrough": True,
}


def _sqlalchemy_url() -> str:
    # Deliberately return the RAW postgresql:// URL so DBSourceClient._normalize_url is
    # exercised — SQLAlchemy would otherwise default to the (uninstalled) psycopg2 driver.
    # This test is the regression guard for that production bug.
    return settings.DATABASE_URL


def _seed_source():
    with pg.pool().connection() as c:
        c.execute(f"DROP TABLE IF EXISTS {MOCK_TABLE}")
        c.execute(f"""CREATE TABLE {MOCK_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            lender_name TEXT, lead_code TEXT, loan_account_number TEXT,
            amount TEXT, payment_date TEXT, utr TEXT, payment_document_url TEXT)""")
        rows = [
            ("SMFG_PL", "LC1", "LN1", "5000", "2025-10-10", "U1", "http://x/y1.jpg"),   # valid
            ("SMFG_PL", "LC2", "LN2", "6000", "2025-10-11", "U2", "http://x/y2.jpg"),   # valid
            ("SMFG_PL", "LC3", "LN3", "abc",  "2025-10-12", "U3", "http://x/y3.jpg"),   # bad amount
            ("SMFG_PL", "LC4", "LN4", "7000", "not-a-date", "U4", "http://x/y4.jpg"),   # bad date
            ("SMFG_PL", "LC5", "LN5", "",     "2025-10-13", "U5", "http://x/y5.jpg"),   # missing amount
        ]
        for r in rows:
            c.execute(f"INSERT INTO {MOCK_TABLE}"
                      "(lender_name,lead_code,loan_account_number,amount,payment_date,utr,payment_document_url)"
                      " VALUES(%s,%s,%s,%s,%s,%s,%s)", r)


@pytest.fixture(autouse=True)
def _cleanup():
    # prefetch OFF so the cycle never hits the network — the URL passes through as the ref
    prev = settings.IMAGE_PREFETCH
    settings.IMAGE_PREFETCH = False
    yield
    settings.IMAGE_PREFETCH = prev
    with pg.pool().connection() as c:
        c.execute(f"DROP TABLE IF EXISTS {MOCK_TABLE}")
        c.execute("DELETE FROM jobs               WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM lead_events        WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM lead_results       WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM ingest_quarantine  WHERE source=%s", (SOURCE,))
        c.execute("DELETE FROM ingest_runs        WHERE source=%s", (SOURCE,))
        c.execute("DELETE FROM ingest_watermarks  WHERE source=%s", (SOURCE,))


# ── pure mapping/validation (no source DB) ────────────────────────────────────
def test_mapping_maps_valid_row():
    m = Mapping(_MAP)
    row, lead_id, reason = m.map_row(
        {"id": 7, "created_at": "2025-10-10", "lender_name": "SMFG_PL",
         "amount": "5000", "payment_date": "2025-10-10", "payment_document_url": "http://x/z.jpg"})
    assert reason is None
    assert lead_id == "INGTEST-7"
    assert row["institute_name"] == "SMFG_PL"
    assert row["payment_document"] == "http://x/z.jpg"   # image → the resolver's column


@pytest.mark.parametrize("patch,frag", [
    ({"amount": "xyz"}, "not numeric"),
    ({"payment_date": "nope"}, "not parseable"),
    ({"amount": ""}, "missing required"),
    ({"id": ""}, "missing lead id"),
])
def test_mapping_quarantine_reasons(patch, frag):
    m = Mapping(_MAP)
    base = {"id": 1, "created_at": "2025-10-10", "lender_name": "SMFG_PL",
            "amount": "5000", "payment_date": "2025-10-10", "payment_document_url": "u"}
    base.update(patch)
    row, _lead, reason = m.map_row(base)
    assert row is None and reason and frag in reason


# ── content-addressed blob store ──────────────────────────────────────────────
def test_blobstore_roundtrip_and_dedup():
    st = blobstore.store()
    sha1 = st.put(b"hello-image-bytes", content_type="image/jpeg", source_url="http://x")
    sha2 = st.put(b"hello-image-bytes")                 # same content → same key
    assert sha1 == sha2
    assert st.get(blobstore.BLOB_PREFIX + sha1) == b"hello-image-bytes"
    assert blobstore.is_blob_ref(blobstore.BLOB_PREFIX + sha1)
    with pg.pool().connection() as c:
        c.execute("DELETE FROM image_blobs WHERE sha256=%s", (sha1,))


# ── full cycle end-to-end against the mock source ─────────────────────────────
def _client():
    return DBSourceClient(Mapping(_MAP), url=_sqlalchemy_url())


def _jobs_count():
    with pg.pool().connection() as c:
        return c.execute("SELECT COUNT(*) n FROM jobs WHERE lead_id LIKE %s",
                         (PREFIX + "%",)).fetchone()["n"]


def _quarantine_count():
    with pg.pool().connection() as c:
        return c.execute("SELECT COUNT(*) n FROM ingest_quarantine WHERE source=%s",
                         (SOURCE,)).fetchone()["n"]


def test_full_cycle_enqueues_valid_quarantines_bad_and_is_idempotent():
    _seed_source()
    # rewind the watermark to the distant past so the cycle reads the existing rows
    watermark.reset(SOURCE, datetime(2000, 1, 1, tzinfo=timezone.utc))
    client = _client()
    try:
        s1 = ingester.run_cycle(Mapping(_MAP), client, SOURCE, mode="live")
        assert s1["rows_seen"] == 5
        assert s1["enqueued"] == 2            # the two valid rows
        assert s1["quarantined"] == 3         # bad amount, bad date, missing amount
        assert s1["error"] is None
        assert _jobs_count() == 2
        assert _quarantine_count() == 3

        # a queued job carries the passed-through image URL (prefetch off) and real-data flag
        with pg.pool().connection() as c:
            j = c.execute("SELECT image_url, is_test, priority FROM jobs WHERE lead_id=%s",
                          (PREFIX + "1",)).fetchone()
        assert j["image_url"] == "http://x/y1.jpg" and j["is_test"] is False and j["priority"] == 5

        # second cycle: the overlap re-reads everything but enqueues NOTHING new
        s2 = ingester.run_cycle(Mapping(_MAP), client, SOURCE, mode="live")
        assert s2["enqueued"] == 0
        assert s2["duplicates"] == 2          # the two valid rows, idempotently skipped
        assert _jobs_count() == 2             # unchanged
        assert _quarantine_count() == 3       # upserted, not duplicated
    finally:
        client.close()


def test_source_down_does_not_advance_watermark():
    _seed_source()
    watermark.reset(SOURCE, datetime(2000, 1, 1, tzinfo=timezone.utc))

    class Broken:
        def fetch_since(self, since, limit):
            raise RuntimeError("source unreachable")
        def close(self): pass

    before = watermark.read(SOURCE)
    s = ingester.run_cycle(Mapping(_MAP), Broken(), SOURCE, mode="live")
    after = watermark.read(SOURCE)
    assert s["error"] and "source unreachable" in s["error"]
    assert before == after                    # degrade, never corrupt: cursor unchanged
    assert _jobs_count() == 0
