"""
STAGE 2 - OCR + document classification + payment-method labelling.

Runs OCR on the (already quality-passed) image, then deterministically decides:
  - is this a VALID payment document, or a NON-document (and what it shows)?
  - which payment method/app produced it (Google Pay, PhonePe, ... or Other)?
"""
from __future__ import annotations

from config import settings

# receipt must show at least one of these to count as a payment proof
_PAYMENT_MARKERS = (
    "paid", "successful", "payment receipt", "amount paid", "txn", "utr", "rrn",
    "reference", "receipt no", "receipt number", "debited", "credited", "transaction id",
    "paid to", "payment of", "amount", "rs.", "inr", "₹",
    # loan closure / No-Dues proofs (valid payment proof even without an amount)
    "no dues", "no due", "no objection", "noc", "fully paid", "loan closed",
    "loan closure", "account closed", "closure", "foreclosure", "settled",
    "no outstanding", "outstanding is nil", "cleared", "fully repaid",
)


def classify_payment_method(document_type: str, full_text: str) -> str:
    dt = (document_type or "").lower()
    txt = (full_text or "").lower()
    # doc_type is the strongest signal
    if "phonepe" in dt: return "PhonePe"
    if "gpay" in dt or "google" in dt: return "Google Pay"
    if "paytm" in dt: return "Paytm"
    if "cash" in dt: return "Cash Receipt"
    if "neft" in dt or "imps" in dt or "rtgs" in dt: return "Bank Transfer (NEFT/IMPS/RTGS)"
    if "cheque" in dt: return "Cheque"
    # otherwise scan text against the keyword map
    for label, keys in settings.PAYMENT_METHOD_KEYWORDS:
        if any(k in txt for k in keys):
            return label
    return "Other"


def looks_like_payment(extraction: dict) -> bool:
    if not extraction.get("is_payment_document"):
        return False
    txt = (extraction.get("full_text") or "").lower()
    has_marker = any(m in txt for m in _PAYMENT_MARKERS)
    has_value = any(extraction.get(k) for k in ("amount", "reference_id", "loan_account_number"))
    return has_marker or has_value


def run(image, row: dict, ocr_client) -> tuple[bool, str, dict, dict]:
    """
    Runs the vision model on the (already validated) PIL image, then decides if
    this is a valid payment document and which payment method produced it.
    Returns (is_valid_payment_doc, reason, extraction, metrics).
    """
    extraction = ocr_client.extract(image, row)
    method = classify_payment_method(extraction.get("document_type", ""),
                                     extraction.get("full_text", ""))
    extraction["payment_method"] = method

    vmeta = extraction.get("_meta", {}) or {}
    metrics = {
        "document_type": extraction.get("document_type", ""),
        "payment_method": method,
        "text_len": len(extraction.get("full_text") or ""),
        "model": vmeta.get("model", ""),
        "model_ms": vmeta.get("elapsed_ms", ""),
        "model_error": vmeta.get("error", ""),
    }

    if looks_like_payment(extraction):
        return True, "valid payment document", extraction, metrics

    describes = extraction.get("describes") or extraction.get("document_type") or "not a payment receipt"
    return False, f"not a valid payment document ({describes})", extraction, metrics
