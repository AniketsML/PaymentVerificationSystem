"""
Document and page filtering for the Legal Pipeline.

Pre-filters documents and pages BEFORE sending to the VLM API,
reducing unnecessary API calls and improving speed.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple


# Keywords are written the way a person writes them. Matching normalises both sides, so one
# entry covers "LOAN AGREEMENT.pdf", "Loan-Agreement.pdf", "loan_agreement.pdf" and
# "LOANAGREEMENT-67e80b17a8d5a.pdf" alike. Order here is only a tie-break of last resort.
KEYWORDS_MAP: Dict[str, Tuple[str, ...]] = {
    "sanction_letter": ("sanction letter", "sanction", "approval letter", "approval", "sl"),
    "foreclosure_notice": ("foreclosure notice", "foreclosure", "demand notice", "fcl"),
    "modt": ("modt", "modtd", "memorandum of deposit of title deed", "deposit of title deed",
             "memorandum", "mortgage deed", "title deed"),
    "legal_report": ("legal report", "title search report", "title search", "advocate report",
                     "title report", "search report"),
    "collateral_deed": ("collateral documents", "collateral docs", "collateral", "link sale deed",
                        "sale deed", "release deed", "gift deed", "partition deed", "conveyance",
                        "pattadhar passbook", "patta", "adangal"),
    "loan_agreement": ("loan agreement key facts statement", "loan agreement", "agreement"),
}

_SEP = re.compile(r"[^a-z0-9]+")
# the boundaries a filename implies without punctuation: camelCase, ACRONYMWord, and the joins
# between letters and digits ("StoreMODT", "SaleDeed3371", "HCFKOLSEC00001018478fcl")
_IMPLIED = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
                      r"|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")


def _normalise(text: str) -> Tuple[str, str]:
    """A filename as (spaced, squashed): 'UG-01 LoanAgreement.pdf' -> (' ug 01 loan agreement ',
    'ug01loanagreement'). Case and letter/digit changes are treated as separators, so a name
    written without any is still read word by word; the squashed form is the last resort for
    ALLCAPSRUNTOGETHER names, where there is no boundary left to find."""
    base = os.path.splitext(str(text or ""))[0]
    spaced = _SEP.sub(" ", _IMPLIED.sub(" ", base).lower()).strip()
    return f" {spaced} ", spaced.replace(" ", "")


def _squashed_hit(squashed: str, needle: str) -> int:
    """Where `needle` starts in the run-together name, ignoring hits that continue a word.

    Only the character BEFORE is checked: run-together names exist precisely to be followed by
    more words ("PattadharPassbookCopy"), so requiring a boundary after would throw most of them
    away. Requiring one before is what separates the account-number prefix in
    'HCFKOLSEC00001018478fcl' (a real fcl) from the 'sl' buried in 'HSLA'."""
    if len(needle) <= 2:
        return -1                                     # too short to be safe without separators
    at = squashed.find(needle)
    while at >= 0:
        if not at or not squashed[at - 1].isalpha():
            return at
        at = squashed.find(needle, at + 1)
    return -1


def classify_doc_by_filename(filename: str) -> str:
    """
    Classify a document type based on its filename using keyword matching.

    Filenames in the wild separate words with spaces, hyphens, underscores or nothing at all,
    so both the name and the keywords are normalised before comparing rather than matched as
    raw substrings — that is what lets one "legal report" entry catch "LEGAL_REPORT.pdf",
    "UGKOLTH0000073501 - LEGAL REPORT.pdf" and "LegalReport.pdf".

    Where several keywords match, the one appearing earliest in the name wins (a filename
    tends to lead with what the document is), and a longer keyword beats a shorter one at the
    same position, so "loan agreement key facts statement" is not read as a bare "agreement".

    Returns one of: sanction_letter, foreclosure_notice, modt, legal_report,
    collateral_deed, loan_agreement, or 'document' if no match.
    """
    spaced, squashed = _normalise(filename)
    best: Optional[Tuple[int, int, str]] = None      # (position, -length, doc_type)
    for doc_type, keywords in KEYWORDS_MAP.items():
        for kw in keywords:
            k_spaced, k_squashed = f" {kw} ", kw.replace(" ", "")
            pos = spaced.find(k_spaced)
            if pos < 0:
                # no whole-word hit: try the run-together form ("loanagreement", "…478fcl")
                pos = _squashed_hit(squashed, k_squashed)
                if pos >= 0:
                    pos += 1                          # keep it comparable to the spaced offsets
            if pos < 0:
                continue
            cand = (pos, -len(k_squashed), doc_type)
            if best is None or cand < best:
                best = cand
    return best[2] if best else "document"


def filter_documents(documents: List[Dict], extraction_plan) -> List[Dict]:
    """
    Filter documents based on the ExtractionPlan's target document types.

    If the plan specifies target_docs, only include documents whose classified
    type matches. Otherwise returns all documents.
    """
    if extraction_plan is None:
        return documents

    target_docs = getattr(extraction_plan, "target_docs", [])
    if not target_docs:
        return documents

    target_lower = [d.lower() for d in target_docs]
    filtered = []
    for doc in documents:
        filename = doc.get("filename", "")
        doc_type = classify_doc_by_filename(filename)
        if doc_type in target_lower or "document" in target_lower:
            filtered.append(doc)

    # If filtering resulted in nothing, return all docs (safety fallback)
    return filtered if filtered else documents


def get_priority_pages(doc_type: str, page_count: int, extraction_plan=None) -> List[int]:
    """
    Determine which pages to process for a given document type.

    Uses the ExtractionPlan's target_pages if available, otherwise
    selects pages based on document type heuristics.
    """
    if page_count <= 0:
        return []

    # Check extraction plan for explicit page targets
    if extraction_plan is not None:
        target_pages = getattr(extraction_plan, "target_pages", [])
        if target_pages:
            valid = [p for p in target_pages if 0 <= p < page_count]
            if valid:
                return sorted(set(valid))

    # Document-type-based page selection
    dt = doc_type.lower()
    pages: set = set()

    if dt in ("sanction_letter", "sl"):
        pages.update([0, 1, 2])
        if page_count > 3:
            pages.add(page_count - 1)
    elif dt in ("foreclosure_notice", "fcl"):
        pages.update([0, 1])
    elif dt in ("modt", "memorandum", "mortgage_deed"):
        pages.update([0, 1, 2, 3])
    elif dt in ("legal_report", "title_search"):
        pages.update([0, 1, 2])
    elif dt in ("collateral_deed", "sale_deed", "patta"):
        pages.update([0, 1, 2, 3])
    elif dt in ("loan_agreement", "agreement"):
        pages.update([0, 1])
        if page_count > 6:
            for i in range(max(0, page_count - 4), page_count):
                pages.add(i)
    else:
        # Unknown doc type: process all pages
        pages.update(range(page_count))

    return sorted(p for p in pages if 0 <= p < page_count)


def discover_pages_via_text(file_path: str, search_terms: List[str]) -> List[int]:
    """
    Scans the physical text layer of a PDF to find pages containing any of the search terms.
    
    This is used for discovery (fast path) to prioritize pages, NOT for exclusion.
    If the text layer is empty or messy, it just returns an empty list and the pipeline
    will fallback to default heuristic pages (full scan logic).
    """
    if not search_terms:
        return []
        
    try:
        import fitz
    except ImportError:
        sys.stderr.write("[doc_filter] PyMuPDF (fitz) not installed, skipping text discovery\n")
        return []

    matched_pages = set()
    try:
        doc = fitz.open(file_path)
        # Clean terms for searching
        terms_lower = [t.lower().strip() for t in search_terms if t.strip()]
        if not terms_lower:
            return []
            
        for page_num in range(len(doc)):
            text = doc[page_num].get_text("text").lower()
            if not text:
                continue
                
            for term in terms_lower:
                if term in text:
                    matched_pages.add(page_num)
                    break # Move to next page
        doc.close()
    except Exception as e:
        sys.stderr.write(f"[doc_filter] PDF text discovery error for {file_path}: {e}\n")

    return sorted(matched_pages)
