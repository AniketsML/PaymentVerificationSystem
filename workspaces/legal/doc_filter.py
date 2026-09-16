"""
Document and page filtering for the Legal Pipeline.

Pre-filters documents and pages BEFORE sending to the VLM API,
reducing unnecessary API calls and improving speed.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional


def classify_doc_by_filename(filename: str) -> str:
    """
    Classify a document type based on its filename using keyword matching.

    Returns one of: sanction_letter, foreclosure_notice, modt, legal_report,
    collateral_deed, loan_agreement, or 'document' if no match.
    """
    fn = filename.lower()
    keywords_map = {
        "sanction_letter": ("sanction", "approval", "sl_"),
        "foreclosure_notice": ("fcl", "foreclosure", "demand_notice"),
        "modt": ("modt", "memorandum", "mortgage_deed"),
        "legal_report": ("legal_report", "title_search", "advocate_report"),
        "collateral_deed": ("collateral", "sale_deed", "patta", "conveyance"),
        "loan_agreement": ("loan_agreement", "agreement"),
    }
    for doc_type, keywords in keywords_map.items():
        if any(k in fn for k in keywords):
            return doc_type
    return "document"


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
