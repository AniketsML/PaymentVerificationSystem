"""
HTTP API endpoints for the SARFAESI Legal workspace.
"""
from __future__ import annotations

import io
import os
import time
import zipfile
import json
from typing import Any, Dict, List

import pandas as pd
from flask import (
    Blueprint, Response, abort, jsonify, redirect,
    render_template, request, send_file, session,
)
from werkzeug.utils import secure_filename

from config import settings
from db import pg
from workspaces.legal.db import clear_legal_test_data, init_schema
from workspaces.legal.jobs import (
    batch_lead_progress, batch_leads, open_legal_work_count,
    scan_and_enqueue_folder,
)
from workspaces.legal.logger import PgLegalLeadLogger
from workspaces.legal.metrics import snapshot as legal_metrics_snapshot
from workspaces.legal.excel_generator import generate_workbook_bytes
from workspaces.legal.models import SARFAESILeadResult

legal_bp = Blueprint("legal", __name__)
logger = PgLegalLeadLogger()


def _model_info() -> dict:
    from config import runtime
    cfg = runtime.model_config()
    return {"model": cfg["model"], "url": cfg["url"],
            "stream": cfg["stream"], "workers": settings.WORKER_COUNT}


@legal_bp.route("/ws/legal")
@legal_bp.route("/ws/legal/lead/<lead_id>")
def legal_index(lead_id=None):
    session["workspace"] = "legal"
    return render_template(
        "legal.html",
        model=_model_info(),
        deep_lead=lead_id or "",
        user=(session.get("user", "") if settings.AUTH_USER else ""),
        workspace_id="legal",
        workspace_name="SARFAESI Recovery Console",
    )


