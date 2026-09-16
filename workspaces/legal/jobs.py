"""
Durable job queue for SARFAESI Legal Lead Document Processing.
Enhanced with Account LAN discovery from folder names and filenames.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any, Dict, List, Optional
from psycopg.types.json import Jsonb

from config import settings
from db import pg
from workspaces.legal.db import init_schema

SUPPORTED_EXTENSIONS = {
    '.pdf', '.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.webp',
    '.gif', '.docx', '.doc', '.xlsx', '.xls',
}

_LAN_RE = re.compile(settings.ACCOUNT_LAN_PATTERN, re.IGNORECASE)


def _generate_lead_id(folder_name: str, idx: int) -> str:
    clean = folder_name.strip()
    if clean:
        h = hashlib.sha256(clean.encode('utf-8')).hexdigest()[:6].upper()
        return f"LEAD-{h}-{idx:04d}"
    return f"LEAD-{idx:04d}-{int(time.time()) % 100000}"


def _generate_doc_id(lead_id: str, filename: str) -> str:
    raw = f"{lead_id}|{filename}"
    h = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8].upper()
    return f"DOC-{h}"


def _discover_lan(folder_name: str, filenames: List[str]) -> str:
    """Discover Account LAN from folder name or filenames."""
    for source in [folder_name] + filenames:
        m = _LAN_RE.search(source or "")
        if m:
            return m.group(1)
    return ""


# Folder names that are treated as a SHARED FCL/Foreclosure pool,
# not as individual leads.  Files inside are injected into every LAN
# lead that has no FCL document of its own.
_SHARED_FCL_FOLDER_NAMES = {
    'fcl', 'fcl_docs', 'fcl_documents', 'foreclosure', 'foreclosures',
    'fore_closure', 'foreclosure_docs', 'foreclosure_notices',
    'demand_notices', 'fcl_notice', 'fcl_notices',
}


def _is_shared_fcl_folder(name: str) -> bool:
    return name.strip().lower() in _SHARED_FCL_FOLDER_NAMES


def _lead_has_fcl_doc(file_paths: List[str]) -> bool:
    """Return True if any file in the lead folder looks like an FCL document."""
    for fp in file_paths:
        fn = os.path.basename(fp).lower()
        if any(k in fn for k in ('fcl', 'foreclosure', 'demand_notice', 'demand notice')):
            return True
    return False


def _find_shared_fcl_docs(root_path: str) -> List[str]:
    """Return paths of all supported documents inside any shared FCL folder (at root or within subdirectories)."""
    shared: List[str] = []
    try:
        for cur_dir, dirs, files in os.walk(root_path):
            dir_name = os.path.basename(cur_dir.rstrip("/\\"))
            if _is_shared_fcl_folder(dir_name):
                for f in sorted(files):
                    fp = os.path.join(cur_dir, f)
                    if os.path.isfile(fp) and os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS:
                        shared.append(fp)
    except Exception:
        pass
    return shared


def _discover_lead_directories(root_path: str) -> List[Dict[str, Any]]:
    """Recursively discover lead directories in root_path.
    A lead directory is any directory containing supported document files.
    Shared FCL/Foreclosure folders at the root level are excluded from leads —
    their documents are injected into leads by scan_and_enqueue_folder instead.
    """
    leads: List[Dict[str, Any]] = []
    if not os.path.isdir(root_path):
        return leads

    immediate_subdirs = [
        os.path.join(root_path, d) for d in sorted(os.listdir(root_path))
        if os.path.isdir(os.path.join(root_path, d))
        and not d.startswith('.') and d != '__MACOSX'
        and not _is_shared_fcl_folder(d)
    ]
    immediate_files = [
        os.path.join(root_path, f) for f in sorted(os.listdir(root_path))
        if os.path.isfile(os.path.join(root_path, f)) and os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
    ]

    # If root has files directly and no subdirectories:
    if immediate_files and not immediate_subdirs:
        leads.append({
            "name": os.path.basename(root_path.rstrip("/\\")) or "Lead_Direct",
            "path": root_path,
            "files": immediate_files,
        })
        return leads

    # If root has only 1 subdirectory and NO files (e.g. single wrapper folder), unwrap it
    if len(immediate_subdirs) == 1 and not immediate_files:
        child = immediate_subdirs[0]
        child_subdirs = [
            os.path.join(child, d) for d in sorted(os.listdir(child))
            if os.path.isdir(os.path.join(child, d)) and not d.startswith('.') and d != '__MACOSX'
            and not _is_shared_fcl_folder(d)
        ]
        child_files = [
            os.path.join(child, f) for f in sorted(os.listdir(child))
            if os.path.isfile(os.path.join(child, f)) and os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
        ]
        if child_subdirs:
            return _discover_lead_directories(child)
        elif child_files:
            leads.append({
                "name": os.path.basename(child.rstrip("/\\")),
                "path": child,
                "files": child_files,
            })
            return leads

    # Walk directories to find all folders that directly hold document files
    for cur_dir, dirs, files in os.walk(root_path):
        dirs[:] = [
            d for d in dirs
            if not d.startswith('.') and d != '__MACOSX'
            and not _is_shared_fcl_folder(d)
        ]
        if cur_dir == root_path:
            continue
        valid_files = [
            os.path.join(cur_dir, f) for f in sorted(files)
            if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
        ]
        if valid_files:
            leads.append({
                "name": os.path.basename(cur_dir.rstrip("/\\")),
                "path": cur_dir,
                "files": valid_files,
            })

    if not leads and immediate_files:
        leads.append({
            "name": os.path.basename(root_path.rstrip("/\\")) or "Lead_Direct",
            "path": root_path,
            "files": immediate_files,
        })

    return leads


def scan_and_enqueue_folder(root_path: str, is_test: bool = False, extraction_prompt: str = "") -> Dict[str, Any]:
    """Scan uploaded folder structure and enqueue leads and their documents.

    Shared FCL/Foreclosure folder:
      If the batch root contains a folder named 'FCL', 'Foreclosure', etc., its
      documents are NOT treated as a separate lead.  Instead, they are injected as
      documents into every LAN lead that does NOT already have its own FCL file.
      The pipeline recognises them via metadata={'shared_fcl': True} and filters
      extractions to only the rows/pages that match the lead's own LAN.
    """
    init_schema()
    batch_id = f"batch-legal-{int(time.time())}"
    leads_enqueued, docs_enqueued, skipped = 0, 0, 0

    lead_folders = _discover_lead_directories(root_path)

    # Detect shared FCL docs at batch root (e.g. root/FCL/*.pdf)
    shared_fcl_files = _find_shared_fcl_docs(root_path)

    def _enqueue_doc(c, lead_id: str, fpath: str, is_test: bool,
                     shared_fcl: bool = False) -> None:
        """Insert one document into legal_lead_documents."""
        nonlocal docs_enqueued
        fname = os.path.basename(fpath)
        ext = os.path.splitext(fname)[1].lower().lstrip('.')
        doc_id = _generate_doc_id(lead_id, fname)
        fsize = os.path.getsize(fpath) if os.path.exists(fpath) else 0
        pcnt = 1
        if ext == 'pdf' and os.path.exists(fpath):
            try:
                import fitz
                with fitz.open(fpath) as d_tmp:
                    pcnt = d_tmp.page_count
            except Exception:
                pcnt = 1
        meta = {'shared_fcl': True} if shared_fcl else None
        from psycopg.types.json import Jsonb
        c.execute(
            "INSERT INTO legal_lead_documents(document_id, lead_id, filename, "
            "file_path, file_type, file_size_bytes, page_count, processing_status, is_test, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s) "
            "ON CONFLICT (document_id) DO UPDATE SET "
            "file_path = EXCLUDED.file_path, "
            "file_size_bytes = EXCLUDED.file_size_bytes, "
            "page_count = EXCLUDED.page_count, "
            "processing_status = 'pending', "
            "is_test = EXCLUDED.is_test, "
            "metadata = COALESCE(EXCLUDED.metadata, legal_lead_documents.metadata);",
            (doc_id, lead_id, fname, fpath, ext, fsize, pcnt, is_test,
             Jsonb(meta) if meta else None)
        )
        docs_enqueued += 1

    with pg.pool().connection() as c:
        for idx, lf in enumerate(lead_folders, start=1):
            folder_name = lf["name"]
            folder_path = lf["path"]
            lead_id = _generate_lead_id(folder_name, idx)
            doc_files = lf.get("files", [])

            if not doc_files:
                skipped += 1
                continue

            filenames = [os.path.basename(f) for f in doc_files]
            account_lan = _discover_lan(folder_name, filenames)
            
            if not account_lan:
                # User requested: only treat folders with a LAN as leads.
                # If it doesn't have a LAN, it might be a shared folder (like FCL) that got misnamed.
                # We'll add its documents to the shared FCL pool and skip creating a lead for it.
                shared_fcl_files.extend(doc_files)
                skipped += 1
                continue

            # Re-determine inject_fcl now that shared_fcl_files might have grown
            if shared_fcl_files and not _lead_has_fcl_doc(doc_files):
                matching_fcl = [
                    f for f in shared_fcl_files
                    if account_lan and account_lan.lower() in os.path.basename(f).lower()
                ]
                inject_fcl = matching_fcl if matching_fcl else (shared_fcl_files if len(shared_fcl_files) == 1 else [])
            else:
                inject_fcl = []
            total_doc_count = len(doc_files) + len(inject_fcl)

            lead_name = folder_name
            for prefix in ('Lead_', 'LEAD_', 'lead_'):
                if lead_name.startswith(prefix):
                    lead_name = lead_name[len(prefix):]
            if account_lan and lead_name.startswith(account_lan):
                lead_name = lead_name[len(account_lan):]
            lead_name = lead_name.strip('0123456789_- ').replace('_', ' ') or folder_name

            res = c.execute(
                "INSERT INTO legal_leads(lead_id, batch_id, folder_name, lead_name, "
                "folder_path, account_lan, total_documents, status, is_test, extraction_prompt) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s) "
                "ON CONFLICT (lead_id) DO UPDATE SET "
                "batch_id = EXCLUDED.batch_id, "
                "folder_path = EXCLUDED.folder_path, "
                "lead_name = EXCLUDED.lead_name, "
                "account_lan = EXCLUDED.account_lan, "
                "total_documents = EXCLUDED.total_documents, "
                "status = 'draft', "
                "is_test = EXCLUDED.is_test, "
                "extraction_prompt = EXCLUDED.extraction_prompt, "
                "updated_at = now() RETURNING lead_id;",
                (lead_id, batch_id, folder_name, lead_name, folder_path,
                 account_lan, total_doc_count, is_test, extraction_prompt)
            ).fetchone()

            if not res:
                skipped += 1
                continue
            leads_enqueued += 1

            # Enqueue the lead's own documents
            for fpath in doc_files:
                _enqueue_doc(c, lead_id, fpath, is_test, shared_fcl=False)

            # Inject shared FCL documents (marked so the pipeline filters by LAN)
            for fpath in inject_fcl:
                _enqueue_doc(c, lead_id, fpath, is_test, shared_fcl=True)
                
            c.execute("UPDATE legal_leads SET status = 'pending' WHERE lead_id = %s", (lead_id,))

    return {"batch_id": batch_id, "leads_enqueued": leads_enqueued,
            "documents_enqueued": docs_enqueued, "skipped_empty_folders": skipped,
            "shared_fcl_files_found": len(shared_fcl_files),
            "workspace": "legal"}



def claim_one_lead(lease_seconds: int = 600) -> Optional[Dict[str, Any]]:
    init_schema()
    sql = """
    UPDATE legal_leads SET status = 'processing', updated_at = now()
    WHERE lead_id = (
        SELECT lead_id FROM legal_leads WHERE status = 'pending'
        ORDER BY created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1
    ) RETURNING *;
    """
    with pg.pool().connection() as c:
        row = c.execute(sql).fetchone()
    if not row:
        return None
    result = dict(row)
    with pg.pool().connection() as c:
        docs = c.execute("SELECT * FROM legal_lead_documents WHERE lead_id=%s ORDER BY filename",
                         (result["lead_id"],)).fetchall()
    result["documents"] = [dict(d) for d in docs]
    return result


def complete_lead(lead_id: str, status: str = "completed") -> None:
    with pg.pool().connection() as c:
        c.execute("UPDATE legal_leads SET status=%s, updated_at=now() WHERE lead_id=%s", (status, lead_id))


def fail_lead(lead_id: str, error: str) -> None:
    with pg.pool().connection() as c:
        c.execute("UPDATE legal_leads SET status='failed', updated_at=now() WHERE lead_id=%s", (lead_id,))


def update_document_status(document_id: str, status: str, document_type: str = None,
                           ocr_text: str = None, extracted_data: Dict[str, Any] = None,
                           confidence_score: float = None, error_message: str = None,
                           ocr_route: str = None, phase: str = None,
                           property_tier: int = None, page_count: int = None,
                           file_size_bytes: int = None, metadata: Dict[str, Any] = None) -> None:
    sets = ["processing_status=%s", "updated_at=now()"]
    params: list = [status]
    if document_type is not None:
        sets.append("document_type=%s"); params.append(document_type)
    if ocr_text is not None:
        sets.append("ocr_text=%s"); params.append(ocr_text)
    if extracted_data is not None:
        sets.append("extracted_data=%s"); params.append(Jsonb(extracted_data))
    if confidence_score is not None:
        sets.append("confidence_score=%s"); params.append(confidence_score)
    if error_message is not None:
        sets.append("error_message=%s"); params.append(error_message)
    if ocr_route is not None:
        sets.append("ocr_route=%s"); params.append(ocr_route)
    if phase is not None:
        sets.append("phase=%s"); params.append(phase)
    if property_tier is not None:
        sets.append("property_tier=%s"); params.append(property_tier)
    if page_count is not None:
        sets.append("page_count=%s"); params.append(page_count)
    if file_size_bytes is not None:
        sets.append("file_size_bytes=%s"); params.append(file_size_bytes)
    if metadata is not None:
        sets.append("metadata=%s"); params.append(Jsonb(metadata))
    params.append(document_id)
    with pg.pool().connection() as c:
        c.execute(f"UPDATE legal_lead_documents SET {', '.join(sets)} WHERE document_id=%s", params)


def update_lead_doc_counts(lead_id: str) -> None:
    with pg.pool().connection() as c:
        c.execute(
            "UPDATE legal_leads SET "
            "processed_documents = (SELECT count(*) FROM legal_lead_documents WHERE lead_id=%s AND processing_status IN ('processed', 'completed')), "
            "failed_documents = (SELECT count(*) FROM legal_lead_documents WHERE lead_id=%s AND processing_status='failed'), "
            "updated_at = now() WHERE lead_id=%s",
            (lead_id, lead_id, lead_id))


def batch_lead_progress(batch_id: str) -> Dict[str, Any]:
    with pg.pool().connection() as c:
        rows = c.execute("SELECT status,count(*) as n FROM legal_leads WHERE batch_id=%s GROUP BY status", (batch_id,)).fetchall()
        total = c.execute("SELECT count(*) as cnt FROM legal_leads WHERE batch_id=%s", (batch_id,)).fetchone()
        doc_total = c.execute(
            "SELECT count(*) as cnt FROM legal_lead_documents WHERE lead_id IN (SELECT lead_id FROM legal_leads WHERE batch_id=%s)", (batch_id,)).fetchone()
        doc_done = c.execute(
            "SELECT count(*) as cnt FROM legal_lead_documents WHERE processing_status='processed' AND lead_id IN (SELECT lead_id FROM legal_leads WHERE batch_id=%s)", (batch_id,)).fetchone()
    statuses = {r["status"]: r["n"] for r in rows}
    return {"batch_id": batch_id, "total_leads": total["cnt"] if total else 0,
            "leads_pending": statuses.get("pending", 0), "leads_processing": statuses.get("processing", 0),
            "leads_completed": statuses.get("completed", 0), "leads_partial": statuses.get("partial", 0),
            "leads_failed": statuses.get("failed", 0), "leads_paused": statuses.get("paused", 0),
            "total_documents": doc_total["cnt"] if doc_total else 0,
            "documents_processed": doc_done["cnt"] if doc_done else 0}


def batch_leads(batch_id: str, limit: int = 800) -> List[Dict[str, Any]]:
    with pg.pool().connection() as c:
        rows = c.execute(
            "SELECT l.lead_id, l.folder_name, l.lead_name, l.account_lan, "
            "l.total_documents, l.processed_documents, l.failed_documents, l.status, "
            "r.processing_status as result_status, r.summary, r.confidence_score, "
            "r.account_no_lan, r.applicant_name, r.sanction_amount, r.tos, "
            "r.property_verification_status, r.phase_timings, r.flags "
            "FROM legal_leads l LEFT JOIN legal_lead_results r ON r.lead_id = l.lead_id "
            "WHERE l.batch_id = %s ORDER BY l.created_at ASC LIMIT %s",
            (batch_id, limit)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for fld in ("sanction_amount", "tos"):
            if d.get(fld) is not None:
                d[fld] = float(d[fld])
        result.append(d)
    return result


def open_legal_work_count() -> int:
    with pg.pool().connection() as c:
        r = c.execute("SELECT count(*) as cnt FROM legal_leads WHERE status IN ('pending','processing')").fetchone()
    return r["cnt"] if r else 0
