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
import hmac
import io
import os
import sys
import tempfile
import time

import pandas as pd
from flask import (Flask, request, send_file, jsonify, render_template, abort, url_for,
                   Response)
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings
from db import pg
from observability.pg_logger import PgLeadLogger
from observability.pg_dedup import PaymentDedup
from observability import metrics
from ocr.medha_client import MedhaVisionOCR, PrecomputedOCR
from pipeline.orchestrator import process_lead
from pipeline import jobs
from run_batch import outcome_text
import worker

app = Flask(__name__)
# cap upload size so a huge file can't OOM the process (default 64 MB, override via env)
app.config["MAX_CONTENT_LENGTH"] = settings.MAX_UPLOAD_MB * 1024 * 1024
pg.init_schema()


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": f"file too large — max {settings.MAX_UPLOAD_MB} MB"}), 413
try:
    _purged = pg.purge_expired_test_data(settings.TEST_TTL_DAYS)
    if _purged.get("purged"):
        sys.stderr.write(f"[server] purged {_purged['purged']} expired test lead(s)\n")
    _orph = pg.sweep_orphan_reviews()
    if _orph:
        sys.stderr.write(f"[server] swept {_orph} orphaned review(s) (lead no longer exists)\n")
    _cache = pg.purge_expired_cache(settings.OCR_CACHE_TTL_DAYS)
    if _cache:
        sys.stderr.write(f"[server] purged {_cache} stale OCR-cache entr(ies)\n")
except Exception as _e:  # never block boot on housekeeping
    sys.stderr.write(f"[server] test-data purge skipped: {_e}\n")
logger = PgLeadLogger()
dedup = PaymentDedup()
worker.start_pool()          # in-process worker pool drains the queue


# ── optional HTTP Basic auth ──────────────────────────────────────────────────
_AUTH_ON = bool(settings.AUTH_USER and settings.AUTH_PASS)
if not _AUTH_ON:
    sys.stderr.write("[server] WARNING: dashboard is UNAUTHENTICATED — anyone who can "
                     "reach this host sees all leads. Set PV_AUTH_USER/PV_AUTH_PASS to lock it.\n")


@app.before_request
def _require_auth():
    if not _AUTH_ON or request.path == "/health":
        return
    a = request.authorization
    if a and hmac.compare_digest(a.username or "", settings.AUTH_USER) \
         and hmac.compare_digest(a.password or "", settings.AUTH_PASS):
        return
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="Payment Verification"'})


def _model_info() -> dict:
    return {"model": settings.VISION_MODEL, "url": settings.VISION_API_URL,
            "stream": settings.VISION_STREAM, "workers": settings.WORKER_COUNT}


class _NullLogger:
    """No-op logger for ephemeral single-image tests — runs the real pipeline but writes
    nothing to the DB (no events, no result row)."""
    def log(self, *a, **k): pass
    def save_result(self, *a, **k): pass


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
    is_test = request.form.get("test") in ("on", "true", "1")
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
                               image_root=image_root, is_test=is_test)
    worker.start_pool()          # ensure workers are running
    return jsonify(result)


@app.route("/api/verify_image", methods=["POST"])
def api_verify_image():
    """Single-image TEST — runs the real pipeline synchronously and returns the full
    breakdown (extraction, verdict, reason). Ephemeral: writes nothing to the DB and
    never touches the duplicate ledger, so you can trial receipts freely."""
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"error": "no image uploaded"}), 400
    row = {
        "institute_name": request.form.get("institute_name", "").strip(),
        "payment_amount": request.form.get("payment_amount", "").strip(),
        "payment_date": request.form.get("payment_date", "").strip(),
        "loan_account_number": request.form.get("loan_account_number", "").strip(),
        "transaction_id": request.form.get("transaction_id", "").strip(),
    }
    lead_id = request.form.get("lead_id", "").strip() or f"TEST-{int(time.time())}"
    # truly ephemeral: a temp file that is deleted right after — nothing lingers on disk,
    # and the browser shows the user's own selected image client-side.
    suffix = os.path.splitext(secure_filename(f.filename))[1] or ".jpg"
    fd, tmp = tempfile.mkstemp(prefix="pvtest_", suffix=suffix)
    os.close(fd)
    try:
        f.save(tmp)
        res = process_lead(lead_id, row["institute_name"], tmp, row,
                           MedhaVisionOCR(), _NullLogger(), skip_image_qc=False,
                           dedup=None, is_test=True)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    res["csv_row"] = row
    res["ephemeral"] = True
    return jsonify(res)


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
    scope = request.args.get("scope", "real")      # real | test
    return jsonify({"counts": logger.status_counts(scope=scope),
                    "methods": logger.method_counts(scope=scope),
                    "test_count": logger.test_count(),
                    "scope": scope,
                    "model": _model_info()})


