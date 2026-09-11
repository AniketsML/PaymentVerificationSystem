"""
Sanitization, normalization & provenance engine for SARFAESI extraction.

Handles entity name cleaning, address reordering, PIN anchoring,
anti-lender-leakage filtering, third-party mortgagor validation,
and provenance gate enforcement.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ── Safe financial regex patterns (bounded, non-greedy, zero catastrophic backtracking) ──

_AMT = r"(?:Rs\.?\s*)?([0-9,]+(?:\.[0-9]{1,2})?)"
_LABEL = r"(?:\s*\([^)\n]{0,30}\))?\s*[:\-]?\s*"

FUTURE_PRINCIPAL_RE = re.compile(
    r"(?:Future\s*Principal)" + _LABEL + _AMT, re.IGNORECASE)
PRINCIPAL_OVERDUE_RE = re.compile(
    r"(?:Principal\s*Overdue|Overdue\s*Principal)" + _LABEL + _AMT, re.IGNORECASE)
INTEREST_OVERDUE_RE = re.compile(
    r"(?:Interest\s*Overdue|Overdue\s*Interest)" + _LABEL + _AMT, re.IGNORECASE)
INTEREST_ON_TERM_RE = re.compile(
    r"(?:Interest\s*on\s*Termination)" + _LABEL + _AMT, re.IGNORECASE)
LATE_PAYMENT_RE = re.compile(
    r"(?:Late\s*Payment\s*Penal|Penal\s*Interest|Penal\s*Charges)" + _LABEL + _AMT, re.IGNORECASE)
CHEQUE_BOUNCE_RE = re.compile(
    r"(?:Cheque\s*Bounce|Bounce\s*Charges|Dishonour\s*Charges|ECS\s*Bounce)"
    + _LABEL + _AMT, re.IGNORECASE)
OTHER_CHARGES_RE = re.compile(
    r"(?:Other\s*Charges|Incidental\s*Charges|Miscellaneous\s*Charges)"
    + _LABEL + _AMT, re.IGNORECASE)
FORECLOSURE_RE = re.compile(
    r"(?:Foreclosure\s*Charges|Pre.?closure\s*Charges)" + _LABEL + _AMT, re.IGNORECASE)
LITIGATION_RE = re.compile(
    r"(?:Litigation\s*Charges|Legal\s*Charges)" + _LABEL + _AMT, re.IGNORECASE)
EXCESS_AMOUNT_RE = re.compile(
    r"(?:Excess\s*Amount|Credit\s*Balance)" + _LABEL + _AMT, re.IGNORECASE)
NET_RECEIVABLE_RE = re.compile(
    r"(?:Net\s*Receivable|Total\s*Due|Total\s*Outstanding)" + _LABEL + _AMT, re.IGNORECASE)
SANCTION_AMOUNT_RE = re.compile(
    r"(?:Sanction(?:ed)?\s*Amount|Loan\s*Amount|Facility\s*Amount)" + _LABEL + _AMT, re.IGNORECASE)
ROI_RE = re.compile(
    r"(?:Rate\s*of\s*Interest|ROI|Interest\s*Rate)\s*[:\-]?\s*([0-9]+\.?[0-9]*)\s*%", re.IGNORECASE)

# ── name noise prefixes and boilerplate ──
_NAME_PREFIXES = re.compile(
    r"^(?:M/s\.?\s*|Mr\.?\s*|Mrs\.?\s*|Ms\.?\s*|Messrs\.?\s*|Smt\.?\s*|Shri\.?\s*|"
    r"Borrower\s*:\s*|Co[\s-]*Borrower\s*:\s*|Guarantor\s*:\s*|Applicant\s*:\s*)",
    re.IGNORECASE,
)
_NAME_BOILERPLATE = re.compile(
    r"(?:Authorised\s*Signatory|Authorized\s*Signatory|Proprietor|Director|"
    r"Managing\s*Director|Partner|Date\s*of\s*Agreement|"
    r"Ugro\s*Capital\s*Limited|Equinox\s*Business\s*Park)",
    re.IGNORECASE,
)

# ── lender HQ address fragments (anti-leakage) ──
_LENDER_HQ_FRAGMENTS = [
    "equinox business park", "kurla (west)", "kurla west", "lbs road",
    "lbs marg", "mumbai 400070", "mumbai-400070", "ugro capital",
    "registered office", "corporate office", "head office",
]

# ── Indian PIN code ──
_PIN_RE = re.compile(r"\b([1-9]\d{5})\b")

# ── address trailing metadata ──
_ADDR_TRAIL = re.compile(
    r"(?:Branch\s*:|Name\s*of\s*Guarantor|Mobile\s*No\.?|PAN\s*:|CIN\s*:|"
    r"Email\s*:|Contact\s*:|Phone\s*:|Fax\s*:).*$",
    re.IGNORECASE,
)


def sanitize_name(raw: str) -> str:
    """Clean entity name: strip prefixes, boilerplate, title-case."""
    if not raw or not raw.strip():
        return ""
    s = raw.strip()
    s = _NAME_PREFIXES.sub("", s).strip()
    if _NAME_BOILERPLATE.search(s):
        cleaned = _NAME_BOILERPLATE.sub("", s).strip()
        if len(cleaned) < 3:
            return ""
        s = cleaned
    s = re.sub(r"\s{2,}", " ", s).strip()
    if len(s) < 2:
        return ""
    return s.title() if s == s.upper() or s == s.lower() else s


def sanitize_address(raw: str) -> str:
    """Clean address: strip lender HQ, reorder, anchor on PIN code."""
    if not raw or not raw.strip():
        return ""
    s = raw.strip()
    lower = s.lower()
    for frag in _LENDER_HQ_FRAGMENTS:
        if frag in lower:
            return ""
    s = _ADDR_TRAIL.sub("", s).strip()
    s = re.sub(r"\s{2,}", " ", s)
    s = s.rstrip(",;. ")
    if len(s) < 5:
        return ""
    return s


def parse_amount(raw) -> Optional[float]:
    """Parse monetary amount string to float, stripping commas/symbols."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("null", "none", "n/a", "-", ""):
        return None
    s = re.sub(r"[₹$,\s]", "", s)
    s = re.sub(r"/-$", "", s)
    try:
        return round(float(s), 2)
    except (ValueError, TypeError):
        return None


