"""
Data models for the lead-cycle flow. Every lead carries its state and the full
result of each stage, so the pipeline is transparent and debuggable end-to-end.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ── stage outcome constants ───────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"

# ── final verification statuses ───────────────────────────────────────────────
VERIFIED = "verified"
UNVERIFIED = "unverified"
NON_DOCUMENT = "non_document"
MANUAL_REVIEW = "manual_review"   # dedup: different loan a/c under a known lead_code
DUPLICATE = "duplicate"           # dedup: exact re-submission (lead+loan+amount+month)


@dataclass
class StageResult:
    """Result of a single pipeline stage for one lead."""
    stage: str
    status: str                       # PASS | FAIL
    reason: str = ""                  # human-readable reason (esp. on FAIL)
    metrics: dict = field(default_factory=dict)   # numbers behind the decision
    data: dict = field(default_factory=dict)      # any structured output

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractedDocument:
    """The JSON we populate from the image, cross-referenced against the CSV row."""
    is_payment_document: bool = False
    document_type: str = ""           # e.g. upi_screenshot_phonepe, cash_receipt
    payment_method: str = "Other"     # Google Pay | PhonePe | ... | Other
    describes: str = ""               # if not a payment doc: what the image shows

    # extracted fields (from the image)
    loan_account_number: Optional[str] = None
    amount: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    reference_id: Optional[str] = None
    receiver_name: Optional[str] = None
    payer_name: Optional[str] = None

    # every field's origin + confidence, for audit
    field_labels: dict = field(default_factory=dict)
    raw_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationOutcome:
    """The final output object per the required schema."""
    lead_id: str
    verification_status: str          # verified | unverified | non_document
    outcome: dict = field(default_factory=dict)
    payment_method: str = ""
    extracted: dict = field(default_factory=dict)
    stages: list = field(default_factory=list)   # ordered StageResult dicts

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Lead:
    """One payment record flowing through the pipeline."""
    lead_id: str
    lender: str
    image_path: str
    system_record: dict               # the CSV row: loan no, amount, date, txn id...
    # populated as it flows
    extracted: Optional[ExtractedDocument] = None
    result: Optional[VerificationOutcome] = None