@legal_bp.route("/api/legal/upload_folder", methods=["POST"])
def api_legal_upload_folder():
    is_test = request.form.get("test") in ("on", "true", "1")
    extraction_prompt = request.form.get("extraction_prompt", "").strip()
    batch_folder_name = f"upload_batch_{int(time.time())}"
    target_extract_dir = os.path.join(settings.UPLOAD_DIR, "legal_batches", batch_folder_name)
    os.makedirs(target_extract_dir, exist_ok=True)

    local_path = request.form.get("folder_path", "").strip()
    if local_path and os.path.isdir(local_path):
        result = scan_and_enqueue_folder(local_path, is_test=is_test, extraction_prompt=extraction_prompt)
        import worker; worker.start_pool()
        return jsonify(result)

    zip_file = request.files.get("zip_file") or request.files.get("file")
    if zip_file and zip_file.filename and zip_file.filename.lower().endswith(".zip"):
        zip_path = os.path.join(target_extract_dir, secure_filename(zip_file.filename))
        zip_file.save(zip_path)
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(target_extract_dir)
            os.remove(zip_path)
        except Exception as e:
            return jsonify({"error": f"Failed to extract zip file: {e}"}), 400
        result = scan_and_enqueue_folder(target_extract_dir, is_test=is_test, extraction_prompt=extraction_prompt)
        import worker; worker.start_pool()
        return jsonify(result)

    paths = request.form.getlist("paths") or request.form.getlist("paths[]")
    if not paths and request.form.get("file_paths"):
        try:
            paths = json.loads(request.form.get("file_paths"))
        except Exception:
            paths = []

    uploaded_files = request.files.getlist("files") or request.files.getlist("files[]")
    if not uploaded_files and request.files.get("file"):
        uploaded_files = [request.files.get("file")]

    if uploaded_files and any(f.filename for f in uploaded_files):
        custom_lead = request.form.get("lead_name", "").strip() or "Lead_Direct_Dossier"
        has_subfolder = False
        if paths and any("/" in str(p).replace("\\", "/") for p in paths):
            has_subfolder = True
        elif any("/" in f.filename.replace("\\", "/") for f in uploaded_files if f.filename):
            has_subfolder = True

        for i, f in enumerate(uploaded_files):
            if not f.filename:
                continue
            rel_path = ""
            if paths and i < len(paths) and paths[i]:
                rel_path = str(paths[i])
            else:
                rel_path = f.filename
            clean_rel = rel_path.replace("\\", "/").lstrip("/")
            if not has_subfolder:
                dest = os.path.join(target_extract_dir, custom_lead, clean_rel)
            else:
                dest = os.path.join(target_extract_dir, clean_rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            f.save(dest)

        result = scan_and_enqueue_folder(target_extract_dir, is_test=is_test, extraction_prompt=extraction_prompt)
        import worker; worker.start_pool()
        return jsonify(result)

    return jsonify({"error": "No valid zip file, folder upload, or folder path provided."}), 400


@legal_bp.route("/api/legal/load_demo_dossier", methods=["POST"])
def api_legal_load_demo():
    from workspaces.legal.demo_data import create_sample_dossier
    batch_folder_name = f"demo_batch_{int(time.time())}"
    target_extract_dir = os.path.join(settings.UPLOAD_DIR, "legal_batches", batch_folder_name)
    os.makedirs(target_extract_dir, exist_ok=True)
    create_sample_dossier(target_extract_dir, lan="LAN987654321", borrower="Ramesh Chandra Sharma")
    is_test = request.form.get("test") in ("on", "true", "1") or request.args.get("scope") == "test"
    result = scan_and_enqueue_folder(target_extract_dir, is_test=is_test)
    import worker; worker.start_pool()
    return jsonify(result)


@legal_bp.route("/api/legal/batch/<batch_id>/action", methods=["POST"])
def api_legal_batch_action(batch_id):
    try:
        data = request.json or {}
        action = data.get("action")
        with pg.pool().connection() as c:
            if action == "pause":
                c.execute("UPDATE legal_leads SET status='paused' WHERE batch_id=%s AND status='pending'", (batch_id,))
            elif action == "resume":
                c.execute("UPDATE legal_leads SET status='pending' WHERE batch_id=%s AND status='paused'", (batch_id,))
            elif action == "cancel":
                c.execute("UPDATE legal_leads SET status='failed' WHERE batch_id=%s AND status IN ('pending', 'paused')", (batch_id,))
            else:
                return jsonify({"error": "invalid action"}), 400
        return jsonify({"success": True, "action": action})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@legal_bp.route("/api/legal/batch/<batch_id>", methods=["GET"])
def api_legal_batch(batch_id):
    prog = batch_lead_progress(batch_id)
    leads = batch_leads(batch_id, limit=800)
    prog["leads"] = leads
    prog["open_work"] = open_legal_work_count()
    return jsonify(prog)


@legal_bp.route("/api/legal/stats")
def api_legal_stats():
    scope = request.args.get("scope", "real")
    return jsonify({
        "counts": logger.status_counts(scope=scope),
        "document_types": logger.document_type_counts(scope=scope),
        "scope": scope,
        "model": _model_info(),
        "workspace": "legal",
    })


@legal_bp.route("/api/legal/leads")
def api_legal_leads():
    status = request.args.get("status", "all")
    q = request.args.get("q", "").strip()
    scope = request.args.get("scope", "real")
    limit = min(int(request.args.get("limit", 300)), 2000)
    rows = logger.query_leads(status=status, q=q, limit=limit, scope=scope)
    return jsonify(rows)


@legal_bp.route("/api/legal/lead/<lead_id>")
def api_legal_lead_detail(lead_id):
    j = logger.get_lead_journey(lead_id)
    if not j["journey"] and not j["final"] and not j["lead"]:
        abort(404)
    j["review"] = logger.latest_review(lead_id)
    j["review_history"] = logger.review_history(lead_id)
    return jsonify(j)


@legal_bp.route("/api/legal/document/<doc_id>/file")
def api_legal_doc_file(doc_id):
    with pg.pool().connection() as c:
        row = c.execute("SELECT file_path,filename,file_type FROM legal_lead_documents WHERE document_id=%s", (doc_id,)).fetchone()
    if not row or not row["file_path"] or not os.path.exists(row["file_path"]):
        abort(404)
    return send_file(row["file_path"], download_name=row["filename"])


@legal_bp.route("/api/legal/document/<doc_id>/page/<int:page_num>")
def api_legal_doc_page_preview(doc_id, page_num):
    """Render a specific document page as an image for visual inspection."""
    with pg.pool().connection() as c:
        row = c.execute("SELECT file_path, filename, file_type FROM legal_lead_documents WHERE document_id=%s", (doc_id,)).fetchone()
    if not row or not row["file_path"] or not os.path.exists(row["file_path"]):
        abort(404)
    file_path = row["file_path"]
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        try:
            import fitz
            with fitz.open(file_path) as doc:
                p_idx = max(0, min(page_num - 1, doc.page_count - 1))
                page = doc[p_idx]
                pix = page.get_pixmap(matrix=fitz.Matrix(130/72, 130/72))
                img_data = pix.tobytes("png")
            return Response(img_data, mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})
        except Exception as e:
            return jsonify({"error": f"Failed to render PDF page: {e}"}), 500
    elif ext in ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif', '.tif', '.tiff'):
        return send_file(file_path)
    else:
        abort(415)


