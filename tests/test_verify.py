"""
Golden-set regression for deterministic verification (pipeline/verify.py).

These lock the verdict logic so a future edit to receiver matching, tolerances or
the direction guard cannot silently flip results. They are pure/deterministic — no
model, no DB. Run:  uv run --with-requirements requirements.txt --with pytest pytest -q
"""
from pipeline.models import ExtractedDocument
from pipeline.verify import (
    LENDER_RECEIVERS, check_receiver, looks_incoming_credit, run,
)

# a lender code that is NOT in config → falls back to the lender's OWN name, so these
# cases are independent of the live allowlist file.
NOAL = "ZZTEST_CORP"      # self-names: "zztest corp", "zztest"


def doc(receiver=None, text="", amount="5000", date="2025-10-10", lan=None):
    return ExtractedDocument(receiver_name=receiver, raw_text=text, amount=amount,
                             date=date, loan_account_number=lan)


def row(lender=NOAL, amount="5000", date="2025-10-10", lan=None):
    return {"institute_name": lender, "payment_amount": amount,
            "payment_date": date, "loan_account_number": lan}


# ── receiver check: the zero-false-positive core ──────────────────────────────
def test_receiver_field_matches_lender_name_passes():
    ok, _ = check_receiver("ZZTEST Corp", "", NOAL)
    assert ok is True


def test_receiver_field_is_substring_of_lender_name_passes():
    # 'ZZTEST' is a substring of the lender self-name 'ZZTEST CORP' (the TVS-Credit case
    # that a 3-char-token drop used to wrongly reject).
    ok, _ = check_receiver("ZZTEST", "", NOAL)
    assert ok is True


def test_populated_receiver_of_different_party_fails_closed():
    # THE false-positive fix: a readable payee that is someone else must NOT verify,
    # even if the lender name appears elsewhere in the OCR text.
    ok, msg = check_receiver("SpeedoLoan", "paid to speedoloan ... zztest corp helpline", NOAL)
    assert ok is False
    assert "SpeedoLoan" in msg


def test_empty_receiver_with_lender_name_in_text_passes():
    ok, _ = check_receiver("", "beneficiary: ZZTEST CORP  amount 5000", NOAL)
    assert ok is True


def test_empty_receiver_without_name_fails():
    ok, _ = check_receiver("", "some unrelated words here", NOAL)
    assert ok is False


def test_text_fallback_requires_whole_word_not_fragment():
    # a generic fragment embedded in another word must not leak a match
    ok, _ = check_receiver("", "myzztestcorporationxyz", NOAL)
    assert ok is False


def test_allowlisted_lender_accepts_listed_name_and_rejects_other():
    lender = next((k for k, v in LENDER_RECEIVERS.items() if v), None)
    assert lender, "expected at least one lender with an allowlist"
    accepted = LENDER_RECEIVERS[lender][0]
    ok, _ = check_receiver(accepted, "", lender)
    assert ok is True
    ok2, _ = check_receiver("Totally Unrelated Person 999", "", lender)
    assert ok2 is False


# ── receiver tiers: lender name -> allowlist -> UPI id (soft, token-anchored) ─
def test_lender_own_name_matches_even_when_allowlist_lacks_it():
    # KHATABOOK's allowlist only has SMFG names; the lender's own name is tier 1
    # and must pass regardless.
    ok, msg = check_receiver("Khatabook", "", "KHATABOOK")
    assert ok is True and "lender name" in msg


def test_soft_match_anchors_on_distinctive_token():
    # 'HDB' is the main element of 'HDB Financial Services Limited' — enough.
    ok, _ = check_receiver("HDB Fin Services", "", "HDBFS_PL")
    assert ok is True


def test_generic_words_alone_never_match():
    # identical generic tail, different main element -> must fail closed.
    ok, _ = check_receiver("Ram Financial Services", "", "HDBFS_PL")
    assert ok is False


def test_soft_match_tolerates_one_typo_in_distinctive_token():
    ok, _ = check_receiver("ZZTst Corp", "", NOAL)          # zztest with a dropped 'e'
    assert ok is True


def test_upi_id_rescues_unknown_payee_name():
    # THE new tier: shop-front payee name, but the UPI handle IS the lender.
    ok, msg = check_receiver("Ram Kirana Store",
                             "paid via UPI to smfgindia@icici ref 12345", "SMFG_PL")
    assert ok is True and "smfgindia@icici" in msg


def test_upi_lookalike_handle_does_not_match():
    # 'janardhan' contains 'jana' but is NOT decomposable into the bank's name.
    ok, _ = check_receiver("Ram Kirana Store",
                           "from janardhan@oksbi paid rs 500", "JANA_SF")
    assert ok is False


def test_email_address_is_not_a_upi_id():
    # a support email on the receipt is not evidence of where the money went
    ok, _ = check_receiver("Ram Kirana Store",
                           "helpdesk care@smfgindiacredit.co.in", "SMFG_PL")
    assert ok is False


def test_full_verify_via_upi_id_end_to_end():
    d = doc(receiver="Ram Kirana Store",
            text="paid to ram kirana store upi smfgindia@icici rs 5000",
            lan="LN00112233")
    status, outcome = run(d, {"institute_name": "SMFG_PL", "payment_amount": "5000",
                              "payment_date": "2025-10-10", "loan_account_number": "LN00112233"})
    assert status == "verified"


# ── direction guard: incoming credit never auto-verifies ──────────────────────
def test_incoming_credit_blocks_verify_even_when_fields_match():
    d = doc(receiver="ZZTEST CORP", text="Rs 5000 credited to your account on 2025-10-10")
    status, outcome = run(d, row())
    assert status == "unverified"
    assert outcome.get("flag") == "incoming_credit"


def test_outgoing_payment_with_matching_fields_verifies():
    d = doc(receiver="ZZTEST CORP", text="paid to zztest corp rs 5000 debited")
    status, _ = run(d, row())
    assert status == "verified"


def test_looks_incoming_credit_detector():
    assert looks_incoming_credit("amount credited to your account") is True
    assert looks_incoming_credit("credited to your account but you paid rs 500") is False  # outgoing overrides
    assert looks_incoming_credit("payment successful debited") is False


# ── amount / date tolerances ──────────────────────────────────────────────────
def test_amount_out_of_tolerance_fails():
    d = doc(receiver="ZZTEST CORP", text="paid to zztest corp", amount="5100")
    status, outcome = run(d, row(amount="5000"))
    assert status == "unverified"
    assert "amount" in outcome.get("failed_fields", [])


def test_date_out_of_tolerance_fails():
    d = doc(receiver="ZZTEST CORP", text="paid to zztest corp", date="2025-10-20")
    status, outcome = run(d, row(date="2025-10-10"))
    assert status == "unverified"
    assert "date" in outcome.get("failed_fields", [])


def test_full_match_verifies():
    d = doc(receiver="ZZTEST CORP", text="paid to zztest corp rs 5000")
    status, _ = run(d, row())
    assert status == "verified"
