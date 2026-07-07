"""
Golden-set regression for the non-document gate (pipeline/ocr_classify.py).

Locks the deterministic 'is this a payment document?' decision: content-driven,
zero false positives into non_document (borderline → unverified, never discarded).
"""
from pipeline.ocr_classify import is_payment_candidate, payment_evidence


def test_keyboard_photo_is_not_a_payment_candidate():
    assert is_payment_candidate({"full_text": "qwerty asdf keyboard laptop"}) is False


def test_empty_extraction_is_not_a_candidate():
    assert is_payment_candidate({"full_text": "", "amount": None}) is False


def test_model_flag_alone_does_not_make_a_candidate():
    # the model's is_payment_document flag is logged but NOT decisive
    ev = {"full_text": "a picture of a wall", "is_payment_document": True}
    assert is_payment_candidate(ev) is False


def test_lone_date_is_not_a_candidate():
    assert is_payment_candidate({"full_text": "calendar 10 october 2025"}) is False


def test_amount_field_makes_a_candidate():
    assert is_payment_candidate({"full_text": "", "amount": "5000"}) is True


def test_reference_field_makes_a_candidate():
    assert is_payment_candidate({"full_text": "", "reference_id": "UTR123456"}) is True


def test_payment_marker_in_text_makes_a_candidate():
    assert is_payment_candidate({"full_text": "Paid Rs 5000 successfully"}) is True


def test_no_dues_certificate_is_a_candidate():
    assert is_payment_candidate({"full_text": "No Dues Certificate loan closed"}) is True


def test_payment_evidence_reports_markers_and_fields():
    ev = payment_evidence({"full_text": "amount paid utr 999", "amount": "500"})
    assert ev["fields"] == ["amount"]
    assert ev["markers"]  # non-empty
