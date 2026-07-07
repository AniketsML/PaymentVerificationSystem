"""
Sandbox/test-mode isolation regression (the production invariant: test data must NEVER
leak into real dashboard/metrics, and must be born flagged — no race).

DB-backed; skipped automatically if Postgres isn't reachable.
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
    from observability.pg_logger import PgLeadLogger
    from observability import metrics
    from pipeline.orchestrator import process_lead
    from ocr.medha_client import MedhaVisionOCR

PREFIX = "SANDBOXTEST"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with pg.pool().connection() as c:
        c.execute("DELETE FROM lead_events       WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM lead_results      WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM jobs              WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM lead_reviews      WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM processed_payments WHERE lead_code LIKE %s", (PREFIX + "%",))


def test_test_lead_is_born_flagged_no_race():
    # image-less lead -> non_document, no model call. is_test is threaded into EVERY write,
    # so there is no window where any row for this lead is visible as real.
    lg = PgLeadLogger()
    lid = f"{PREFIX}-BORN"
    process_lead(lid, "VARTHANA", "", {"institute_name": "VARTHANA",
                 "payment_amount": "5000", "payment_date": "2025-10-10"},
                 MedhaVisionOCR(), lg, dedup=None, is_test=True)
    with pg.pool().connection() as c:
        r = c.execute("SELECT is_test FROM lead_results WHERE lead_id=%s", (lid,)).fetchone()
        ev = c.execute("SELECT COUNT(*) n, COUNT(*) FILTER (WHERE is_test) t "
                       "FROM lead_events WHERE lead_id=%s", (lid,)).fetchone()
    assert r["is_test"] is True
    assert ev["n"] > 0 and ev["n"] == ev["t"]      # ALL events flagged, none ever real


def test_real_view_excludes_test():
    lg = PgLeadLogger()
    lg.save_result(f"{PREFIX}-R", "VIEWLENDER", "verified", "PhonePe", {}, {}, is_test=False)
    lg.save_result(f"{PREFIX}-T", "VIEWLENDER", "verified", "PhonePe", {}, {}, is_test=True)
    with pg.pool().connection() as c:
        base = c.execute("SELECT COUNT(*) n FROM lead_results WHERE lender='VIEWLENDER'").fetchone()["n"]
        real = c.execute("SELECT COUNT(*) n FROM lead_results_real WHERE lender='VIEWLENDER'").fetchone()["n"]
    assert base == 2 and real == 1


def test_metrics_exclude_test():
    # Use an unbounded aggregate (fill-rate n) so this is robust no matter how much real
    # data is already present: adding 1 real + 1 test lead must raise the REAL metric by
    # exactly 1 (the test lead is invisible to it).
    lg = PgLeadLogger()
    before = (metrics.extraction_fillrates() or {}).get("n", 0)
    lg.save_result(f"{PREFIX}-FR", "SBXLENDER", "verified", "PhonePe", {}, {"amount": "1"}, is_test=False)
    lg.save_result(f"{PREFIX}-FT", "SBXLENDER", "verified", "PhonePe", {}, {"amount": "1"}, is_test=True)
    after = (metrics.extraction_fillrates() or {}).get("n", 0)
    assert after - before == 1

    # and the sandbox lead IS visible in test scope
    test_n = (metrics.snapshot(ttl=0, scope="test") or {}).get("fillrates", {}).get("n", 0)
    assert test_n >= 1


def test_status_counts_scope_and_test_count():
    lg = PgLeadLogger()
    lg.save_result(f"{PREFIX}-SCR", "X", "verified", "m", {}, {}, is_test=False)
    lg.save_result(f"{PREFIX}-SCT", "X", "verified", "m", {}, {}, is_test=True)
    assert lg.test_count() >= 1
    ours_real = [r for r in lg.query_results(scope="real", limit=2000) if r["lead_id"].startswith(PREFIX)]
    ours_test = [r for r in lg.query_results(scope="test", limit=2000) if r["lead_id"].startswith(PREFIX)]
    assert any(r["lead_id"] == f"{PREFIX}-SCR" for r in ours_real)
    assert all(r["lead_id"] != f"{PREFIX}-SCT" for r in ours_real)     # test not in real
    assert any(r["lead_id"] == f"{PREFIX}-SCT" for r in ours_test)


def test_clear_test_data_removes_only_test():
    lg = PgLeadLogger()
    lg.save_result(f"{PREFIX}-KEEP", "X", "verified", "m", {}, {}, is_test=False)
    lg.save_result(f"{PREFIX}-DROP", "X", "verified", "m", {}, {}, is_test=True)
    lg.log(f"{PREFIX}-DROP", "x", "PASS", is_test=True)
    pg.clear_test_data()
    with pg.pool().connection() as c:
        keep = c.execute("SELECT COUNT(*) n FROM lead_results WHERE lead_id=%s", (f"{PREFIX}-KEEP",)).fetchone()["n"]
        drop = c.execute("SELECT COUNT(*) n FROM lead_results WHERE lead_id=%s", (f"{PREFIX}-DROP",)).fetchone()["n"]
    assert keep == 1 and drop == 0


# ── #1 dedup: a retry must never classify itself as a duplicate ───────────────
def test_retry_does_not_self_duplicate():
    from observability.pg_dedup import PaymentDedup
    d = PaymentDedup()
    row = {"lead_code": f"{PREFIX}-LC", "loan_account_number": "SB1", "payment_amount": "5000",
           "payment_date": "2025-10-10"}
    lead = f"{PREFIX}-RETRY"
    v1, _, ident = d.evaluate(row, False, self_lead_id=lead)
    assert v1 == "new"
    d.claim(ident, lead, False)                         # attempt 1 claims the identity
    # attempt 2 (a retry of the SAME lead) must NOT see itself as a duplicate
    v2, _, ident2 = d.evaluate(row, False, self_lead_id=lead)
    assert v2 == "new", f"retry wrongly classified as {v2}"
    assert d.claim(ident2, lead, False) == lead         # re-claim returns itself -> proceed
    with pg.pool().connection() as c:
        c.execute("DELETE FROM processed_payments WHERE lead_code=%s", (f"{PREFIX}-LC",))


def test_atomic_claim_makes_second_lead_the_duplicate():
    from observability.pg_dedup import PaymentDedup
    d = PaymentDedup()
    row = {"lead_code": f"{PREFIX}-LC2", "loan_account_number": "SB2", "payment_amount": "700",
           "payment_date": "2025-10-10"}
    ident = d.identity(row)
    assert d.claim(ident, f"{PREFIX}-A", False) == f"{PREFIX}-A"   # first wins
    assert d.claim(ident, f"{PREFIX}-B", False) == f"{PREFIX}-A"   # second learns the owner
    with pg.pool().connection() as c:
        c.execute("DELETE FROM processed_payments WHERE lead_code=%s", (f"{PREFIX}-LC2",))


# ── #2 reviews: a sandbox review must not count in REAL accuracy ───────────────
def test_test_review_excluded_from_real_accuracy():
    lg = PgLeadLogger()
    before = metrics.review_accuracy()["reviewed"]              # REAL reviewed count
    lg.save_result(f"{PREFIX}-RV", "X", "verified", "m", {}, {}, is_test=True)
    lg.save_review(f"{PREFIX}-RV", "verified", "overturned", corrected_status="unverified",
                   reviewer="audit", is_test=True)
    after = metrics.review_accuracy()["reviewed"]
    assert after == before, "sandbox review leaked into REAL accuracy"   # the core fix
    # but it IS visible in the test workspace's accuracy
    test = metrics.snapshot(ttl=0, scope="test")["accuracy"]
    assert test["reviewed"] >= 1
