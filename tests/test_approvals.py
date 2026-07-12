"""
Receiver-Approval loop regression (the human path for receiver mismatches).

Locks the production invariants: queue groups by (lender, receiver); approve teaches
lender_receivers.json AND deterministically flips receiver-only leads to verified;
reject suppresses the pair permanently; leads never leave `unverified` otherwise.

DB-backed; skipped automatically if Postgres isn't reachable. Uses a throwaway lender
code and restores the config file afterwards.
"""
import json

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
    from psycopg.types.json import Jsonb
    from config import settings
    from pipeline import approvals, verify

LENDER = "APPRTEST_LENDER"
PREFIX = "APPRTEST"


def _seed(lead_id, receiver, failed=("receiver",), status="unverified"):
    """An unverified real lead that failed on the receiver, with everything reverify
    needs (stored extraction + original CSV row)."""
    extracted = {"receiver_name": receiver, "amount": "5000", "date": "2025-10-10",
                 "raw_text": f"paid to {receiver} rs 5000",
                 "is_payment_document": True, "payment_method": "PhonePe"}
    outcome = {"reason": "receiver failed", "failed_fields": list(failed)}
    row = {"institute_name": LENDER, "payment_amount": "5000", "payment_date": "2025-10-10"}
    with pg.pool().connection() as c:
        c.execute("INSERT INTO lead_results(lead_id,lender,verification_status,payment_method,"
                  "outcome,extracted) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (lead_id) DO NOTHING",
                  (lead_id, LENDER, status, "PhonePe", Jsonb(outcome), Jsonb(extracted)))
        c.execute("INSERT INTO jobs(job_id,batch_id,lead_id,lender,row_json) "
                  "VALUES(%s,'appr-test',%s,%s,%s) ON CONFLICT (job_id) DO NOTHING",
                  (lead_id, lead_id, LENDER, Jsonb(row)))


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with pg.pool().connection() as c:
        c.execute("DELETE FROM lead_events  WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM lead_results WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM jobs         WHERE lead_id LIKE %s", (PREFIX + "%",))
        c.execute("DELETE FROM receiver_approvals WHERE lender=%s", (LENDER,))
    # restore the config file (drop the throwaway lender) and reload memory
    with open(settings.LENDER_RECEIVERS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if LENDER in data:
        del data[LENDER]
        with open(settings.LENDER_RECEIVERS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    verify.reload_config()


def _queue_row(receiver="SpeedoTest Finance"):
    return next((g for g in approvals.pending_queue()
                 if g["lender"] == LENDER and g["receiver"] == receiver), None)


def test_queue_groups_by_lender_and_receiver():
    _seed(f"{PREFIX}-G1", "SpeedoTest Finance")
    _seed(f"{PREFIX}-G2", "SpeedoTest Finance")
    _seed(f"{PREFIX}-G3", "Someone Else")
    g = _queue_row()
    assert g and g["count"] == 2 and g["ready"] == 2
    assert set(g["lead_ids"]) == {f"{PREFIX}-G1", f"{PREFIX}-G2"}


def test_approve_teaches_config_and_flips_lead():
    _seed(f"{PREFIX}-A1", "SpeedoTest Finance")
    res = approvals.decide(LENDER, "SpeedoTest Finance", "approved", decided_by="tester")
    assert res.get("error") is None
    assert res["config_added"] is True and res["affected"] == 1 and res["flipped"] == 1
    # config really has it
    assert any(verify._norm_name(n) == verify._norm_name("SpeedoTest Finance")
               for n in verify.LENDER_RECEIVERS.get(LENDER, []))
    # the lead really became verified
    with pg.pool().connection() as c:
        st = c.execute("SELECT verification_status s FROM lead_results WHERE lead_id=%s",
                       (f"{PREFIX}-A1",)).fetchone()["s"]
    assert st == "verified"
    # and the pair left the queue
    assert _queue_row() is None


def test_approve_with_other_failures_does_not_flip():
    _seed(f"{PREFIX}-B1", "PartialCo", failed=("amount", "receiver"))
    # make the stored amount genuinely mismatch so re-verify keeps it unverified
    with pg.pool().connection() as c:
        c.execute("UPDATE lead_results SET extracted = extracted || %s WHERE lead_id=%s",
                  (Jsonb({"amount": "9999"}), f"{PREFIX}-B1"))
    res = approvals.decide(LENDER, "PartialCo", "approved", decided_by="tester")
    assert res["affected"] == 1 and res["flipped"] == 0          # stays unverified, honestly
    with pg.pool().connection() as c:
        st = c.execute("SELECT verification_status s FROM lead_results WHERE lead_id=%s",
                       (f"{PREFIX}-B1",)).fetchone()["s"]
    assert st == "unverified"


def test_reject_suppresses_pair_and_lead_stays_unverified():
    _seed(f"{PREFIX}-R1", "RejectMe Pvt")
    res = approvals.decide(LENDER, "RejectMe Pvt", "rejected", decided_by="tester")
    assert res.get("error") is None
    assert _queue_row("RejectMe Pvt") is None                    # gone from the queue
    with pg.pool().connection() as c:
        st = c.execute("SELECT verification_status s FROM lead_results WHERE lead_id=%s",
                       (f"{PREFIX}-R1",)).fetchone()["s"]
    assert st == "unverified"                                    # untouched
    # and the name was NOT added to config
    assert not any(verify._norm_name(n) == verify._norm_name("RejectMe Pvt")
                   for n in verify.LENDER_RECEIVERS.get(LENDER, []))


def test_double_decision_is_refused():
    _seed(f"{PREFIX}-D1", "OnceOnly Ltd")
    assert approvals.decide(LENDER, "OnceOnly Ltd", "rejected").get("error") is None
    assert "already decided" in approvals.decide(LENDER, "OnceOnly Ltd", "approved")["error"]


def test_empty_receiver_never_enters_queue():
    _seed(f"{PREFIX}-E1", "")
    assert all(not (g["lender"] == LENDER and g["receiver"] == "")
               for g in approvals.pending_queue())
