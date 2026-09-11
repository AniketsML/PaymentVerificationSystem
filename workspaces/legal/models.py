"""
Data models for the SARFAESI Legal Lead Document Processing workspace.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ExtractionSource(str, Enum):
    """Stage 4 provenance: how a field value was obtained."""
    OCR_ONLY = "ocr_only"                     # Stage 2A high-confidence printed
    VLM_ONLY = "vlm_only"                     # VLM extracted, no OCR comparison
    OCR_VLM_AGREED = "ocr_vlm_agreed"         # Stage 3 dual-extraction match
    OCR_VLM_MISMATCH = "ocr_vlm_mismatch"     # Stage 3 dual-extraction disagreement
    TROCR_ONLY = "trocr_only"                 # TrOCR handwritten, pre-VLM
    REGEX_DETERMINISTIC = "regex_deterministic" # Regex-based extraction from OCR text


class PageType(str, Enum):
    """Stage 1 triage classification."""
    PRINTED = "printed"
    HANDWRITTEN = "handwritten"
    BLANK = "blank"
    MIXED = "mixed"


class MergeConfidence(int, Enum):
    """Stage 5 merge priority (lower = higher priority)."""
    OCR_VLM_AGREED = 1
    SINGLE_HIGH_CONF = 2
    VLM_RESOLVED_MISMATCH = 3
    OCR_ONLY_LOW_CONF = 4


@dataclass
class FieldCandidate:
    """A single extraction candidate for a field, with full provenance."""
    value: Any = None
    source: str = ""            # ExtractionSource value
    confidence: float = 0.0
    role_tag: str = ""          # "borrower", "co_borrower", "guarantor", etc.
    document_type: str = ""
    document_id: str = ""
    page_number: int = 0
    model_used: str = ""
    ocr_value: Any = None       # Original OCR value (for mismatch display)
    vlm_value: Any = None       # Original VLM value (for mismatch display)
    mismatch_flag: bool = False  # True if dual-source-mismatch detected
    merge_priority: int = 4     # MergeConfidence value


@dataclass
class ValidationFlag:
    """Stage 6 validation flag for cross-field inconsistencies."""
    field_name: str = ""
    flag_type: str = ""         # "inconsistency", "missing", "format_error"
    message: str = ""
    severity: str = "warning"   # "warning", "error"
    related_fields: List[str] = field(default_factory=list)


class DocumentType(str, Enum):
    SANCTION_LETTER = "sanction_letter"
    FORECLOSURE_NOTICE = "foreclosure_notice"
    MODT = "modt"
    LEGAL_REPORT = "legal_report"
    COLLATERAL_DEED = "collateral_deed"
    LOAN_AGREEMENT = "loan_agreement"
    IDENTITY_DOCUMENT = "identity_document"
    VALUATION_REPORT = "valuation_report"
    BANK_STATEMENT = "bank_statement"
    OTHER = "other"


class PropertyTier(int, Enum):
    TIER1_MODT = 1
    TIER2_LEGAL_REPORT = 2
    TIER3_COLLATERAL = 3


class OCRRoute(str, Enum):
    DIGITAL_NATIVE = "digital_native"
    RAPIDOCR = "rapidocr"
    BAIDU_OCR = "baidu_ocr"
    BAIDU_CLOUD = "baidu_cloud"
    MEDHA_VLM = "medha_vlm"
    GEMINI_FALLBACK = "gemini_fallback"


class Phase(str, Enum):
    PHASE0_DIGITAL = "phase0_digital"
    PHASE1_HIGHYIELD = "phase1_highyield"
    PHASE2_PROPERTY = "phase2_property"
    PHASE3_AGREEMENT = "phase3_agreement"


# Map document types to processing priority (lower = first)
DOC_TYPE_PRIORITY = {
    DocumentType.SANCTION_LETTER: 1,
    DocumentType.FORECLOSURE_NOTICE: 2,
    DocumentType.MODT: 3,
    DocumentType.LEGAL_REPORT: 4,
    DocumentType.COLLATERAL_DEED: 5,
    DocumentType.LOAN_AGREEMENT: 6,
    DocumentType.OTHER: 9,
}

# Map document types to property tier
DOC_TYPE_PROPERTY_TIER = {
    DocumentType.MODT: PropertyTier.TIER1_MODT,
    DocumentType.LEGAL_REPORT: PropertyTier.TIER2_LEGAL_REPORT,
    DocumentType.COLLATERAL_DEED: PropertyTier.TIER3_COLLATERAL,
}


@dataclass
class LeadDocument:
    """A single document within a lead's folder."""
    document_id: str = ""
    lead_id: str = ""
    filename: str = ""
    file_path: str = ""
    file_type: str = ""
    document_type: str = ""
    page_count: int = 0
    file_size_bytes: int = 0
    ocr_text: str = ""
    extracted_data: Dict[str, Any] = field(default_factory=dict)
    document_priority: int = 5
    property_tier: Optional[int] = None
    ocr_route: Optional[str] = None
    phase: Optional[str] = None
    processing_status: str = "pending"
    confidence_score: float = 0.0
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SARFAESILeadResult:
    """The complete 31-column SARFAESI extraction result for a lead."""
    lead_id: str = ""
    account_no_lan: str = ""
    property_owner_mortgagor: str = ""
    applicant_name: str = ""
    applicant_address: str = ""
    co_applicant_1: str = ""
    co_applicant_address_1: str = ""
    co_applicant_2: str = ""
    co_applicant_address_2: str = ""
    co_applicant_3: str = ""
    co_applicant_address_3: str = ""
    guarantor_1: str = ""
    guarantor_1_add: str = ""
    guarantor_2: str = ""
    guarantor_2_add: str = ""
    sanction_amount: Optional[float] = None
    sanction_amount_in_words: str = ""
    roi_in_number: str = ""
    sanction_date: str = ""
    disbursal_date: str = ""
    npa_date: str = ""
    future_principal: Optional[float] = None
    principal_overdue: Optional[float] = None
    interest_overdue: Optional[float] = None
    interest_on_termination: Optional[float] = None
    late_payment_penal: Optional[float] = None
    cheque_bounce_inc_gst: Optional[float] = None
    other_charges_inc_gst: Optional[float] = None
    foreclosure_charges: Optional[float] = None
    litigation_charges: Optional[float] = None
    excess_amount: Optional[float] = None
    tos: Optional[float] = None
    # Property verification
    mortgaged_property_detail_1: str = ""
    directions: str = ""
    mortgaged_property_detail_2: str = ""
    directions_2: str = ""
    property_source_doc: str = ""
    property_source_priority: int = 0
    property_cross_verified_with: str = ""
    property_verification_status: str = ""
    property_verification_details: str = ""
    third_party_mortgagor_flag: str = ""
    # Metadata
    processing_status: str = "pending"
    confidence_score: float = 0.0
    summary: str = ""
    flags: List[str] = field(default_factory=list)
    custom_fields: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # Column headers for Excel output
    EXCEL_COLUMNS = [
        "account_no./ LAN", "property_owner/mortgagor", "Applicant Name",
        "applicant_address", "Co_Applicant_1", "Co_Applicant_Address_1",
        "Co_Applicant_2", "Co_Applicant_Address_2", "Co_Applicant_3",
        "Co_Applicant_Address_3", "guarantor_1", "guarantor_1_add",
        "guarantor_2", "guarantor_2_add", "sanction_amount",
        "sanction_amount_in_words", "roi_in_number", "sanction_date",
        "disbursal_date", "npa_date", "Future Principal",
        "Principal Overdue", "Interest Overdue", "Interest on Termination",
        "Late Payment Penal", "Cheque Bounce (Inc. GST)",
        "Other Charges (Inc. GST)", "Foreclosure Charges",
        "Litigation Charges", "Excess Amount", "TOS",
    ]

    EXCEL_FIELD_MAP = [
        "account_no_lan", "property_owner_mortgagor", "applicant_name",
        "applicant_address", "co_applicant_1", "co_applicant_address_1",
        "co_applicant_2", "co_applicant_address_2", "co_applicant_3",
        "co_applicant_address_3", "guarantor_1", "guarantor_1_add",
        "guarantor_2", "guarantor_2_add", "sanction_amount",
        "sanction_amount_in_words", "roi_in_number", "sanction_date",
        "disbursal_date", "npa_date", "future_principal",
        "principal_overdue", "interest_overdue", "interest_on_termination",
        "late_payment_penal", "cheque_bounce_inc_gst",
        "other_charges_inc_gst", "foreclosure_charges",
        "litigation_charges", "excess_amount", "tos",
    ]

    CURRENCY_COLUMNS = {14, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30}
