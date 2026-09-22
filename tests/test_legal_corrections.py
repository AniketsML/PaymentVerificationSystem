"""
Regression for manual correction (workspaces/legal/corrections.py) and the orphan-free purge.

DB-backed; skipped automatically if Postgres isn't reachable. Every row it creates is a test
lead under a fixed prefix and is removed afterwards.
"""
import pytest

try:
    from db import pg
    from workspaces.legal.db import init_schema
    init_schema()
    with pg.pool().connection() as c:
        c.execute("SELECT 1")
    HAVE_DB = True
except Exception:
    HAVE_DB = False

pytestmark = pytest.mark.skipif(not HAVE_DB, reason="Postgres not available")

LEAD = "LEAD-CORRTEST-0001"


def _extraction():
    return {
        "borrower_name": "Ravi Kumr",
        "co_borrower_1_name": "Sita Devi",
        "account_no_lan": "HL123",
        "_schema": {"version": "2", "source": "keywords",
                    "fields": [{"key": "borrower_name", "label": "Borrower name", "kind": "name",
                                "group": "borrower", "hint": "", "doc_types": []}],
                    "families": {"co_borrower": ["name"]}},
        "_page_extractions": [{"document_id": "D", "page_number": 1,
                               "fields": {"borrower_name": "Ravi Kumr", "co_borrower_1_name": "Sita Devi"},
                               "field_sources": {}, "field_evidence": {
                                   "borrower_name": {"script": "handwritten", "legibility": "partial"}}}],
    }


@pytest.fixture(autouse=True)
def lead():
    from psycopg.types.json import Jsonb
    from workspaces.legal.db import _delete_leads
    with pg.pool().connection() as c:
        _delete_leads(c, [LEAD])
        c.execute("INSERT INTO legal_leads(lead_id, batch_id, folder_name, status, is_test, extracted_data) "
                  "VALUES (%s, 'batch-corrtest', 'CORRTEST', 'completed', true, %s)", (LEAD, Jsonb(_extraction())))
        c.execute("INSERT INTO legal_lead_results(lead_id, processing_status, raw_extractions, is_test) "
                  "VALUES (%s, 'completed', %s, true)", (LEAD, Jsonb(_extraction())))
    yield
    with pg.pool().connection() as c:
        _delete_leads(c, [LEAD])


def stored():
    with pg.pool().connection() as c:
        return c.execute("SELECT extracted_data FROM legal_leads WHERE lead_id=%s", (LEAD,)).fetchone()["extracted_data"]


def test_a_correction_reaches_the_stored_value_and_keeps_what_the_model_read():
    from workspaces.legal.corrections import record
    res = record(LEAD, "borrower_name", "corrected", "Ravi Kumar", expected="Ravi Kumr", reviewer="asha")
    assert res["value"] == "Ravi Kumar" and res["trust"]["state"] == "corrected"
    ed = stored()
    assert ed["borrower_name"] == "Ravi Kumar"
    assert ed["_model_values"]["borrower_name"] == "Ravi Kumr"
    assert ed["_trust"]["fields"]["borrower_name"]["state"] == "corrected"


def test_the_dashboard_shows_the_correction():
    from workspaces.legal.corrections import record
    from workspaces.legal.logger import PgLegalLeadLogger
    record(LEAD, "borrower_name", "corrected", "Ravi Kumar", expected="Ravi Kumr")
    row = next(r for r in PgLegalLeadLogger().query_leads(scope="test", batch_id="batch-corrtest"))
    assert row["borrower_name"] == "Ravi Kumar"


def test_a_cleared_value_is_not_refilled_from_the_page_behind_it():
    from workspaces.legal.corrections import record
    from workspaces.legal.logger import PgLegalLeadLogger
    record(LEAD, "co_borrower_1_name", "corrected", "", expected="Sita Devi")
    row = next(r for r in PgLegalLeadLogger().query_leads(scope="test", batch_id="batch-corrtest"))
    assert "co_borrower_1_name" not in row           # the page record still says Sita Devi


