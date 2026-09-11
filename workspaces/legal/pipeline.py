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
from workspaces.legal.ocr import LegalOCRClient
from workspaces.legal.sanitize import (
    sanitize_name, sanitize_address, parse_amount,
    extract_financial_from_text, check_third_party_mortgagor,
    apply_provenance_gate, compute_tos,
)
from workspaces.legal.logger import PgLegalLeadLogger
from workspaces.legal.jobs import update_document_status, update_lead_doc_counts

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

def _extract_pdf_pages(file_path: str, pages: List[int]) -> List[Image.Image]:
    images = []
    try:
        import fitz
        doc = fitz.open(file_path)
        for p in pages:
            if 0 <= p < doc.page_count:
                pix = doc[p].get_pixmap(matrix=fitz.Matrix(200/72, 200/72))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                images.append(img)
        doc.close()
    except Exception as e:
        sys.stderr.write(f"[pipeline] PDF page extract error: {e}\n")
    return images

def _classify_by_filename(filename: str) -> str:
    fn = filename.lower()
    for kws, dt in [
        (("sanction", "approval", "sl_"), "sanction_letter"),
        (("fcl", "foreclosure", "demand_notice"), "foreclosure_notice"),
        (("modt", "memorandum", "mortgage_deed"), "modt"),
        (("legal_report", "title_search", "advocate_report"), "legal_report"),
        (("collateral", "sale_deed", "patta", "conveyance"), "collateral_deed"),
        (("loan_agreement", "agreement"), "loan_agreement"),
    ]:
        if any(k in fn for k in kws):
            return dt
    return ""

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


