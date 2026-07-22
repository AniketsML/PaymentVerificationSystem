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
import secrets
import sys
import tempfile
import time
from datetime import timedelta

import pandas as pd
from flask import (Flask, g,request, send_file, jsonify, render_template, abort, url_for,
                   redirect, session)
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings
from db import pg
from observability.pg_logger import PgLeadLogger
from observability.pg_dedup import PaymentDedup
from observability import metrics
from ocr.medha_client import MedhaVisionOCR, PrecomputedOCR
from pipeline.orchestrator import process_lead
from pipeline import approvals, jobs
from run_batch import outcome_text
import worker

app = Flask(__name__)
# cap upload size so a huge file can't OOM the process (default 64 MB, override via env)
app.config["MAX_CONTENT_LENGTH"] = settings.MAX_UPLOAD_MB * 1024 * 1024
app.secret_key = settings.SECRET_KEY or secrets.token_hex(32)  # signs the session cookie
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))
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
    _ev = pg.purge_expired_events(settings.LEAD_EVENTS_TTL_DAYS)
    if _ev:
        sys.stderr.write(f"[server] purged {_ev} lead_event(s) past retention\n")
except Exception as _e:  # never block boot on housekeeping
    sys.stderr.write(f"[server] test-data purge skipped: {_e}\n")
logger = PgLeadLogger()
dedup = PaymentDedup()
worker.start_pool()          # in-process worker pool drains the queue


# ── session login (styled login page, not the browser Basic-auth popup) ───────
_AUTH_ON = bool(settings.AUTH_USER and settings.AUTH_PASS)
_TOKENS_ON = bool(settings.API_TOKENS)

# Fail CLOSED. A public deployment with no credentials configured must not start —
# silently serving borrower PII to anyone who finds the port is not an acceptable
# default. PV_ALLOW_INSECURE=1 overrides this for local development only.
if not (_AUTH_ON or _TOKENS_ON):
    if not settings.ALLOW_INSECURE:
        sys.stderr.write(
            "[server] FATAL: no authentication configured.\n"
            "  Set PV_AUTH_USER + PV_AUTH_PASS (dashboard login) and/or\n"
            "  PV_API_TOKENS='name:token,...' (machine callers).\n"
            "  For local dev only: PV_ALLOW_INSECURE=1\n")
        raise SystemExit(2)
    sys.stderr.write("[server] WARNING: running UNAUTHENTICATED (PV_ALLOW_INSECURE). "
                     "Anyone who can reach this host sees all leads.\n")

_PUBLIC_PATHS = {"/health", "/login", "/logout"}


def _bearer_caller():
    """Return the caller name for a valid bearer token, else None.

    Compares against every configured token in constant time and without early exit,
    so response timing can't be used to recover a token byte by byte. Deliberately
    not a dict lookup for the same reason.
    """
    hdr = request.headers.get("Authorization") or ""
    if not hdr.startswith("Bearer "):
        return None
    presented = hdr[7:].strip()
    if not presented:
        return None
    match = None
    for token, name in settings.API_TOKENS.items():
        if hmac.compare_digest(presented, token):
            match = name
    return match


@app.before_request
def _require_auth():
    if not (_AUTH_ON or _TOKENS_ON):
        return                                   # insecure dev mode, already warned
    p = request.path
    if p in _PUBLIC_PATHS or p.startswith("/static/"):
        return

    # 1) machine caller with a bearer token
    caller = _bearer_caller()
    if caller:
        g.api_caller = caller                    # available to handlers for attribution
        return

    # 2) browser session
    if session.get("authed"):
        return

    # 3) rejected — navigate browsers to login, answer API/XHR with 401
    wants_page = request.method == "GET" and "text/html" in (request.headers.get("Accept") or "")
    if wants_page and _AUTH_ON:
        return redirect(url_for("login", next=p))
    resp = jsonify({"error": "authentication required",
                    "detail": "send 'Authorization: Bearer <token>'"})
    resp.headers["WWW-Authenticate"] = 'Bearer realm="payment-verification"'
    return resp, 401

@app.route("/login", methods=["GET", "POST"])
def login():
    if not _AUTH_ON or session.get("authed"):
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        ok = (hmac.compare_digest(u, settings.AUTH_USER)
              and hmac.compare_digest(pw, settings.AUTH_PASS))
        if ok:
            session.clear()
            session["authed"] = True
            session["user"] = u
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        error = "Incorrect email or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.after_request
def _cache_headers(resp):
    """HTML is never cached, so the browser always re-fetches the page and sees the
    current ?v= asset URLs. Versioned static files (with ?v=) may be cached freely."""
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    elif request.args.get("v"):          # a versioned static asset -> cache it hard
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.context_processor
def _asset_helper():
    """`asset('style.css')` -> /static/style.css?v=<mtime>, so the browser always re-fetches
    the file after it changes (kills stale-CSS caching)."""
    def asset(filename):
        url = url_for("static", filename=filename)
        try:
            mt = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
            return f"{url}?v={mt}"
        except OSError:
            return url
    return {"asset": asset}