def test_a_stale_save_is_refused():
    from workspaces.legal.corrections import CorrectionError, record
    record(LEAD, "borrower_name", "corrected", "Ravi Kumar", expected="Ravi Kumr", reviewer="asha")
    with pytest.raises(CorrectionError) as e:          # a colleague still looking at the old value
        record(LEAD, "borrower_name", "corrected", "R. Kumar", expected="Ravi Kumr", reviewer="vik")
    assert e.value.status == 409
    assert stored()["borrower_name"] == "Ravi Kumar"


def test_revert_restores_what_the_model_read():
    from workspaces.legal.corrections import latest, record
    record(LEAD, "borrower_name", "corrected", "Ravi Kumar", expected="Ravi Kumr")
    record(LEAD, "borrower_name", "reverted")
    ed = stored()
    assert ed["borrower_name"] == "Ravi Kumr" and "borrower_name" not in ed.get("_overrides", {})
    assert "borrower_name" not in latest(LEAD)


def test_confirming_settles_without_changing_the_value():
    from workspaces.legal.corrections import record
    res = record(LEAD, "borrower_name", "confirmed", expected="Ravi Kumr")
    assert res["value"] == "Ravi Kumr" and res["trust"]["state"] == "confirmed"


def test_bookkeeping_and_unknown_fields_cannot_be_edited():
    from workspaces.legal.corrections import CorrectionError, record
    for field in ("_trust", "field_scripts_borrower_name", "sanction_amount"):
        with pytest.raises(CorrectionError):
            record(LEAD, field, "corrected", "x")


def test_corrections_survive_a_re_run():
    from workspaces.legal.corrections import apply_to, latest, record
    record(LEAD, "borrower_name", "corrected", "Ravi Kumar", expected="Ravi Kumr")
    fresh = _extraction()                              # what a re-run would produce
    fresh["borrower_name"] = "Ravi Kumaar"
    apply_to(fresh, latest(LEAD))
    assert fresh["borrower_name"] == "Ravi Kumar"
    assert fresh["_model_values"]["borrower_name"] == "Ravi Kumaar"   # "the model now reads …"


def test_a_confirmation_of_a_value_that_changed_does_not_carry_over():
    from workspaces.legal.corrections import apply_to, latest, record
    record(LEAD, "borrower_name", "confirmed", expected="Ravi Kumr")
    fresh = _extraction()
    fresh["borrower_name"] = "Something Else"
    standing = latest(LEAD)
    apply_to(fresh, standing)
    assert "borrower_name" not in standing


def test_purging_a_lead_leaves_no_orphans():
    from workspaces.legal.corrections import record
    from workspaces.legal.db import _LEAD_TABLES, clear_legal_test_data
    record(LEAD, "borrower_name", "corrected", "Ravi Kumar", expected="Ravi Kumr")
    clear_legal_test_data("batch-corrtest")
    with pg.pool().connection() as c:
        for table in _LEAD_TABLES + ("legal_leads",):
            n = c.execute(f"SELECT count(*) n FROM {table} WHERE lead_id=%s", (LEAD,)).fetchone()["n"]
            assert n == 0, table


def test_the_review_metrics_count_what_people_checked():
    from workspaces.legal.corrections import record
    from workspaces.legal.metrics import Slice, review
    record(LEAD, "borrower_name", "corrected", "Ravi Kumar", expected="Ravi Kumr")
    record(LEAD, "co_borrower_1_name", "confirmed", expected="Sita Devi")
    rv = review(Slice("test", "batch-corrtest", None))
    assert rv["corrections"]["checked"] == 2 and rv["corrections"]["agreement"] == 50.0
    fields = {f["field"]: f for f in rv["corrections"]["fields"]}
    assert fields["co_borrower_n_name"]["confirmed"] == 1          # co-borrowers counted as one field
    assert rv["states"] == {"reviewed": 1}
