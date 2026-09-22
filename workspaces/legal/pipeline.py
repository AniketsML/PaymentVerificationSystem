"""
8-stage confidence-gated extraction pipeline for SARFAESI loan dossiers.

Stage 1 — Triage (CNN/heuristic) -> printed / handwritten / blank
Stage 2A — Printed path (RapidOCR) -> High confidence skips VLM.
Stage 2B — Handwritten path (TrOCR) -> Always escalates to Stage 3.
Stage 3 — Dual extraction & comparison (VLM)
Stage 4 — Per-document structured output
Stage 5 — Cross-document merge (per person/lead)
Stage 6 — Validation pass
Stage 7 — Human review queue
Stage 8 — Speed optimization (Batching - not fully implemented here yet)
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from config import settings
from workspaces.legal.models import (
    SARFAESILeadResult, DocumentType, PropertyTier, OCRRoute, Phase,
    DOC_TYPE_PRIORITY, DOC_TYPE_PROPERTY_TIER,
    ExtractionSource, PageType, FieldCandidate, ValidationFlag, MergeConfidence
)
from workspaces.legal.ocr import LegalVLMClient

from workspaces.legal.logger import PgLegalLeadLogger
from workspaces.legal.jobs import update_document_status, update_lead_doc_counts
from workspaces.legal.meta import is_meta_key as _is_meta_key

_LAN_RE = re.compile(settings.ACCOUNT_LAN_PATTERN, re.IGNORECASE)

def _discover_lan(folder_name: str, filenames: List[str], digital_texts: Optional[List[str]] = None) -> str:
    for source in [folder_name] + filenames + (digital_texts or []):
        m = _LAN_RE.search(source or "")
        if m:
            return m.group(1)
    return ""

def _load_document(file_path: str) -> Tuple[Optional[Image.Image], str, int]:
    if not file_path or not os.path.exists(file_path):
        return None, f"File not found: {file_path}", 0
    ext = os.path.splitext(file_path)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp', '.gif'):
        try:
            img = Image.open(file_path)
            img.load()
            return img, "", 1
        except Exception as e:
            return None, f"Failed to open image: {e}", 0
    if ext == '.pdf':
        try:
            import fitz
            doc = fitz.open(file_path)
            pc = doc.page_count
            if pc == 0:
                doc.close()
                return None, "PDF has no pages", 0
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(200/72, 200/72))
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            doc.close()
            return img, "", pc
        except ImportError:
            return None, "PyMuPDF (fitz) not installed", 0
        except Exception as e:
            return None, f"Failed to process PDF: {e}", 0
    return None, f"Unsupported file type: {ext}", 0

def _is_institutional_lender(text: str) -> bool:
    """Check if a string represents an institutional financing entity, bank, or NBFC rather than an individual borrower."""
    if not text or not isinstance(text, str):
        return False
    t = text.strip().upper()
    lender_tokens = (
        "UGRO CAPITAL", "CHOKHANI SECURITIES", "EQUINOX BUSINESS PARK",
        "OFF BKC", "LBS ROAD, KURLA", "LBS ROAD KURLA", "MUMBAI 400070", "MUMBAI - 400070",
        "BANK OF BARODA", "STATE BANK OF INDIA", "HDFC BANK", "ICICI BANK", "AXIS BANK",
        "KOTAK MAHINDRA", "PUNJAB NATIONAL BANK", "CANARA BANK", "UNION BANK",
        "HOUSING FINANCE CORPORATION", "HOUSING FINANCE LIMITED", "HOUSING FINANCE LTD",
        "NON-BANKING FINANCIAL", "REGISTERED OFFICE OF THE LENDER"
    )
    for tok in lender_tokens:
        if tok in t:
            return True
    return False


def _extract_document_batch(file_path: str, pages: List[int], dpi: int = 90) -> List[Tuple[int, Image.Image]]:
    """
    Extracts specified 0-indexed pages from a document (PDF or image) and stamps
    a clear [PAGE X] visual badge on each image for direct multi-page VLM understanding.
    Returns list of (page_num_1_indexed, PIL_Image).
    """
    results = []
    if not file_path or not os.path.exists(file_path):
        return results

    ext = os.path.splitext(file_path)[1].lower()
    from PIL import ImageDraw, ImageFont

    # Load high-visibility font for [PAGE X] badge
    badge_font = None
    try:
        font_paths = [
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\calibrib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                badge_font = ImageFont.truetype(fp, 30)
                break
        if not badge_font:
            badge_font = ImageFont.load_default(size=26)
    except Exception:
        badge_font = None

    if ext in ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp', '.gif'):
        try:
            img = Image.open(file_path)
            img.load()
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            draw.rectangle([(10, 10), (220, 56)], fill=(220, 20, 20), outline=(255, 255, 255), width=2)
            if badge_font:
                draw.text((22, 16), "PAGE 1", fill=(255, 255, 255), font=badge_font)
            else:
                draw.text((20, 18), "PAGE 1", fill=(255, 255, 255))
            results.append((1, img))
        except Exception as e:
            sys.stderr.write(f"[pipeline] Image load error {file_path}: {e}\n")
        return results

    if ext == '.pdf':
        try:
            import fitz
            doc = fitz.open(file_path)
            scale = max(0.9, dpi / 72.0)
            for p_idx in pages:
                if 0 <= p_idx < doc.page_count:
                    pix = doc[p_idx].get_pixmap(matrix=fitz.Matrix(scale, scale))
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    
                    # Stamp high-contrast [PAGE X] badge with white outline and bold font
                    draw = ImageDraw.Draw(img)
                    badge_w = min(220, max(140, int(pix.width * 0.28)))
                    badge_h = min(56, max(36, int(pix.height * 0.055)))
                    draw.rectangle([(10, 10), (10 + badge_w, 10 + badge_h)], fill=(220, 20, 20), outline=(255, 255, 255), width=2)
                    if badge_font:
                        draw.text((22, 16), f"PAGE {p_idx + 1}", fill=(255, 255, 255), font=badge_font)
                    else:
                        draw.text((20, 16), f"PAGE {p_idx + 1}", fill=(255, 255, 255))
                    
                    results.append((p_idx + 1, img))
            doc.close()
        except Exception as e:
            sys.stderr.write(f"[pipeline] PDF batch extract error {file_path}: {e}\n")
    return results

def _extract_pdf_pages(file_path: str, pages: List[int]) -> List[Image.Image]:
    """Backward-compatible helper returning raw PIL images."""
    items = _extract_document_batch(file_path, pages, dpi=120)
    return [img for _, img in items]

def _classify_by_filename(filename: str) -> str:
    """One classifier, shared with doc_filter — two copies of the keyword table drifted apart
    once already. Returns "" rather than "document" so callers can tell "no idea" apart."""
    from workspaces.legal.doc_filter import classify_doc_by_filename
    dt = classify_doc_by_filename(filename)
    return "" if dt == "document" else dt

# Names that describe a bundle rather than a document type. A file called "ENTIRE FILE" or
# "LEGAL DOCUMENTS" may hold the loan agreement, the sale deed and the KYC all at once, so its
# name is no evidence that it lacks what the prompt asks for — it is never shortlisted out.
_BUNDLE_NAMES = ("entire file", "enter file", "entier file", "enter scan file", "whole file",
                 "all documents", "legal documents", "loan documents", "misc documents",
                 "property documents", "document", "documents", "scan", "file")


def _is_bundle_name(filename: str) -> bool:
    from workspaces.legal.doc_filter import _normalise
    spaced, _ = _normalise(filename)
    return any(f" {b} " in spaced or spaced.strip().endswith(b) for b in _BUNDLE_NAMES)


def _doc_matches_targets(doc: dict, target_docs: List[str]) -> bool:
    """Could this document hold what the prompt asks for? Deliberately generous: a wrong `False`
    silently drops evidence, while a wrong `True` only costs one extra model call."""
    filename = doc.get("filename", "") or ""
    if _is_bundle_name(filename):
        return True
    hint = _classify_by_filename(filename) or (
        "foreclosure_notice" if (doc.get("metadata") or {}).get("shared_fcl") else "document")
    fn_lower = filename.lower()
    for td in target_docs:
        td_clean = str(td).lower().replace("_", " ")
        if hint == td or td in hint:
            return True
        if td_clean in fn_lower or any(p in fn_lower for p in td_clean.split() if len(p) > 3):
            return True
    return False


def _get_priority_pages_for_type(doc_type: str, page_count: int) -> List[int]:
    if page_count <= 1: return [0]
    dt = doc_type.lower()
    if dt in ("sanction_letter", "sl"):
        pages = [0, 1, 2]
        if page_count > 3: pages.append(page_count - 1)
        return [p for p in pages if p < page_count]
    elif dt in ("foreclosure_notice", "fcl"):
        return [p for p in [0, 1] if p < page_count]
    elif dt in ("modt", "memorandum", "mortgage_deed"):
        return [p for p in [0, 1, 2, 3] if p < page_count]
    elif dt in ("legal_report", "title_search"):
        return [p for p in [0, 1, 2] if p < page_count]
    elif dt in ("collateral_deed", "sale_deed", "patta"):
        return [p for p in [0, 1, 2, 3] if p < page_count]
    elif dt in ("loan_agreement", "agreement"):
        pages = [0, 1]
        if page_count > 6:
            pages.extend([page_count - 4, page_count - 3, page_count - 2, page_count - 1])
        return sorted(list(set([p for p in pages if 0 <= p < page_count])))
    return [0]

def _merge_field(current: str, new_val: Optional[str]) -> str:
    if new_val and new_val.strip() and new_val.strip().lower() not in ("null", "none"):
        if not current or not current.strip():
            return new_val.strip()
    return current

def _merge_amount(current: Optional[float], new_val) -> Optional[float]:
    v = parse_amount(new_val)
    if v is not None and current is None:
        return v
    return current


def _get_merge_priority(source: str) -> MergeConfidence:
    if source == ExtractionSource.OCR_VLM_AGREED.value:
        return MergeConfidence.OCR_VLM_AGREED
    if source == ExtractionSource.OCR_ONLY.value:
        return MergeConfidence.SINGLE_HIGH_CONF
    if source == ExtractionSource.OCR_VLM_MISMATCH.value:
        return MergeConfidence.VLM_RESOLVED_MISMATCH
    return MergeConfidence.OCR_ONLY_LOW_CONF


def stage_5_cross_document_merge(lead_id: str, field_candidates: List[FieldCandidate], result: SARFAESILeadResult) -> None:
    """Stage 5: Merge fields across documents prioritizing by confidence."""
    
    # Group candidates by field name
    grouped: Dict[str, List[FieldCandidate]] = {}
    for c in field_candidates:
        # Ignore empty values
        if c.value is None or str(c.value).strip() == "" or str(c.value).lower() in ("null", "none"):
            continue
        if c.role_tag not in grouped:
            grouped[c.role_tag] = []
        grouped[c.role_tag].append(c)

    # Resolve field by field
    for field_name, candidates in grouped.items():
        if not candidates: continue
        
        final_field = field_name
        
        # Sort by merge priority (lower is better) and confidence (higher is better)
        candidates.sort(key=lambda x: (x.merge_priority.value, -x.confidence))
        
        best_candidate = candidates[0]
        
        # Check if ANY contributing candidate for this field has a mismatch flag
        # "Any field where the final chosen value still carries a dual-source-mismatch flag 
        # from any contributing document -> the merged field itself is flagged too"
        has_conflict = any(c.mismatch_flag for c in candidates)
        
        if has_conflict and best_candidate.role_tag not in result.flags:
            result.flags.append(f"mismatch:{best_candidate.role_tag}")
            
        # Apply to result
        val = best_candidate.value
        if hasattr(result, final_field):
            if final_field in ["sanction_amount", "future_principal", "principal_overdue", "interest_overdue", "total_outstanding_amount"]:
                parsed_val = parse_amount(val)
                if parsed_val is not None:
                    setattr(result, final_field, parsed_val)
            else:
                setattr(result, final_field, str(val))
        else:
            if not hasattr(result, "custom_fields"):
                setattr(result, "custom_fields", {})
            result.custom_fields[final_field] = val


def stage_6_validation_pass(result: SARFAESILeadResult) -> List[ValidationFlag]:
    """Stage 6: Cross-field sanity checks."""
    flags = []
    
    # 1. Check if LAN is missing
    if not result.account_no_lan:
        flags.append(ValidationFlag(field_name="account_no_lan", flag_type="missing", message="Account LAN is completely missing", severity="error"))
        
    # 2. Check if primary borrower is missing
    if not result.applicant_name:
        flags.append(ValidationFlag(field_name="applicant_name", flag_type="missing", message="Primary Borrower name is missing", severity="error"))
        
    # 3. Third party mortgagor check
    tp_res = check_third_party_mortgagor(result.property_owner_mortgagor, result.applicant_name, 
                                         [result.co_applicant_1, result.co_applicant_2, result.co_applicant_3])
    if tp_res.get("is_third_party"):
        flags.append(ValidationFlag(field_name="property_owner_mortgagor", flag_type="inconsistency", message=tp_res.get("compliance_note", ""), severity="warning"))
        if "third_party_mortgagor" not in result.flags:
            result.flags.append("third_party_mortgagor")

    return flags


def _normalize_extracted_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes and flattens dynamic extractions into clean top-level fields.
    Guarantees standard naming for:
      - borrower_name, borrower_address
      - co_borrower_1_name, co_borrower_1_address
      - co_borrower_2_name, co_borrower_2_address, etc.
    Flattens any nested dictionaries or lists of objects so no raw JSON objects reach the UI.
    """
    if not isinstance(data, dict):
        return {}

    normalized = {}

    def _clean_str(val):
        if val is None:
            return None
        s = str(val).strip()
        if not s or s.lower() in ("null", "none", "n/a", "not applicable", "not mentioned", "not found", "{}", "[]", "—"):
            return None
        return s

    # 1. Handle borrower / primary borrower objects or arrays
    borrower_obj = (
        data.get("borrower") or data.get("primary_borrower") or data.get("applicant") or
        data.get("borrower_details") or data.get("details_of_borrower") or data.get("details_of_the_borrower") or data.get("borrowers")
    )
    if isinstance(borrower_obj, list) and borrower_obj:
        first_b = borrower_obj[0]
        if isinstance(first_b, dict):
            b_name = (
                first_b.get("borrower_name") or first_b.get("applicant_name") or
                first_b.get("primary_borrower_name") or first_b.get("name")
            )
            b_addr = (
                first_b.get("borrower_address") or first_b.get("applicant_address") or
                first_b.get("primary_borrower_address") or first_b.get("address")
            )
            if _clean_str(b_name): normalized["borrower_name"] = _clean_str(b_name)
            if _clean_str(b_addr): normalized["borrower_address"] = _clean_str(b_addr)
            for bk, bv in first_b.items():
                if bk not in ("name", "borrower_name", "address", "borrower_address", "applicant_name", "applicant_address"):
                    cval = _clean_str(bv)
                    if cval: normalized[f"borrower_{bk}"] = cval
        elif isinstance(first_b, str) and _clean_str(first_b):
            normalized["borrower_name"] = _clean_str(first_b)

        # Subsequent entries in borrower_details are secondary parties (co-borrowers)
        for extra_idx, extra_b in enumerate(borrower_obj[1:], start=1):
            if isinstance(extra_b, dict):
                eb_name = (
                    extra_b.get("co_borrower_name") or extra_b.get("co_applicant_name") or
                    extra_b.get("borrower_name") or extra_b.get("name")
                )
                eb_addr = (
                    extra_b.get("co_borrower_address") or extra_b.get("co_applicant_address") or
                    extra_b.get("borrower_address") or extra_b.get("address")
                )
                if _clean_str(eb_name): normalized[f"co_borrower_{extra_idx}_name"] = _clean_str(eb_name)
                if _clean_str(eb_addr): normalized[f"co_borrower_{extra_idx}_address"] = _clean_str(eb_addr)
            elif isinstance(extra_b, str) and _clean_str(extra_b):
                normalized[f"co_borrower_{extra_idx}_name"] = _clean_str(extra_b)

    elif isinstance(borrower_obj, dict):
        b_name = (
            borrower_obj.get("borrower_name") or borrower_obj.get("applicant_name") or
            borrower_obj.get("primary_borrower_name") or borrower_obj.get("name")
        )
        b_addr = (
            borrower_obj.get("borrower_address") or borrower_obj.get("applicant_address") or
            borrower_obj.get("primary_borrower_address") or borrower_obj.get("address")
        )
        if _clean_str(b_name): normalized["borrower_name"] = _clean_str(b_name)
        if _clean_str(b_addr): normalized["borrower_address"] = _clean_str(b_addr)
        for bk, bv in borrower_obj.items():
            if bk not in ("name", "borrower_name", "address", "borrower_address", "applicant_name", "applicant_address"):
                cval = _clean_str(bv)
                if cval: normalized[f"borrower_{bk}"] = cval

    # 2. Handle co-borrowers (list of dicts, list of strings, single dict, etc.)
    co_borrowers_raw = (
        data.get("co_borrowers") or data.get("co_borrower") or
        data.get("co_applicants") or data.get("co_applicant") or
        data.get("co_borrower_details") or data.get("details_of_co_borrower")
    )
    if isinstance(co_borrowers_raw, list):
        start_idx = 1
        while f"co_borrower_{start_idx}_name" in normalized:
            start_idx += 1
        for idx, item in enumerate(co_borrowers_raw, start=start_idx):
            if isinstance(item, dict):
                c_name = (
                    item.get("co_borrower_name") or item.get("co_applicant_name") or
                    item.get("name") or item.get("borrower_name")
                )
                c_addr = (
                    item.get("co_borrower_address") or item.get("co_applicant_address") or
                    item.get("address") or item.get("borrower_address")
                )
                if _clean_str(c_name): normalized[f"co_borrower_{idx}_name"] = _clean_str(c_name)
                if _clean_str(c_addr): normalized[f"co_borrower_{idx}_address"] = _clean_str(c_addr)
                for ik, iv in item.items():
                    if ik not in ("name", "co_borrower_name", "address", "co_borrower_address", "borrower_name", "borrower_address", "co_applicant_name", "co_applicant_address"):
                        cval = _clean_str(iv)
                        if cval: normalized[f"co_borrower_{idx}_{ik}"] = cval
            elif isinstance(item, str) and _clean_str(item):
                normalized[f"co_borrower_{idx}_name"] = _clean_str(item)
    elif isinstance(co_borrowers_raw, dict):
        c_name = (
            co_borrowers_raw.get("co_borrower_name") or co_borrowers_raw.get("co_applicant_name") or
            co_borrowers_raw.get("name") or co_borrowers_raw.get("borrower_name")
        )
        c_addr = (
            co_borrowers_raw.get("co_borrower_address") or co_borrowers_raw.get("co_applicant_address") or
            co_borrowers_raw.get("address") or co_borrowers_raw.get("borrower_address")
        )
        if _clean_str(c_name): normalized["co_borrower_1_name"] = _clean_str(c_name)
        if _clean_str(c_addr): normalized["co_borrower_1_address"] = _clean_str(c_addr)
        for ik, iv in co_borrowers_raw.items():
            if ik not in ("name", "co_borrower_name", "address", "co_borrower_address", "borrower_name", "borrower_address", "co_applicant_name", "co_applicant_address"):
                cval = _clean_str(iv)
                if cval: normalized[f"co_borrower_1_{ik}"] = cval

    # 3. Process remaining keys from data
    for k, v in data.items():
        if k.startswith("_") or k.endswith("_page_sources") or k in ("borrower", "primary_borrower", "applicant", "co_borrowers", "co_borrower", "co_applicants", "co_applicant", "borrower_details", "co_borrower_details", "details_of_borrower", "details_of_co_borrower", "details_of_the_borrower", "borrowers", "cited_pages", "field_page_sources"):
            continue
        # the model's bookkeeping is never a value — spelled without its underscore, a nested
        # note like field_scripts used to be flattened into "field_scripts_borrower_name" columns
        if _is_meta_key(k):
            continue

        clean_k = k.strip().lower().replace("-", "_").replace(" ", "_")

        # Normalize borrower aliases
        if clean_k in ("applicant_name", "primary_applicant", "name_of_borrower", "borrower") and "borrower_name" not in normalized:
            cval = _clean_str(v)
            if cval:
                normalized["borrower_name"] = cval
            continue
        if clean_k in ("applicant_address", "address_of_borrower") and "borrower_address" not in normalized:
            cval = _clean_str(v)
            if cval:
                normalized["borrower_address"] = cval
            continue

        # Normalize single co-borrower
        if clean_k in ("co_borrower_name", "co_applicant_name") and "co_borrower_1_name" not in normalized:
            cval = _clean_str(v)
            if cval:
                normalized["co_borrower_1_name"] = cval
            continue
        if clean_k in ("co_borrower_address", "co_applicant_address") and "co_borrower_1_address" not in normalized:
            cval = _clean_str(v)
            if cval:
                normalized["co_borrower_1_address"] = cval
            continue

        # Match co_borrower_1_name, co_applicant_1_address, etc.
        m_co = re.match(r"co_(?:borrower|applicant)_?(\d+)(?:_(name|address))?", clean_k)
        if m_co:
            num = m_co.group(1)
            field_type = m_co.group(2) or "name"
            target_key = f"co_borrower_{num}_{field_type}"
            cval = _clean_str(v)
            if cval:
                normalized[target_key] = cval
            continue

        # Nested dict
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                cval = _clean_str(sub_v)
                if cval:
                    normalized[f"{clean_k}_{sub_k}"] = cval
            continue

        # List
        if isinstance(v, list):
            scalar_items = [str(x).strip() for x in v if _clean_str(x)]
            if scalar_items:
                normalized[clean_k] = ", ".join(scalar_items)
            continue

        cval = _clean_str(v)
        if cval:
            normalized[clean_k] = cval

    # 4. Strict Deduplication between Borrower and Co-Borrowers
    # A person/entity must NEVER appear as both Borrower and Co-Borrower
    b_name_clean = (normalized.get("borrower_name") or "").strip().lower()
    if b_name_clean:
        co_keys_to_remove = []
        for k, v in list(normalized.items()):
            if re.match(r"^co_borrower_\d+_name$", k) and str(v).strip().lower() == b_name_clean:
                prefix = k[:-len("_name")]
                co_keys_to_remove.extend([k, f"{prefix}_address"])
        for k in co_keys_to_remove:
            normalized.pop(k, None)

    # Re-index co-borrowers so there are no holes (co_borrower_1, co_borrower_2, ...)
    reindexed = {}
    co_tuples = []
    for k, v in list(normalized.items()):
        m = re.match(r"^co_borrower_(\d+)_(name|address)$", k)
        if m:
            co_tuples.append((int(m.group(1)), m.group(2), v, k))
    
    if co_tuples:
        old_indices = sorted(list(set(t[0] for t in co_tuples)))
        index_map = {old_idx: new_idx for new_idx, old_idx in enumerate(old_indices, start=1)}
        for old_idx, field_type, val, orig_key in co_tuples:
            normalized.pop(orig_key, None)
            new_idx = index_map[old_idx]
            reindexed[f"co_borrower_{new_idx}_{field_type}"] = val
        normalized.update(reindexed)

    return normalized