def _model_info() -> dict:
    from config import runtime
    cfg = runtime.model_config()
    return {"model": cfg["model"], "url": cfg["url"],
            "stream": cfg["stream"], "workers": settings.WORKER_COUNT}


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


# ── unprocessed-image bifurcation ─────────────────────────────────────────────
def _is_unprocessed(status, outcome) -> bool:
    """The image never reached OCR — its own first-class status since the migration."""
    return status == "unprocessed"


def _image_issue(outcome: dict) -> str:
    """Deterministic issue bucket for WHY an image never reached OCR, derived from the
    precise load-failure reason recorded by pipeline/image_source.py."""
    if (outcome or {}).get("stage_failed") == "image_qc":
        return "Quality discarded"
    s = ((outcome or {}).get("describes") or "").lower()
    if "no image source provided" in s:
        return "No image URL"
    if "file not found" in s:
        return "File not found"
    if "access denied" in s:
        return "Private / access denied"
    if "returned a web page" in s or "returned an error response" in s or "expired" in s:
        return "Private / expired link"
    if "could not download" in s:
        return "Download timeout" if "timeout" in s else "Download failed"
    if "empty image payload" in s:
        return "Empty file"
    if "unreadable/corrupt" in s or "could not read image source" in s:
        return "Corrupt / unreadable"
    return "Other loading issue"


# ── SPA shell ─────────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/lead/<lead_id>")
def index(lead_id=None):
    return render_template("index.html", model=_model_info(), deep_lead=lead_id or "",
                           user=(session.get("user", "") if _AUTH_ON else ""))


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

# ── JSON submit (async) — the machine-caller entry point ──────────────────────
@app.route("/api/jobs", methods=["POST"])
def api_jobs():
    """Accept JSON rows and enqueue them onto the durable queue.

    The counterpart to /api/enqueue (which requires a CSV upload) and to /api/verify
    (which runs the vision model inside the request thread). Callers get an immediate
    202 and poll /api/batch/<id> or /api/lead/<id>/status.

    Idempotent: job_id = lead_id, so re-sending a lead is a free no-op reported as
    `skipped`. Send a stable id via id_col — otherwise the id is a content hash of the
    row and any field change produces a NEW lead.

    Body:
      {"rows": [ {...}, {...} ],        # or "record": {...} for a single lead
       "id_col": "lead_id",             # column holding your stable id
       "image_col": "payment_document",
       "image_root": "",
       "test": false}
    """
    body = request.get_json(force=True, silent=True) or {}
    rows = body.get("rows")
    if rows is None and isinstance(body.get("record"), dict):
        rows = [body["record"]]
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "provide 'rows' (non-empty list) or 'record' (object)"}), 400
    if not all(isinstance(r, dict) for r in rows):
        return jsonify({"error": "every entry in 'rows' must be an object"}), 400
    if len(rows) > settings.API_MAX_ROWS:
        return jsonify({"error": f"too many rows (max {settings.API_MAX_ROWS} per request)"}), 413

    result = jobs.enqueue_rows(
        rows,
        image_col=body.get("image_col", "payment_document"),
        id_col=body.get("id_col") or None,
        image_root=body.get("image_root", ""),
        is_test=bool(body.get("test")),
    )
    worker.start_pool()
    result["submitted_by"] = getattr(g, "api_caller", "") or session.get("user", "")
    return jsonify(result), 202


