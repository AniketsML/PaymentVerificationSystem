"""
STAGE 3 - Build the structured JSON and label each field.

Starts from a DEFAULT schema derived from the CSV row (what we *expect*), then
populates the *extracted* side from the image. Each extracted value is
cross-referenced against the CSV row so we can positively LABEL what a token is
(e.g. a number that equals the CSV loan account number is labelled `loan_account_number`).
"""
from __future__ import annotations

import re

from pipeline.models import ExtractedDocument


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _num(s):
    if s is None:
        return None
    m = re.search(r"[0-9]+(?:\.[0-9]{1,2})?", str(s).replace(",", ""))
    return float(m.group(0)) if m else None


def default_schema_from_row(row: dict) -> dict:
    """The 'expected' JSON built from the CSV - what the document should confirm."""
    return {
        "lender": row.get("institute_name", ""),
        "loan_account_number": row.get("loan_account_number", ""),
        "amount": row.get("payment_amount", ""),
        "date": row.get("payment_date", ""),
        "transaction_id": row.get("transaction_id", ""),
    }


def run(extraction: dict, row: dict) -> ExtractedDocument:
    doc = ExtractedDocument(
        is_payment_document=True,
        document_type=extraction.get("document_type", ""),
        payment_method=extraction.get("payment_method", "Other"),
        loan_account_number=extraction.get("loan_account_number"),
        amount=extraction.get("amount"),
        date=extraction.get("date"),
        time=extraction.get("time"),
        reference_id=extraction.get("reference_id"),
        receiver_name=extraction.get("receiver_name"),
        payer_name=extraction.get("payer_name"),
        raw_text=extraction.get("full_text", ""),
    )

    # ── field labelling: confirm each extracted value against the CSV row ──────
    labels = {}
    sys_lan = _digits(row.get("loan_account_number"))
    doc_lan = _digits(doc.loan_account_number)
    if doc_lan:
        matches = bool(sys_lan) and (doc_lan == sys_lan or sys_lan in doc_lan or doc_lan in sys_lan)
        labels["loan_account_number"] = {
            "value": doc.loan_account_number,
            "label": "loan_account_number",
            "matches_system": matches,
        }

    sys_amt, doc_amt = _num(row.get("payment_amount")), _num(doc.amount)
    if doc_amt is not None:
        labels["amount"] = {
            "value": doc.amount, "label": "amount",
            "matches_system": sys_amt is not None and abs(sys_amt - doc_amt) <= 1.0,
        }

    if doc.date:
        labels["date"] = {"value": doc.date, "label": "date"}
    if doc.reference_id:
        labels["reference_id"] = {"value": doc.reference_id, "label": "reference_id"}
    if doc.receiver_name:
        labels["receiver_name"] = {"value": doc.receiver_name, "label": "receiver_name"}

    doc.field_labels = labels
    return doc