def process_lead(
    lead_id: str, lead_name: str, folder_name: str,
    documents: List[Dict[str, Any]], ocr: Any,
    logger: PgLegalLeadLogger, is_test: bool = False,
    extraction_prompt: str = "",
) -> Dict[str, Any]:
    """Process an entire lead through the intelligent scope & VLM extraction pipeline."""
    t_start = time.perf_counter()
    phase_timings: Dict[str, float] = {}
    ocr_routes_used: List[str] = []

    logger.log(lead_id, "lead_received", "PASS",
               reason=f"Lead '{lead_name}' with {len(documents)} document(s)", is_test=is_test)

    # Fetch documents from DB if not provided
    if not documents:
        from db import pg
        with pg.pool().connection() as c:
            documents = c.execute("SELECT * FROM legal_lead_documents WHERE lead_id=%s ORDER BY filename", (lead_id,)).fetchall()
            documents = [dict(d) for d in documents]

    if not documents:
        logger.log(lead_id, "load_docs", "FAIL", reason="No documents registered", is_test=is_test)
        return {"status": "missing_documents", "lead_id": lead_id, "error": "No documents"}

    docs_sorted = sorted(documents, key=lambda d: (
        1 if (d.get("metadata") or {}).get("shared_fcl") else 0,
        DOC_TYPE_PRIORITY.get(
            DocumentType(_classify_by_filename(d.get("filename", "")) or "other")
            if _classify_by_filename(d.get("filename", "")) else DocumentType.OTHER, 9)
    ))

    filenames = [d.get("filename", "") for d in docs_sorted]
    folder_lan = _discover_lan(folder_name, filenames, [])

    import threading
    seen_page_hashes = set()
    hash_lock = threading.Lock()

    # Stage 0: The Intent Planner
    try:
        from workspaces.legal.prompt_analyzer import analyze_extraction_prompt
        plan_obj = analyze_extraction_prompt(extraction_prompt)
        headings = [inst.heading_hint for inst in getattr(plan_obj, "instructions", []) if getattr(inst, "heading_hint", "")]
        extraction_plan = {
            "target_docs": plan_obj.target_docs,
            "target_pages": plan_obj.target_pages,
            "keywords": plan_obj.keywords,
            "headings": headings,
            "heading": headings[0] if headings else "",
        }
    except Exception as e:
        sys.stderr.write(f"[pipeline] Planner failed: {e}\n")
        extraction_plan = {}

    processed_docs, failed_docs, skipped_docs = 0, 0, 0
    raw_extractions: Dict[str, Any] = {}
    if folder_lan:
        raw_extractions["account_no_lan"] = folder_lan

    page_extractions: List[Dict[str, Any]] = []
    # one entry per model call, whatever it returned — so an empty dossier can say why,
    # and tokens / model time count every call (not only the ones that yielded values)
    vlm_calls: List[Dict[str, Any]] = []

    def record_batch(batch: Dict[str, Any], outcome: str, status: str, meta: Dict[str, Any] = None,
                     values: int = 0, error: str = "", raw: str = "") -> None:
        meta = meta or {}
        entry = dict(batch, outcome=outcome, status=status, values=values,
                     model=meta.get("model", ""), route=meta.get("route", ""),
                     prompt_tokens=meta.get("prompt_tokens") or 0,
                     completion_tokens=meta.get("completion_tokens") or 0,
                     error=error[:400])
        vlm_calls.append(entry)
        p0, p1 = batch["pages"]
        logger.log(lead_id, "vlm_batch", status, document_id=batch["document_id"],
                   reason=f"{batch['filename']} · pages {p0}–{p1}: {outcome}",
                   ms=batch["ms"],
                   metrics={"prompt_tokens": entry["prompt_tokens"],
                            "completion_tokens": entry["completion_tokens"], "values": values},
                   data={"pages": batch["pages"], "route": entry["route"], "error": entry["error"],
                         "raw_response": (raw or "")[:1500]},
                   is_test=is_test)

    # Which documents does the prompt actually ask for? Worked out for the WHOLE dossier before
    # reading any of it, because the answer "none of them" must not mean "read nothing" — a
    # dossier whose files are named LAN_ENTIRE_FILE.pdf or LAN_LEGAL_DOCUMENTS.pdf says nothing
    # about its contents, and skipping it reported 154 dossiers as "missing documents" when
    # every document was present. Shortlisting is an optimisation; it never empties a dossier.
    target_docs = extraction_plan.get("target_docs", []) or []
    shortlist = {d.get("document_id", "") for d in docs_sorted
                 if _doc_matches_targets(d, target_docs)} if target_docs else set()
    if target_docs and not shortlist:
        logger.log(lead_id, "shortlist", "INFO",
                   reason=f"no filename matches {', '.join(target_docs)} — reading all "
                          f"{len(docs_sorted)} document(s) rather than none", is_test=is_test)

    t_ocr = time.perf_counter()
    for doc in docs_sorted:
        fp = doc.get("file_path", "")
        doc_id = doc.get("document_id", "")
        filename = doc.get("filename", "")
        is_shared_fcl = bool((doc.get("metadata") or {}).get("shared_fcl"))
        doc_type_hint = _classify_by_filename(filename) or ("foreclosure_notice" if is_shared_fcl else "document")

        if shortlist and doc_id not in shortlist:
            # deliberately not read — recorded as such, so the dossier does not sit there
            # looking like it is still queued once it has finished
            update_document_status(doc_id, "skipped", document_type=doc_type_hint,
                                   error_message=f"not among the document types the prompt asks for "
                                                 f"({', '.join(target_docs)})")
            skipped_docs += 1
            update_lead_doc_counts(lead_id)
            continue

        if not fp or not os.path.exists(fp):
            logger.log(lead_id, "doc_load", "FAIL", document_id=doc_id,
                       reason=f"{filename}: file is missing from disk", is_test=is_test)
            update_document_status(doc_id, "failed", document_type=doc_type_hint,
                                   error_message="file is missing from disk")
            failed_docs += 1
            update_lead_doc_counts(lead_id)
            continue

        fsize = os.path.getsize(fp)

        try:
            # Check page count
            ext = os.path.splitext(fp)[1].lower()
            page_count = 1
            if ext == '.pdf':
                try:
                    import fitz
                    pdoc = fitz.open(fp)
                    page_count = pdoc.page_count
                    pdoc.close()
                except Exception:
                    page_count = 1

            if page_count == 0:
                logger.log(lead_id, "doc_load", "FAIL", document_id=doc_id, reason="PDF has no pages", is_test=is_test)
                update_document_status(doc_id, "failed", document_type=doc_type_hint,
                                       error_message="PDF has no pages", file_size_bytes=fsize)
                failed_docs += 1
                update_lead_doc_counts(lead_id)
                continue

            # Stage 1.6: Scope Pages for this document
            if extraction_plan.get("target_pages"):
                pages_to_process = [p for p in extraction_plan["target_pages"] if 0 <= p < page_count]
                if not pages_to_process:
                    pages_to_process = list(range(page_count))
            else:
                # Process the whole document in batches
                pages_to_process = list(range(page_count))

            # Batch pages into chunks of at most 20 pages (or 1 batch if <= 20)
            BATCH_SIZE = 20
            page_chunks = [pages_to_process[i:i + BATCH_SIZE] for i in range(0, len(pages_to_process), BATCH_SIZE)]

            doc_ok = doc_empty = doc_err = 0
            last_err = ""
            for chunk in page_chunks:
                # 1. Render and stamp pages with [PAGE X]
                page_items = _extract_document_batch(fp, chunk, dpi=90)
                if not page_items:
                    continue

                chunk_page_nums = [item[0] for item in page_items]
                chunk_images = [item[1] for item in page_items]

                # 2. Directly call VLM on the entire batch in a single API call!
                t_call = time.perf_counter()
                extracted = ocr.extract_from_images(
                    images=chunk_images,
                    document_type=f"{filename} ({doc_type_hint})",
                    page_numbers=chunk_page_nums,
                    extraction_prompt=extraction_prompt,
                    heading_hint=extraction_plan.get("heading", ""),
                    headings=extraction_plan.get("headings", []),
                    keywords=extraction_plan.get("keywords", []),
                    total_pages=page_count
                )
                batch = {"document_id": doc_id, "filename": filename,
                         "pages": [chunk_page_nums[0], chunk_page_nums[-1]],
                         "ms": round((time.perf_counter() - t_call) * 1000, 1)}

                if not extracted or extracted.get("_status") in ("blank_discarded", "duplicate_discarded"):
                    record_batch(batch, "model returned nothing", "EMPTY")
                    doc_empty += 1
                    continue

                meta = extracted.pop("_meta", {})
                route = extracted.pop("_ocr_route", "vlm")
                raw_resp = extracted.pop("_raw_response", "")
                if route not in ocr_routes_used:
                    ocr_routes_used.append(route)

                # a failed model call is recorded as such, never mistaken for "found nothing"
                call_error = extracted.pop("_medha_error", "") or ""
                fallback_error = extracted.pop("_error", "") or ""
                if fallback_error or extracted.get("_parse_error"):
                    last_err = call_error or fallback_error or "unreadable model response"
                    record_batch(batch, f"model error — {last_err}", "FAIL", meta,
                                 error=last_err, raw=raw_resp or extracted.get("_raw_text", ""))
                    doc_err += 1
                    continue

                # Capture provenance metadata
                cited_pages = extracted.pop("_cited_pages", [])
                field_page_sources = extracted.pop("_field_page_sources", {}) or {}
                # how each value appears on the page (handwritten / printed) — absent on
                # runs extracted before this was asked for, which read as "unknown".
                field_scripts = extracted.pop("_field_scripts", {}) or {}
                if not isinstance(field_scripts, dict):
                    field_scripts = {}

                # Also capture flat keys like borrower_name_page_sources
                for k in list(extracted.keys()):
                    if k.endswith("_page_sources"):
                        val = extracted.pop(k)
                        base_field = k[:-len("_page_sources")]
                        if val and base_field not in field_page_sources:
                            try:
                                p_str = str(val).split(",")[0].strip()
                                if p_str.isdigit():
                                    field_page_sources[base_field] = int(p_str)
                            except Exception:
                                pass

                # Normalize extracted fields
                norm_fields = _normalize_extracted_fields(extracted)
                if not norm_fields:
                    record_batch(batch, "model returned no values", "EMPTY", meta, raw=raw_resp)
                    doc_empty += 1
                    continue

                # Filter out institutional lenders/financing entities from borrower fields
                clean_norm_fields = {}
                for fk, fv in norm_fields.items():
                    if isinstance(fv, str):
                        if ("borrower" in fk or fk in ("applicant_name", "applicant_address")) and _is_institutional_lender(fv):
                            continue
                        if "address" in fk and _is_institutional_lender(fv):
                            continue
                    clean_norm_fields[fk] = fv
                norm_fields = clean_norm_fields
                if not norm_fields:
                    record_batch(batch, "only lender / bank details found — filtered out", "EMPTY", meta, raw=raw_resp)
                    doc_empty += 1
                    continue

                # Record page_extractions with EXACT pages where data was found
                if isinstance(field_page_sources, dict) and field_page_sources:
                    by_page = {}
                    for fk, fv in norm_fields.items():
                        src_p = field_page_sources.get(fk)
                        if src_p is None:
                            for spk, spv in field_page_sources.items():
                                if spk in fk or fk in spk:
                                    src_p = spv
                                    break
                        try:
                            src_p_int = int(src_p) if src_p is not None else None
                        except (ValueError, TypeError):
                            src_p_int = None

                        if not src_p_int and cited_pages:
                            try:
                                src_p_int = int(cited_pages[0])
                            except Exception:
                                src_p_int = chunk_page_nums[0]
                        elif not src_p_int:
                            src_p_int = chunk_page_nums[0]

                        # Clamping / reconciliation: Ensure src_p_int belongs to current chunk
                        if src_p_int not in chunk_page_nums and chunk_page_nums:
                            src_p_int = chunk_page_nums[0]

                        by_page.setdefault(src_p_int, {})[fk] = fv

                    for p_num, p_fields in by_page.items():
                        if p_fields:
                            page_extractions.append({
                                "document_id": doc_id,
                                "filename": filename,
                                "page_number": p_num,
                                "fields": p_fields,
                                "field_scripts": field_scripts,
                                "ocr_route": route,
                                "telemetry": meta,
                                "mismatches": [],
                                "raw_response": raw_resp,
                                "raw_ocr_text": raw_resp,
                                "ocr_confidence": 1.0
                            })
                else:
                    target_cited = []
                    if isinstance(cited_pages, list):
                        for p in cited_pages:
                            try:
                                p_int = int(p)
                                if p_int in chunk_page_nums:
                                    target_cited.append(p_int)
                            except Exception:
                                pass
                    if not target_cited:
                        target_cited = [chunk_page_nums[0]]

                    for p_num in target_cited:
                        page_extractions.append({
                            "document_id": doc_id,
                            "filename": filename,
                            "page_number": p_num,
                            "fields": norm_fields,
                            "field_scripts": field_scripts,
                            "ocr_route": route,
                            "telemetry": meta,
                            "mismatches": [],
                            "raw_response": raw_resp,
                            "raw_ocr_text": raw_resp,
                            "ocr_confidence": 1.0
                        })

                record_batch(batch, f"{len(norm_fields)} value{'s' if len(norm_fields) != 1 else ''} found", "PASS",
                             meta, values=len(norm_fields))
                doc_ok += 1

            if doc_err and not doc_ok and not doc_empty:
                # every model call for this document failed — it was never actually read
                logger.log(lead_id, "doc_ocr", "FAIL", document_id=doc_id,
                           reason=f"{filename}: every model call failed — {last_err}", is_test=is_test)
                update_document_status(doc_id, "failed", document_type=doc_type_hint,
                                       error_message=last_err[:500], file_size_bytes=fsize, page_count=page_count)
                failed_docs += 1
                update_lead_doc_counts(lead_id)
                continue

            processed_docs += 1
            update_document_status(doc_id, "processed", document_type=doc_type_hint, file_size_bytes=fsize, page_count=page_count)
            update_lead_doc_counts(lead_id)

        except Exception as e:
            logger.log(lead_id, "doc_ocr", "FAIL", document_id=doc_id, reason=str(e), is_test=is_test)
            update_document_status(doc_id, "failed", error_message=str(e), file_size_bytes=fsize, page_count=page_count)
            failed_docs += 1
            update_lead_doc_counts(lead_id)
            continue

    phase_timings["document_extraction_ms"] = round((time.perf_counter() - t_ocr) * 1000, 1)

    # File every value under the page the DOCUMENT says it is on, not the page the model claimed.
    # See page_attribution.py: the model's citation was right 42% of the time on a real run.
    # Values are untouched; only the page they are filed under can change.
    if page_extractions:
        t_attr = time.perf_counter()
        try:
            from workspaces.legal.page_attribution import reattribute
            doc_paths = {d.get("document_id", ""): d.get("file_path", "") for d in docs_sorted}
            page_extractions, attr_stats = reattribute(page_extractions, doc_paths)
            phase_timings["page_attribution_ms"] = round((time.perf_counter() - t_attr) * 1000, 1)
            logger.log(lead_id, "page_attribution", "PASS",
                       reason=f"{attr_stats['verified']} of {attr_stats['values']} values confirmed "
                              f"against the document text ({attr_stats['moved']} moved to the right page); "
                              f"{attr_stats['unverified']} had no text layer to check",
                       ms=phase_timings["page_attribution_ms"],
                       metrics={"values": attr_stats["values"], "verified": attr_stats["verified"],
                                "moved": attr_stats["moved"], "unverified": attr_stats["unverified"]},
                       is_test=is_test)
        except Exception as e:  # noqa: BLE001 — never lose an extraction over page bookkeeping
            sys.stderr.write(f"[pipeline] page re-attribution failed for {lead_id}: {e}\n")

    phase_timings["total_pipeline_ms"] = round((time.perf_counter() - t_start) * 1000, 1)

    final_status = "completed"
    if processed_docs == 0:
        # nothing was read: because the model failed on every document, or because no
        # document matched the prompt / existed at all
        final_status = "failed" if failed_docs > 0 else "missing_documents"
    elif failed_docs > 0:
        final_status = "partial"

    try:
        from db import pg
        import json

        # Collect all cited pages accurately across page_extractions
        cited_pages_set = set()
        for px in page_extractions:
            p_num = px.get("page_number")
            if p_num:
                try:
                    cited_pages_set.add(int(p_num))
                except Exception:
                    pass
        raw_extractions["_cited_pages"] = sorted(list(cited_pages_set))

        # Merge page extractions into top-level raw_extractions safely:
        # Prevent institutional lenders from hijacking borrower fields and avoid blind string length overwrites
        for px in page_extractions:
            fields = px.get("fields", {})
            for k, v in fields.items():
                if k.startswith("_"):
                    continue
                if v is None or v == "" or v == "null" or v == {}:
                    continue
                if isinstance(v, str):
                    clean_v = v.strip()
                    if ("borrower" in k or k in ("applicant_name", "applicant_address")) and _is_institutional_lender(clean_v):
                        continue
                    if "address" in k and _is_institutional_lender(clean_v):
                        continue

                    if k not in raw_extractions or not raw_extractions[k]:
                        raw_extractions[k] = clean_v
                    else:
                        existing = str(raw_extractions[k]).strip()
                        if _is_institutional_lender(existing):
                            raw_extractions[k] = clean_v
                        elif clean_v.lower().startswith(existing.lower()) or existing.lower() in clean_v.lower():
                            if len(clean_v) > len(existing):
                                raw_extractions[k] = clean_v
                else:
                    if k not in raw_extractions:
                        raw_extractions[k] = v

        # Re-normalize full raw_extractions payload
        clean_top_level = _normalize_extracted_fields(raw_extractions)
        raw_extractions.update(clean_top_level)

        # Lead-level aggregated model and token telemetry — summed over every model call
        # (a call that returned nothing still cost tokens and time; and one call citing two
        # pages is counted once, not per page)
        total_prompt_tokens = sum(c.get("prompt_tokens") or 0 for c in vlm_calls)
        total_completion_tokens = sum(c.get("completion_tokens") or 0 for c in vlm_calls)
        total_vlm_ms = sum(c.get("ms") or 0 for c in vlm_calls)
        models_used = sorted({c.get("model") for c in vlm_calls if c.get("model")})
        model_name = ", ".join(models_used) if models_used else "Medha VLM"

        lead_telemetry = {
            "model": model_name,
            "vlm_latency_ms": round(total_vlm_ms, 1),
            "pipeline_latency_ms": phase_timings.get("total_pipeline_ms", 0),
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "cited_page_count": len(page_extractions),
            "vlm_calls": len(vlm_calls),
            "vlm_errors": sum(1 for c in vlm_calls if c.get("status") == "FAIL"),
            "empty_batches": sum(1 for c in vlm_calls if c.get("status") == "EMPTY"),
        }
        raw_extractions["_vlm_calls"] = vlm_calls

        raw_extractions["_telemetry"] = lead_telemetry
        raw_extractions["telemetry"] = lead_telemetry
        raw_extractions["_phase_timings"] = phase_timings
        raw_extractions["_page_extractions"] = page_extractions
        raw_extractions["page_extractions"] = page_extractions
        raw_extractions["_cited_pages"] = sorted(list(set(px["page_number"] for px in page_extractions)))

        with pg.pool().connection() as c:
            c.execute(
                "UPDATE legal_leads SET status = %s, extracted_data = %s, updated_at = now() WHERE lead_id = %s",
                (final_status, json.dumps(raw_extractions), lead_id)
            )
            c.execute("""
                INSERT INTO legal_lead_results (
                    lead_id, processing_status, applicant_name, applicant_address,
                    account_no_lan, raw_extractions, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (lead_id) DO UPDATE SET
                    processing_status = EXCLUDED.processing_status,
                    applicant_name = COALESCE(EXCLUDED.applicant_name, legal_lead_results.applicant_name),
                    applicant_address = COALESCE(EXCLUDED.applicant_address, legal_lead_results.applicant_address),
                    account_no_lan = COALESCE(EXCLUDED.account_no_lan, legal_lead_results.account_no_lan),
                    raw_extractions = EXCLUDED.raw_extractions,
                    updated_at = now()
            """, (
                lead_id,
                final_status,
                raw_extractions.get("borrower_name") or raw_extractions.get("applicant_name"),
                raw_extractions.get("borrower_address") or raw_extractions.get("applicant_address"),
                raw_extractions.get("account_no_lan") or raw_extractions.get("loan_account_number"),
                json.dumps(raw_extractions)
            ))

        update_lead_doc_counts(lead_id)
        logger.log(lead_id, "pipeline_complete", "SUCCESS", is_test=is_test, reason=final_status)
    except Exception as e:
        sys.stderr.write(f"[pipeline] DB update failed: {e}\n")
        try:
            from db import pg
            with pg.pool().connection() as c:
                c.execute("UPDATE legal_leads SET status = %s WHERE lead_id = %s", ("failed", lead_id))
        except:
            pass

    return {"status": final_status, "lead_id": lead_id, "extracted": len(raw_extractions)}


def run_dynamic_extraction(lead_id: str, extraction_prompt: str, is_test: bool = False) -> Dict[str, Any]:
    """Execute dynamic user-prompt-driven extraction for a single lead dossier."""
    logger = PgLegalLeadLogger()
    from workspaces.legal.ocr import LegalVLMClient
    ocr = LegalVLMClient()
    return process_lead(
        lead_id=lead_id, lead_name="", folder_name="", documents=[],
        ocr=ocr, logger=logger, is_test=is_test, extraction_prompt=extraction_prompt
    )