@app.route("/api/lead/<lead_id>/status")
def api_lead_status(lead_id):
    """Cheap poll — queue state + verdict only, no journey/extraction blobs.

    Use this instead of /api/lead/<id> when polling in a loop; that endpoint pulls the
    full event journey and raw model output on every call.
    """
    with pg.pool().connection() as c:
        row = c.execute(
            "SELECT j.lead_id, j.status AS job_status, j.attempts, j.max_attempts, "
            "j.last_error, j.is_test, j.updated_at, "
            "COALESCE(r.verification_status, j.verification_status) AS verification_status "
            "FROM jobs j LEFT JOIN lead_results r ON r.lead_id = j.lead_id "
            "WHERE j.job_id = %s", (lead_id,)).fetchone()
    if not row:
        abort(404)
    row["updated_at"] = row["updated_at"].isoformat() if row.get("updated_at") else None
    row["terminal"] = row["job_status"] in ("done", "failed")
    return jsonify(row)


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
    # unprocessed-image bifurcation: total + per-issue counts for the dashboard band
    issues = {}
    for o in logger.unprocessed_outcomes(scope=scope):
        k = _image_issue(_as_obj(o))
        issues[k] = issues.get(k, 0) + 1
    unprocessed = {"total": sum(issues.values()),
                   "issues": [{"issue": k, "n": n}
                              for k, n in sorted(issues.items(), key=lambda x: -x[1])]}
    return jsonify({"counts": logger.status_counts(scope=scope),
                    "methods": logger.method_counts(scope=scope),
                    "unprocessed": unprocessed,
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


@app.route("/api/approvals")
def api_approvals():
    """The Receiver-Approval queue (grouped receiver mismatches) + recent decisions."""
    return jsonify({"pending": approvals.pending_queue(),
                    "decisions": approvals.recent_decisions()})


@app.route("/api/approvals/decide", methods=["POST"])
def api_approvals_decide():
    """Human decision on a (lender, receiver) pair. approve -> config + re-verify all
    affected leads; reject -> suppress the pair from the queue. Leads never leave
    `unverified` except by legitimately re-verifying."""
    body = request.get_json(force=True) or {}
    res = approvals.decide(body.get("lender", ""), body.get("receiver", ""),
                           body.get("decision", ""),
                           decided_by=session.get("user", "") or (request.remote_addr or ""),
                           note=(body.get("note") or "").strip())
    return (jsonify(res), 409) if res.get("error") else jsonify(res)


@app.route("/api/observability/detail")
def api_observability_detail():
    """Drill-down rows behind a clicked metric (?kind=&field=&lender=&scope=)."""
    scope = "test" if request.args.get("scope") == "test" else "real"
    return jsonify(metrics.detail(request.args.get("kind", ""),
                                  field=request.args.get("field"),
                                  lender=request.args.get("lender"),
                                  scope=scope))


@app.route("/api/config/model", methods=["GET", "POST"])
def api_config_model():
    """Get (key redacted) or update the Medha endpoint config. Changes apply to the next
    lead — no restart. An empty key on POST keeps the existing key."""
    from config import runtime
    if request.method == "POST":
        b = request.get_json(force=True) or {}
        runtime.set_model_config(url=b.get("url"), key=b.get("key"),
                                 model=b.get("model"), stream=b.get("stream"))
    return jsonify(runtime.masked())


@app.route("/api/config/model/test", methods=["POST"])
def api_config_model_test():
    """Probe an endpoint (the posted candidate, or the saved one) for reachability + auth,
    without saving. Hits the OpenAI-compatible /models list."""
    import time as _t
    import requests as _rq
    from config import runtime
    b = request.get_json(force=True) or {}
    cfg = runtime.model_config()
    url = (b.get("url") or cfg["url"] or "").rstrip("/")
    key = b.get("key") or cfg["key"]
    want_model = (b.get("model") or cfg["model"] or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "no endpoint URL"}), 400
    t0 = _t.time()
    try:
        r = _rq.get(f"{url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=8)
        ms = round((_t.time() - t0) * 1000)
        ids = []
        if r.headers.get("content-type", "").startswith("application/json"):
            ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
        ok = r.status_code == 200
        return jsonify({"ok": ok, "status": r.status_code, "ms": ms,
                        "models": ids[:25],
                        "model_present": (want_model in ids) if (ids and want_model) else None,
                        "error": "" if ok else f"HTTP {r.status_code}"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "ms": round((_t.time() - t0) * 1000),
                        "error": f"{type(e).__name__}: {e}"[:200]})


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
        o = _as_obj(r.get("outcome"))
        r["outcome_text"] = _outcome_text(r.get("verification_status"), o)
        r["issue"] = _image_issue(o) if _is_unprocessed(r.get("verification_status"), o) else ""
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

    valid = {"verified", "unverified", "non_document", "unprocessed", "duplicate"}
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
    order = {"unverified": 0, "unprocessed": 1, "non_document": 2, "duplicate": 3, "verified": 4}
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
    # host/port overridable via env (PORT) so the app can avoid a busy port or sit behind a
    # reverse proxy on a chosen port without a code change.
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    try:
        from waitress import serve
        print(f"[server] waitress (production WSGI) on {host}:{port} · {settings.WEB_THREADS} threads"
              f" · auth {'ON' if _AUTH_ON else 'OFF'}")
        serve(app, host=host, port=port, threads=settings.WEB_THREADS, _quiet=True)
    except ImportError:
        print("[server] waitress not installed — Flask dev server (dev only)")
        app.run(host=host, port=port, debug=False, threaded=True)