def extract_financial_from_text(text: str) -> Dict[str, Optional[float]]:
    """Extract all financial line items from raw OCR text using safe regex."""
    result = {}
    for name, pattern in [
        ("future_principal", FUTURE_PRINCIPAL_RE),
        ("principal_overdue", PRINCIPAL_OVERDUE_RE),
        ("interest_overdue", INTEREST_OVERDUE_RE),
        ("interest_on_termination", INTEREST_ON_TERM_RE),
        ("late_payment_penal", LATE_PAYMENT_RE),
        ("cheque_bounce_inc_gst", CHEQUE_BOUNCE_RE),
        ("other_charges_inc_gst", OTHER_CHARGES_RE),
        ("foreclosure_charges", FORECLOSURE_RE),
        ("litigation_charges", LITIGATION_RE),
        ("excess_amount", EXCESS_AMOUNT_RE),
        ("net_receivable", NET_RECEIVABLE_RE),
        ("sanction_amount", SANCTION_AMOUNT_RE),
    ]:
        m = pattern.search(text)
        if m:
            result[name] = parse_amount(m.group(1))
    roi_m = ROI_RE.search(text)
    if roi_m:
        result["roi_in_number"] = roi_m.group(1) + "%"
    return result


def check_third_party_mortgagor(
    property_owner: str,
    applicant_name: str,
    co_applicants: List[str],
) -> Dict[str, Any]:
    """Compare property owner against borrower/co-borrowers."""
    if not property_owner or not property_owner.strip():
        return {"is_third_party": False, "flag_text": "", "compliance_note": ""}

    owner_norm = property_owner.strip().lower()
    all_borrowers = [applicant_name.strip().lower()] if applicant_name else []
    all_borrowers.extend(c.strip().lower() for c in co_applicants if c and c.strip())

    for b in all_borrowers:
        if not b:
            continue
        if b in owner_norm or owner_norm in b:
            return {"is_third_party": False, "flag_text": "", "compliance_note": ""}
        # fuzzy: check if >60% words match
        bw = set(b.split())
        ow = set(owner_norm.split())
        if bw and ow and len(bw & ow) / max(len(bw), len(ow)) > 0.6:
            return {"is_third_party": False, "flag_text": "", "compliance_note": ""}

    return {
        "is_third_party": True,
        "flag_text": "[Third-Party Mortgagor / Security Provider]",
        "compliance_note": (
            f"Property owner '{property_owner}' is neither the primary borrower "
            f"nor any co-borrower. Verify consent and documentation."
        ),
    }


def apply_provenance_gate(
    result: Dict[str, Any],
    doc_types_found: List[str],
) -> Dict[str, Any]:
    """If only FCL docs and no mortgage/title docs, block property fields."""
    property_doc_types = {"modt", "legal_report", "title_search", "collateral_deed"}
    has_property_doc = bool(set(doc_types_found) & property_doc_types)

    if not has_property_doc:
        result["mortgaged_property_detail_1"] = "NOT FOUND"
        result["directions"] = "NOT FOUND"
        result["mortgaged_property_detail_2"] = "NOT FOUND"
        result["directions_2"] = "NOT FOUND"
        result["property_owner_mortgagor"] = "NOT FOUND"
        result["property_verification_status"] = "N/A (No Mortgage Document Uploaded)"
        result["property_verification_details"] = (
            "Only Foreclosure Notice uploaded; no MODT, Legal Report, "
            "or Title Deed in uploaded bundle."
        )
    return result


def compute_tos(result: Dict[str, Any]) -> Optional[float]:
    """Compute TOS = Net Receivable (or sum of components) - Foreclosure Charges."""
    net = parse_amount(result.get("net_receivable"))
    fc = parse_amount(result.get("foreclosure_charges")) or 0.0

    if net is not None:
        return round(net - fc, 2)

    # Sum individual components
    components = [
        "future_principal", "principal_overdue", "interest_overdue",
        "interest_on_termination", "late_payment_penal",
        "cheque_bounce_inc_gst", "other_charges_inc_gst",
        "litigation_charges",
    ]
    total = 0.0
    found_any = False
    for c in components:
        v = parse_amount(result.get(c))
        if v is not None:
            total += v
            found_any = True

    excess = parse_amount(result.get("excess_amount")) or 0.0
    if found_any:
        return round(total - excess, 2)
    return None