def process_lead(
    lead_id: str, lead_name: str, folder_name: str,
    documents: List[Dict[str, Any]], ocr: LegalOCRClient,
    logger: PgLegalLeadLogger, is_test: bool = False,
    extraction_prompt: str = "",
) -> Dict[str, Any]:
    """Process an entire lead through the 8-stage pipeline."""
    t_start = time.perf_counter()
    phase_timings: Dict[str, float] = {}
    ocr_routes_used: List[str] = []
    doc_types_found: List[str] = []

    logger.log(lead_id, "lead_received", "PASS",
               reason=f"Lead '{lead_name}' with {len(documents)} document(s)", is_test=is_test)

    result = SARFAESILeadResult(lead_id=lead_id)
    total_docs = len(documents)
    processed_docs, failed_docs = 0, 0
    raw_extractions: Dict[str, Any] = {}
    page_extractions: List[Dict[str, Any]] = []
    all_field_candidates: List[FieldCandidate] = []

    docs_sorted = sorted(documents, key=lambda d: (
        1 if (d.get("metadata") or {}).get("shared_fcl") else 0,
        DOC_TYPE_PRIORITY.get(
            DocumentType(_classify_by_filename(d.get("filename", "")) or "other")
            if _classify_by_filename(d.get("filename", "")) else DocumentType.OTHER, 9)
    ))
    
    filenames = [d.get("filename", "") for d in docs_sorted]
    folder_lan = _discover_lan(folder_name, filenames, [])
    if folder_lan:
        result.account_no_lan = folder_lan

    import threading
    seen_page_hashes = set()
    hash_lock = threading.Lock()
    
    # ── Stage 0: The Intent Planner ──
    extraction_plan = ocr.analyze_prompt_intent(extraction_prompt) if hasattr(ocr, "analyze_prompt_intent") else {}
    
    # ── Stage 4: Per-document structured output ──
    t_ocr = time.perf_counter()
    for doc in docs_sorted:
        fp = doc.get("file_path", "")
        doc_id = doc.get("document_id", "")
        filename = doc.get("filename", "")
        is_shared_fcl = bool((doc.get("metadata") or {}).get("shared_fcl"))
        doc_type_hint = _classify_by_filename(filename) or ("foreclosure_notice" if is_shared_fcl else "document")
        doc_types_found.append(doc_type_hint)

        # Stage 1.5: Document Routing
        if extraction_plan.get("target_docs"):
            if doc_type_hint not in extraction_plan["target_docs"]:
                # The LLM planner says this document is not relevant to the prompt. Skip it!
                continue

        if not fp or not os.path.exists(fp):
            continue
            
        fsize = os.path.getsize(fp)
        
        try:
            img, load_err, page_count = _load_document(fp)
            if load_err or img is None:
                logger.log(lead_id, "doc_load", "FAIL", document_id=doc_id, reason=load_err, is_test=is_test)
                failed_docs += 1
                continue

            # Stage 1.6: Page Routing
            if extraction_plan.get("target_pages"):
                # Use ONLY the exact target pages if they are valid for this document
                prio_pages = [p for p in extraction_plan["target_pages"] if 0 <= p < page_count]
                if not prio_pages:
                    # Fallback to default if out of bounds or empty
                    prio_pages = _get_priority_pages_for_type(doc_type_hint, page_count)
            else:
                prio_pages = _get_priority_pages_for_type(doc_type_hint, page_count)
            
            doc_extractions = []
            
            import concurrent.futures
            
            def process_page(p_idx):
                try:
                    if p_idx == 0:
                        p_img = img
                    else:
                        rendered = _extract_pdf_pages(fp, [p_idx])
                        p_img = rendered[0] if rendered else None
                        
                    if not p_img: return None
                    
                    # 1. Rotation Correction (EXIF transpose)
                    from PIL import ImageOps
                    p_img = ImageOps.exif_transpose(p_img)
                    
                    # 2. Perceptual Hashing (Dedup)
                    # Resize to 8x8 grayscale and create a 64-bit string hash
                    thumb = p_img.convert("L").resize((8, 8))
                    pixels = list(thumb.getdata())
                    avg = sum(pixels) / 64.0
                    phash = "".join("1" if p > avg else "0" for p in pixels)
                    
                    with hash_lock:
                        if phash in seen_page_hashes:
                            return {"_status": "duplicate_discarded", "_page_number": p_idx + 1}
                        seen_page_hashes.add(phash)
                    
                    extracted = ocr.extract_data(p_img, document_type=doc_type_hint, prompt=extraction_prompt, extraction_plan=extraction_plan)
                    if extracted.get("_status") in ("blank_discarded", "duplicate_discarded", "boilerplate_discarded"):
                        return None
                        
                    extracted["_page_number"] = p_idx + 1
                    return extracted
                except Exception as e:
                    return {"_error": str(e), "_page_number": p_idx + 1}

            extracted_results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(process_page, p_idx) for p_idx in prio_pages]
                for fut in concurrent.futures.as_completed(futures):
                    res = fut.result()
                    if res: extracted_results.append(res)
                    
            extracted_results.sort(key=lambda x: x["_page_number"])
            
            for extracted in extracted_results:
                if "_error" in extracted:
                    continue
                    
                p_num = extracted["_page_number"]
                route = extracted.get("_ocr_route", "unknown")
                if route not in ocr_routes_used:
                    ocr_routes_used.append(route)
                    
                # Extract candidates
                candidates = extracted.get("_field_candidates", {})
                for field_name, cand_data in candidates.items():
                    fc = FieldCandidate(
                        value=cand_data.get("value"),
                        source=cand_data.get("source"),
                        confidence=cand_data.get("confidence", 0.0),
                        role_tag=field_name, # Use field name as role tag for simplicity here
                        document_type=doc_type_hint,
                        document_id=doc_id,
                        page_number=p_num,
                        model_used=route,
                        mismatch_flag=cand_data.get("mismatch_flag", False),
                        merge_priority=_get_merge_priority(cand_data.get("source", ""))
                    )
                    all_field_candidates.append(fc)
                    
                page_extractions.append({
                    "document_id": doc_id,
                    "filename": filename,
                    "page_number": p_num,
                    "fields": extracted.get("extracted_fields", extracted),
                    "ocr_route": route,
                    "mismatches": extracted.get("dual_source_mismatch_fields", []),
                    "raw_ocr_text": extracted.get("_raw_ocr_text", ""),
                    "ocr_confidence": extracted.get("_ocr_confidence", 0.0)
                })
                    
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

    # ── Stage 5: Cross-document merge ──
    stage_5_cross_document_merge(lead_id, all_field_candidates, result)
    
    # ── Stage 6: Validation pass ──
    val_flags = stage_6_validation_pass(result)
    for vf in val_flags:
        if vf.severity == "error" and f"error:{vf.field_name}" not in result.flags:
            result.flags.append(f"error:{vf.field_name}")
            
    # Stage 7 Human Review Queue is handled by the UI filtering for `mismatch:` or `error:` flags in `result.flags`.

    # Finalize
    final_status = "completed"
    if processed_docs == 0:
        final_status = "failed"
    elif failed_docs > 0:
        final_status = "partial"
    elif any(f.startswith("error:") for f in result.flags):
        final_status = "validation_failed"
    elif any(f.startswith("mismatch:") for f in result.flags):
        final_status = "needs_review"

    result.processing_status = final_status
    total_ms = round((time.perf_counter() - t_start) * 1000, 1)

    raw_extractions["page_extractions"] = page_extractions
    raw_extractions["custom_fields"] = getattr(result, "custom_fields", {})
    raw_extractions["validation_flags"] = [vars(f) for f in val_flags]
    raw_extractions["field_candidates"] = [vars(f) for f in all_field_candidates]

    raw_extractions["lead_metadata"] = {
        "lead_id": lead_id,
        "lead_name": lead_name,
        "folder_name": folder_name,
        "account_lan": result.account_no_lan,
        "applicant_name": result.applicant_name,
        "total_documents": total_docs,
        "processed_documents": processed_docs,
        "failed_documents": failed_docs,
        "phase_timings": phase_timings,
        "total_ms": total_ms,
    }

    # Save
    import json
    try:
        with pg.pool().connection() as c:
            c.execute(
                "UPDATE legal_leads SET extracted_data = %s WHERE lead_id = %s",
                (json.dumps(result.to_dict()), lead_id)
            )
    except: pass

    logger.save_lead_result(
        lead_id, result.to_dict(), processing_status=final_status,
        is_test=is_test, phase_timings=phase_timings,
        ocr_routes_used=ocr_routes_used,
        raw_extractions=raw_extractions
    )
    update_lead_doc_counts(lead_id)

    logger.log(lead_id, "lead_closed", "PASS",
               reason=f"Final: {final_status} ({processed_docs}/{total_docs})",
               ms=total_ms, is_test=is_test)

    return {"lead_id": lead_id, "processing_status": final_status,
            "total_documents": total_docs, "processed_documents": processed_docs,
            "failed_documents": failed_docs}
