"""
Web app + API (PostgreSQL + durable job queue).

Upload no longer processes synchronously - it ENQUEUES one job per lead and an
in-process worker pool drains the queue. The UI polls batch progress. A restart
mid-run loses nothing: unfinished jobs stay queued and resume automatically.

  GET  /                       SPA (also /lead/<id> deep-link)
  POST /api/enqueue            upload CSV/Excel -> enqueue jobs -> {batch_id, enqueued, skipped}
  GET  /api/batch/<batch_id>   live batch progress + per-lead rows
  GET  /api/stats              status + payment-method breakdown
  GET  /api/leads              filtered/paginated results (?status=&q=)
  GET  /api/lead/<lead_id>     one lead: final + full journey + image
  GET  /download/<batch_id>    results CSV for a batch
  GET  /uploaded/<name>        serve a locally-uploaded image
  GET  /logs/<lead_id>         raw journey (JSON)
  POST /api/verify             verify one record synchronously (JSON) - integrations

Run:  python -m app.server      (workers start automatically)
"""
import io
import os
import sys
import time

import pandas as pd
from flask import (Flask, request, send_file, jsonify, render_template, abort, url_for)
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings
from db import pg
from observability.pg_logger import PgLeadLogger
from observability.pg_dedup import PaymentDedup
from ocr.medha_client import MedhaVisionOCR, PrecomputedOCR
from pipeline.orchestrator import process_lead
from pipeline import jobs
from run_batch import outcome_text
import worker

app = Flask(__name__)
pg.init_schema()
logger = PgLeadLogger()
dedup = PaymentDedup()
worker.start_pool()          # in-process worker pool drains the queue


def _model_info() -> dict:
    return {"model": settings.VISION_MODEL, "url": settings.VISION_API_URL,
            "stream": settings.VISION_STREAM, "workers": settings.WORKER_COUNT}


def _as_obj(v):
    """JSONB comes back as dict already; tolerate str too."""
    if isinstance(v, (dict, list)):
        return v
    import json
    try:
        return json.loads(v or "{}")
    except Exception:
        return {}


def _outcome_text(status, outcome):
    return outcome_text(status, _as_obj(outcome))


# ── SPA shell ─────────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/lead/<lead_id>")
def index(lead_id=None):
    return render_template("index.html", model=_model_info(), deep_lead=lead_id or "")


# ── enqueue (upload) ──────────────────────────────────────────────────────────
@app.route("/api/enqueue", methods=["POST"])
def api_enqueue():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file uploaded"}), 400
    precomputed = request.form.get("precomputed") in ("on", "true", "1")
    image_col = request.form.get("image_col", "payment_document")
    id_col = request.form.get("id_col") or None
    image_root = request.form.get("image_root", "")

    fn = (f.filename or "").lower()
    if fn.endswith((".xlsx", ".xls")):
        df = pd.read_excel(f, dtype=str).fillna("")
    else:
        df = pd.read_csv(f, dtype=str).fillna("")
    rows = df.to_dict("records")
    result = jobs.enqueue_rows(rows, image_col=image_col, id_col=id_col,
                               image_root=image_root, precomputed=precomputed)
    worker.start_pool()          # ensure workers are running
    return jsonify(result)


@app.route("/api/verify_image", methods=["POST"])
def api_verify_image():
    """Single-image quick test - runs synchronously (not queued) for instant feedback."""
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"error": "no image uploaded"}), 400
    saved = os.path.join(settings.UPLOAD_DIR, f"{int(time.time())}_{secure_filename(f.filename)}")
    f.save(saved)
    row = {
        "institute_name": request.form.get("institute_name", "").strip(),
        "payment_amount": request.form.get("payment_amount", "").strip(),
        "payment_date": request.form.get("payment_date", "").strip(),
        "loan_account_number": request.form.get("loan_account_number", "").strip(),
        "transaction_id": request.form.get("transaction_id", "").strip(),
    }
    lead_id = request.form.get("lead_id", "").strip() or f"IMG-{int(time.time())}"
    res = process_lead(lead_id, row["institute_name"], saved, row,
                       MedhaVisionOCR(), logger, skip_image_qc=False, dedup=dedup)
    return jsonify({"lead_id": lead_id, "verification_status": res["verification_status"]})


