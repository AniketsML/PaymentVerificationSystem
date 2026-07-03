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


# fields whose presence is deterministic evidence that this IS a payment document
_EVIDENCE_FIELDS = ("amount", "reference_id", "loan_account_number", "date")


def _present(v) -> bool:
    return v is not None and str(v).strip().lower() not in ("", "null", "none", "nan")


def payment_evidence(extraction: dict) -> dict:
    """The deterministic signals behind the non-document vs payment-candidate call.
    Any single positive signal is enough to treat the image as a payment document
    and route it to verification — a real payment proof is never discarded."""
    txt = (extraction.get("full_text") or "").lower()
    return {
        "model_says_payment": bool(extraction.get("is_payment_document")),
        "markers": [m for m in _PAYMENT_MARKERS if m in txt],
        "fields": [k for k in _EVIDENCE_FIELDS if _present(extraction.get(k))],
    }


def is_payment_candidate(extraction: dict) -> bool:
    """Deterministic gate for the non-document decision.

        True  -> treat as a payment document; verify.py decides verified/unverified.
        False -> genuinely a non-document (safe to discard).

    We return False ONLY when every signal is absent at once: the model does not
    think it is a payment document, AND no payment keyword appears in the OCR text,
    AND no payment field (amount / reference / LAN / date) was extracted. If even
    one signal is positive the lead is routed to verification instead of being
    discarded — so `non_document` carries zero false positives, and everything
    uncertain lands in `unverified` for a human. Reproducible from the extraction."""
    ev = payment_evidence(extraction)
    return ev["model_says_payment"] or bool(ev["markers"]) or bool(ev["fields"])


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

    ev = payment_evidence(extraction)
    vmeta = extraction.get("_meta", {}) or {}
    metrics = {
        "document_type": extraction.get("document_type", ""),
        "payment_method": method,
        "text_len": len(extraction.get("full_text") or ""),
        "model": vmeta.get("model", ""),
        "model_ms": vmeta.get("elapsed_ms", ""),
        "model_error": vmeta.get("error", ""),
        "evidence": ev,
    }

    if is_payment_candidate(extraction):
        why = []
        if ev["model_says_payment"]:
            why.append("model=payment_document")
        if ev["fields"]:
            why.append("fields:" + ",".join(ev["fields"]))
        if ev["markers"]:
            why.append(f"{len(ev['markers'])} text marker(s)")
        return True, "payment document (" + "; ".join(why) + ")", extraction, metrics

    # every signal absent -> genuinely a non-document (deterministic, no evidence)
    describes = extraction.get("describes") or extraction.get("document_type") or "no payment content"
    return False, f"non-document — no payment evidence ({describes})", extraction, metrics