@legal_bp.route("/api/legal/lead/<lead_id>/review", methods=["POST"])
def api_legal_review(lead_id):
    body = request.get_json(force=True) or {}
    decision = body.get("decision", "confirmed")
    corrected_data = body.get("corrected_data") or {}
    reviewer = (body.get("reviewer") or "").strip() or (session.get("user", "") or "reviewer")
    note = (body.get("note") or "").strip()
    j = logger.get_lead_journey(lead_id)
    if not j["final"] and not j["lead"]:
        abort(404)
    system_status = (j.get("final") or {}).get("processing_status") or (j.get("lead") or {}).get("status")
    rec = logger.save_review(
        lead_id, system_status, decision,
        corrected_data=corrected_data, reviewer=reviewer, note=note,
        is_test=bool((j.get("final") or {}).get("is_test") or (j.get("lead") or {}).get("is_test")))
    return jsonify(rec)


@legal_bp.route("/api/legal/observability")
def api_legal_observability():
    scope = "test" if request.args.get("scope") == "test" else "real"
    return jsonify(legal_metrics_snapshot(scope=scope))


@legal_bp.route("/api/legal/clear_test", methods=["POST"])
def api_legal_clear_test():
    batch_id = request.args.get("batch_id") or (request.form.get("batch_id") if request.form else None)
    return jsonify(clear_legal_test_data(batch_id=batch_id or None))


@legal_bp.route("/api/legal/download/<batch_id>")
def api_legal_download(batch_id):
    leads = batch_leads(batch_id, limit=100000)
    fields = SARFAESILeadResult.EXCEL_FIELD_MAP
    rows = []
    for r in leads:
        row = {f: r.get(f, "") for f in fields}
        row["lead_id"] = r.get("lead_id", "")
        row["status"] = r.get("result_status") or r.get("status", "")
        rows.append(row)
    buf = io.BytesIO(pd.DataFrame(rows).to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"))
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name=f"sarfaesi_leads_{batch_id}.csv")


@legal_bp.route("/api/legal/download/<batch_id>/excel")
def api_legal_download_excel(batch_id):
    leads = batch_leads(batch_id, limit=100000)
    wb_bytes = generate_workbook_bytes(leads)
    buf = io.BytesIO(wb_bytes)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"sarfaesi_leads_{batch_id}.xlsx")


@legal_bp.route("/logs/legal/<lead_id>")
def legal_logs(lead_id):
    return jsonify(logger.get_lead_journey(lead_id))