@app.route("/api/batch/<batch_id>")
def api_batch(batch_id):
    prog = jobs.batch_progress(batch_id)
    leads = jobs.batch_leads(batch_id, limit=800)
    for r in leads:
        r["outcome_text"] = _outcome_text(r.get("verification_status"), r.get("outcome"))
        r.pop("outcome", None)
    prog["leads"] = leads
    prog["open_work"] = jobs.open_work_count()
    return jsonify(prog)


# ── dashboard / table data ────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    return jsonify({"counts": logger.status_counts(),
                    "methods": logger.method_counts(),
                    "model": _model_info()})


@app.route("/api/leads")
def api_leads():
    status = request.args.get("status", "all")
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 300)), 2000)
    rows = logger.query_results(status=status, q=q, limit=limit)
    for r in rows:
        r["outcome_text"] = _outcome_text(r.get("verification_status"), r.get("outcome"))
    return jsonify(rows)


@app.route("/api/lead/<lead_id>")
def api_lead(lead_id):
    j = logger.get_lead_journey(lead_id)
    if not j["journey"] and not j["final"]:
        abort(404)
    j["extracted"] = _as_obj(j["final"].get("extracted")) if j["final"] else {}
    j["outcome"] = _as_obj(j["final"].get("outcome")) if j["final"] else {}
    image_source = ""
    for e in j["journey"]:
        src = (e.get("data") or {}).get("image_source")
        if src:
            image_source = src
            break
    image_url = ""
    if image_source.lower().startswith("http"):
        image_url = image_source
    elif image_source and os.path.dirname(os.path.abspath(image_source)) == os.path.abspath(settings.UPLOAD_DIR):
        image_url = url_for("uploaded", name=os.path.basename(image_source))
    j["image_source"] = image_source
    j["image_url"] = image_url
    j["csv_row"] = jobs.get_row(lead_id)      # exact input row for side-by-side review
    return jsonify(j)


# ── downloads / files ─────────────────────────────────────────────────────────
@app.route("/download/<batch_id>")
def download(batch_id):
    leads = jobs.batch_leads(batch_id, limit=100000)
    rows = [{"lead_id": r["lead_id"], "lender": r["lender"],
             "verification_status": r.get("verification_status") or r.get("job_status"),
             "outcome": _outcome_text(r.get("verification_status"), r.get("outcome")),
             "payment_method": r.get("payment_method") or ""} for r in leads]
    order = {"manual_review": 0, "unverified": 1, "non_document": 2, "duplicate": 3, "verified": 4}
    rows.sort(key=lambda x: order.get(x["verification_status"], -1))
    buf = io.BytesIO(pd.DataFrame(rows).to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"))
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name=f"{batch_id}.csv")


@app.route("/uploaded/<name>")
def uploaded(name):
    path = os.path.join(settings.UPLOAD_DIR, secure_filename(name))
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


# ── raw JSON API (integrations / Metabase) ────────────────────────────────────
@app.route("/logs/<lead_id>")
def logs(lead_id):
    return jsonify(logger.get_lead_journey(lead_id))


@app.route("/api/results")
def results():
    return jsonify(logger.all_results())


@app.route("/api/verify", methods=["POST"])
def api_verify():
    body = request.get_json(force=True)
    row = body.get("record", {})
    lead_id = str(body.get("lead_id", "API-LEAD"))
    image_path = body.get("image_path", "")
    precomputed = bool(body.get("precomputed", False))
    ocr = PrecomputedOCR() if precomputed else MedhaVisionOCR()
    res = process_lead(lead_id, row.get("institute_name", ""), image_path, row,
                       ocr, logger, skip_image_qc=precomputed, dedup=dedup)
    return jsonify(res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