@app.route("/api/clear_test", methods=["POST"])
def api_clear_test():
    """Purge sandbox/test rows — leaves real data and the OCR cache intact. Optional
    ?batch_id= to clear only one test batch."""
    batch_id = request.args.get("batch_id") or (request.form.get("batch_id") if request.form else None)
    return jsonify(pg.clear_test_data(batch_id=batch_id or None))


@app.route("/api/observability")
def api_observability():
    """All operational + quality metrics for the Observability view, one call.
    ?scope=test views the sandbox data instead of real."""
    scope = "test" if request.args.get("scope") == "test" else "real"
    return jsonify(metrics.snapshot(scope=scope))


@app.route("/api/observability/detail")
def api_observability_detail():
    """Drill-down rows behind a clicked metric (?kind=&field=&lender=&scope=)."""
    scope = "test" if request.args.get("scope") == "test" else "real"
    return jsonify(metrics.detail(request.args.get("kind", ""),
                                  field=request.args.get("field"),
                                  lender=request.args.get("lender"),
                                  scope=scope))


@app.route("/health")
def health():
    """Liveness/readiness probe: DB reachable + basic queue state. 200 ok / 503 not."""
    db_ok, detail = True, ""
    try:
        with pg.pool().connection() as c:
            c.execute("SELECT 1")
    except Exception as e:
        db_ok, detail = False, str(e)[:200]
    body = {"ok": db_ok, "db": db_ok, "workers": settings.WORKER_COUNT,
            "model": settings.VISION_MODEL, "open_work": jobs.open_work_count() if db_ok else None}
    if detail:
        body["error"] = detail
    return jsonify(body), (200 if db_ok else 503)


@app.route("/api/leads")
def api_leads():
    status = request.args.get("status", "all")
    q = request.args.get("q", "").strip()
    scope = request.args.get("scope", "real")      # real | test | all
    limit = min(int(request.args.get("limit", 300)), 2000)
    rows = logger.query_results(status=status, q=q, limit=limit, scope=scope)
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
    j["review"] = logger.latest_review(lead_id)
    j["review_history"] = logger.review_history(lead_id)
    return jsonify(j)


@app.route("/api/lead/<lead_id>/review", methods=["POST"])
def api_review(lead_id):
    """Record a reviewer's decision on a lead — the human-review loop. A reviewer
    CONFIRMS the system verdict or OVERTURNS it to a corrected status. This is the
    audit trail and the ground truth behind accuracy telemetry."""
    body = request.get_json(force=True) or {}
    decision = body.get("decision", "confirmed")
    corrected = (body.get("corrected_status") or "").strip() or None
    reviewer = (body.get("reviewer") or "").strip() or (request.remote_addr or "reviewer")
    note = (body.get("note") or "").strip()

    j = logger.get_lead_journey(lead_id)
    if not j["final"]:
        abort(404)
    system_status = j["final"].get("verification_status")

    valid = {"verified", "unverified", "non_document", "duplicate"}
    if decision == "overturned":
        if corrected not in valid:
            return jsonify({"error": f"corrected_status must be one of {sorted(valid)}"}), 400
        if corrected == system_status:
            decision = "confirmed"                 # 'overturn' to the same verdict is a confirm
    rec = logger.save_review(lead_id, system_status, decision,
                             corrected_status=corrected, reviewer=reviewer, note=note,
                             is_test=bool(j["final"].get("is_test")))
    return jsonify(rec)


# ── downloads / files ─────────────────────────────────────────────────────────
@app.route("/download/<batch_id>")
def download(batch_id):
    leads = jobs.batch_leads(batch_id, limit=100000)
    rows = [{"lead_id": r["lead_id"], "lender": r["lender"],
             "verification_status": r.get("verification_status") or r.get("job_status"),
             "outcome": _outcome_text(r.get("verification_status"), r.get("outcome")),
             "payment_method": r.get("payment_method") or ""} for r in leads]
    order = {"unverified": 0, "non_document": 1, "duplicate": 2, "verified": 3}
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
    host, port = "0.0.0.0", 8000
    try:
        from waitress import serve
        print(f"[server] waitress (production WSGI) on {host}:{port} · {settings.WEB_THREADS} threads"
              f" · auth {'ON' if _AUTH_ON else 'OFF'}")
        serve(app, host=host, port=port, threads=settings.WEB_THREADS, _quiet=True)
    except ImportError:
        print("[server] waitress not installed — Flask dev server (dev only)")
        app.run(host=host, port=port, debug=False, threaded=True)
