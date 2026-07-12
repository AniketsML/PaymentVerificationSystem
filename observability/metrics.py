"""
Read-only operational + quality metrics over the pipeline's own Postgres tables
(lead_events, jobs, lead_results). No new infrastructure — this is the data behind
the in-app Observability view and the /health check. Every function is a pure read;
all numerics are cast to float/int so the result is JSON-serialisable as-is.
"""
from __future__ import annotations

import threading
import time

from db import pg
from pipeline.verify import LENDER_RECEIVERS, LENDER_RULES


# scope is carried per-request-thread; snapshot(scope=...) sets it, and _rows/_one swap
# the *_real views for *_test ones — so every query becomes scope-aware with no rewrites.
_ctx = threading.local()


def _scope_rel(sql: str) -> str:
    if getattr(_ctx, "scope", "real") != "test":
        return sql
    return (sql.replace("lead_results_real", "lead_results_test")
               .replace("lead_events_real", "lead_events_test")
               .replace("lead_reviews_real", "lead_reviews_test")
               .replace("jobs_real", "jobs_test"))


def _rows(sql, params=()):
    with pg.pool().connection() as c:
        return c.execute(_scope_rel(sql), params).fetchall()


def _one(sql, params=()):
    with pg.pool().connection() as c:
        return c.execute(_scope_rel(sql), params).fetchone()


# ── tiny TTL cache ────────────────────────────────────────────────────────────
# snapshot() fans out ~15 aggregate queries; the dashboard polls it. Without this
# every poll re-scans the tables, so the page gets slower as data grows. An 8s TTL
# keeps it feeling live while capping DB load to one fan-out per interval.
_CACHE: dict = {}


def _cached(key, ttl, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


# stage2 event time includes OCR + classify + logging; the ACTUAL model call time is
# logged separately as metrics.model_ms. Latency panels must use the real model time.
_MODEL_MS = "(metrics->>'model_ms')::float"
_HAS_MODEL_MS = ("stage='stage2_ocr_classify' AND jsonb_typeof(metrics->'model_ms')='number' "
                 "AND (metrics->>'model_ms')::float > 0")


# ── operational (the "is it healthy / why is it slow" signals) ─────────────────
def model_latency() -> dict:
    """TRUE Medha-call latency (metrics.model_ms), not the whole stage2 wall-clock.
    This is the throughput bottleneck's real profile."""
    return _one(f"""
        SELECT COUNT(*) AS n,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY {_MODEL_MS})::float AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY {_MODEL_MS})::float AS p95,
               percentile_cont(0.99) WITHIN GROUP (ORDER BY {_MODEL_MS})::float AS p99,
               AVG({_MODEL_MS})::float AS avg, MAX({_MODEL_MS})::float AS max
        FROM lead_events_real WHERE {_HAS_MODEL_MS}
    """) or {}


def latency_series(minutes: int = 120) -> list:
    """Per-minute model latency p50/p95 — this is what shows a run slowing down."""
    return _rows(f"""
        SELECT to_char(date_trunc('minute', ts), 'HH24:MI') AS t,
               COUNT(*) AS n,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY {_MODEL_MS})::float AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY {_MODEL_MS})::float AS p95
        FROM lead_events_real
        WHERE {_HAS_MODEL_MS} AND ts > now() - make_interval(mins => %s)
        GROUP BY date_trunc('minute', ts) ORDER BY 1
    """, (minutes,))


def throughput_series(minutes: int = 120) -> list:
    """Leads closed per minute."""
    return _rows("""
        SELECT to_char(date_trunc('minute', ts), 'HH24:MI') AS t, COUNT(*) AS n
        FROM lead_events_real
        WHERE stage='lead_closed' AND ts > now() - make_interval(mins => %s)
        GROUP BY date_trunc('minute', ts) ORDER BY 1
    """, (minutes,))


def stage_timings() -> list:
    """Average + p95 time per stage — shows where the wall-clock goes."""
    return _rows("""
        SELECT stage, COUNT(*) AS n, AVG(ms)::float AS avg_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY ms)::float AS p95_ms
        FROM lead_events_real WHERE ms IS NOT NULL
        GROUP BY stage ORDER BY avg_ms DESC NULLS LAST
    """)


def queue() -> dict:
    rows = _rows("SELECT status, COUNT(*) AS n FROM jobs_real GROUP BY status")
    q = {r["status"]: r["n"] for r in rows}
    extra = _one("""
        SELECT COUNT(*) FILTER (WHERE attempts > 1) AS retried,
               COUNT(*) FILTER (WHERE status='failed') AS failed,
               COALESCE(AVG(attempts)::float, 0) AS avg_attempts,
               COUNT(*) AS total
        FROM jobs_real
    """) or {}
    q.update(extra)
    return q


def ocr_cache_stats() -> dict:
    """How much model work the extraction cache is saving."""
    r = _one("SELECT COUNT(*)::int AS entries, COALESCE(SUM(hits),0)::int AS hits FROM ocr_cache") or {}
    served = _one("""
        SELECT COUNT(*) FILTER (WHERE data->'model_meta'->>'cache'='hit')::int AS from_cache,
               COUNT(*)::int AS total
        FROM lead_events_real WHERE stage='stage2_ocr_classify'
    """) or {}
    total = served.get("total", 0) or 0
    return {"entries": r.get("entries", 0), "hits": r.get("hits", 0),
            "served_from_cache": served.get("from_cache", 0), "stage2_total": total,
            "hit_rate_pct": _pct(served.get("from_cache", 0), total)}


def errors() -> dict:
    model_err = _one("""
        SELECT COUNT(*) AS n FROM lead_events_real
        WHERE stage='stage2_ocr_classify' AND COALESCE(metrics->>'model_error','') <> ''
    """) or {"n": 0}
    failed = _rows("""
        SELECT COALESCE(NULLIF(last_error,''), '(no message)') AS err, COUNT(*) AS n
        FROM jobs_real WHERE status='failed' GROUP BY 1 ORDER BY 2 DESC LIMIT 8
    """)
    return {"model_errors": model_err.get("n", 0), "failed_jobs": failed}


# ── quality (the "are the verdicts good / drifting" signals) ───────────────────
def unverified_reasons() -> list:
    """Which mandatory field fails most often — the direct signal for what to tune
    in config (a spike in `receiver` fails => a lender needs an allowlist, etc.)."""
    return _rows("""
        SELECT f AS field, COUNT(*) AS n
        FROM lead_results_real,
             LATERAL jsonb_array_elements_text(
                 COALESCE(outcome->'failed_fields',
                          outcome->'verification'->'failed_fields',
                          '[]'::jsonb)) AS f
        WHERE verification_status='unverified'
        GROUP BY 1 ORDER BY 2 DESC
    """)


def lender_funnel() -> list:
    return _rows("""
        SELECT lender,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE verification_status='verified')     AS verified,
               COUNT(*) FILTER (WHERE verification_status='unverified')   AS unverified,
               COUNT(*) FILTER (WHERE verification_status='duplicate')    AS duplicate,
               COUNT(*) FILTER (WHERE verification_status='non_document') AS non_document,
               COUNT(*) FILTER (WHERE verification_status='unprocessed')  AS unprocessed
        FROM lead_results_real
        WHERE COALESCE(lender,'') <> ''
        GROUP BY lender ORDER BY total DESC LIMIT 40
    """)


def extraction_fillrates() -> dict:
    """Of the leads that reached extraction, how often each field was read. A drop
    here over time is the earliest signal of model/prompt drift or a format change."""
    return _one("""
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE COALESCE(extracted->>'amount','') <> '')              AS amount,
               COUNT(*) FILTER (WHERE COALESCE(extracted->>'date','') <> '')                AS date,
               COUNT(*) FILTER (WHERE COALESCE(extracted->>'receiver_name','') <> '')       AS receiver,
               COUNT(*) FILTER (WHERE COALESCE(extracted->>'loan_account_number','') <> '') AS lan
        FROM lead_results_real WHERE verification_status IN ('verified','unverified')
    """) or {}


def fillrate_series(days: int = 14) -> list:
    """Per-day extraction fill-rate — makes the DRIFT the single-number fillrate can't
    show visible: a field's read-rate sliding down over days = model/format drift."""
    return _rows("""
        SELECT to_char(date_trunc('day', updated_at), 'MM-DD') AS t,
               COUNT(*)::int AS n,
               round(100.0 * COUNT(*) FILTER (WHERE COALESCE(extracted->>'amount','') <> '') / COUNT(*), 0)::int AS amount,
               round(100.0 * COUNT(*) FILTER (WHERE COALESCE(extracted->>'date','') <> '') / COUNT(*), 0)::int AS date,
               round(100.0 * COUNT(*) FILTER (WHERE COALESCE(extracted->>'receiver_name','') <> '') / COUNT(*), 0)::int AS receiver,
               round(100.0 * COUNT(*) FILTER (WHERE COALESCE(extracted->>'loan_account_number','') <> '') / COUNT(*), 0)::int AS lan
        FROM lead_results_real
        WHERE verification_status IN ('verified','unverified')
          AND updated_at > now() - make_interval(days => %s)
        GROUP BY date_trunc('day', updated_at) ORDER BY 1
    """, (days,))


def config_coverage() -> list:
    """Lenders present in the data that are missing an explicit rule or receiver
    allowlist — a proactive onboarding signal (they still work via defaults/fallback,
    but explicit config is tighter)."""
    rows = _rows("""
        SELECT lender, COUNT(*) AS n FROM lead_results_real
        WHERE COALESCE(lender,'') <> '' GROUP BY lender ORDER BY n DESC
    """)
    rules = {k for k in LENDER_RULES if k != "__default__"}
    out = []
    for r in rows:
        lender = r["lender"]
        has_recv = bool(LENDER_RECEIVERS.get(lender))
        has_rule = lender in rules
        if not (has_recv and has_rule):
            out.append({"lender": lender, "n": r["n"],
                        "receiver_list": has_recv, "explicit_rule": has_rule})
    return out


# ── accuracy (the "are the verdicts actually CORRECT" signals) ────────────────
# Ground truth = the human-review ledger. A reviewer confirms or overturns each
# verdict, so we can measure precision on `verified`, over-flagging on `unverified`,
# overall agreement, and — crucially — DRIFT over time. Without this the system is
# blind to its own correctness; these are the most important quality signals here.
_LATEST_REVIEWS = """
    WITH latest AS (
        SELECT DISTINCT ON (lead_id)
               lead_id, system_status, decision, corrected_status, reviewer, note, ts
        FROM lead_reviews_real ORDER BY lead_id, id DESC
    )
"""


def _pct(num, den):
    return round(100.0 * num / den, 1) if den else None


def review_accuracy() -> dict:
    """Headline correctness numbers from the review ledger (latest review per lead)."""
    r = _one(_LATEST_REVIEWS + """
        SELECT COUNT(*)::int AS reviewed,
               COUNT(*) FILTER (WHERE decision='confirmed')::int  AS confirmed,
               COUNT(*) FILTER (WHERE decision='overturned')::int AS overturned,
               COUNT(*) FILTER (WHERE system_status='verified')::int AS verified_reviewed,
               COUNT(*) FILTER (WHERE system_status='verified' AND decision='overturned')::int AS verified_overturned,
               COUNT(*) FILTER (WHERE system_status='unverified')::int AS unverified_reviewed,
               COUNT(*) FILTER (WHERE system_status='unverified' AND decision='overturned')::int AS unverified_overturned,
               COUNT(*) FILTER (WHERE system_status='non_document')::int AS nondoc_reviewed,
               COUNT(*) FILTER (WHERE system_status='non_document' AND decision='overturned')::int AS nondoc_overturned
        FROM latest
    """) or {}
    total = _one("SELECT COUNT(*)::int AS n FROM lead_results_real") or {"n": 0}
    reviewed = r.get("reviewed", 0)
    return {
        "reviewed": reviewed,
        "total_results": total.get("n", 0),
        "coverage_pct": _pct(reviewed, total.get("n", 0)),
        "confirmed": r.get("confirmed", 0),
        "overturned": r.get("overturned", 0),
        # of everything reviewed, how often the system agreed with the human
        "agreement_pct": _pct(r.get("confirmed", 0), reviewed),
        # precision proxy on `verified`: of reviewed verifieds, how many stood (were NOT overturned)
        "verified_reviewed": r.get("verified_reviewed", 0),
        "verified_overturned": r.get("verified_overturned", 0),
        "verified_precision_pct": _pct(r.get("verified_reviewed", 0) - r.get("verified_overturned", 0),
                                       r.get("verified_reviewed", 0)),
        # over-flagging on `unverified`: how many the human said were actually fine
        "unverified_reviewed": r.get("unverified_reviewed", 0),
        "unverified_overturned": r.get("unverified_overturned", 0),
        "unverified_overturn_pct": _pct(r.get("unverified_overturned", 0),
                                        r.get("unverified_reviewed", 0)),
        "nondoc_reviewed": r.get("nondoc_reviewed", 0),
        "nondoc_overturned": r.get("nondoc_overturned", 0),
    }


def review_confusion() -> list:
    """system verdict → reviewer's corrected verdict, for the overturned cases only.
    Shows exactly which mistakes the system makes (verified→unverified = false positive)."""
    return _rows(_LATEST_REVIEWS + """
        SELECT system_status, corrected_status, COUNT(*)::int AS n
        FROM latest
        WHERE decision='overturned' AND corrected_status IS NOT NULL
        GROUP BY system_status, corrected_status
        ORDER BY n DESC
    """)


def accuracy_series(days: int = 14) -> list:
    """Per-day agreement rate — the DRIFT signal. A falling line = the model/config is
    getting worse over time (the whole point of tracking accuracy, not just volume)."""
    return _rows(_LATEST_REVIEWS + """
        SELECT to_char(date_trunc('day', ts), 'MM-DD') AS t,
               COUNT(*)::int AS reviewed,
               COUNT(*) FILTER (WHERE decision='confirmed')::int AS confirmed,
               round(100.0 * COUNT(*) FILTER (WHERE decision='confirmed') / COUNT(*), 1)::float AS agreement_pct
        FROM latest
        WHERE ts > now() - make_interval(days => %s)
        GROUP BY date_trunc('day', ts) ORDER BY 1
    """, (days,))


def detail(kind: str, field: str = None, lender: str = None, scope: str = "real") -> dict:
    """Drill-down rows behind a metric — so a clicked number opens the actual leads.
    Every row carries lead_id where possible so the UI can deep-link to the drawer.
    scope='test' swaps the real views for test ones (same as snapshot), so a drill-down
    in the Test workspace opens sandbox rows, not real ones."""
    _ctx.scope = scope
    try:
        return _detail(kind, field, lender)
    finally:
        _ctx.scope = "real"


def _detail(kind: str, field: str = None, lender: str = None) -> dict:
    if kind == "model_errors":
        return {"title": "Model errors", "columns": ["lead_id", "error", "ts"],
                "rows": _rows("""
            SELECT lead_id, left(metrics->>'model_error', 180) AS error,
                   to_char(ts, 'YYYY-MM-DD HH24:MI') AS ts
            FROM lead_events_real
            WHERE stage='stage2_ocr_classify' AND COALESCE(metrics->>'model_error','') <> ''
            ORDER BY ts DESC LIMIT 200
        """)}
    if kind == "failed_jobs":
        return {"title": "Failed jobs", "columns": ["lead_id", "error", "attempts"],
                "rows": _rows("""
            SELECT job_id AS lead_id, left(COALESCE(last_error,''), 180) AS error, attempts
            FROM jobs_real WHERE status='failed' ORDER BY updated_at DESC LIMIT 200
        """)}
    if kind == "missing_field":
        col = {"amount": "amount", "date": "date", "receiver": "receiver_name",
               "lan": "loan_account_number"}.get(field, field or "amount")
        return {"title": f"Leads missing '{field}' in extraction",
                "columns": ["lead_id", "lender", "status"],
                "rows": _rows("""
            SELECT lead_id, lender, verification_status AS status
            FROM lead_results_real
            WHERE verification_status IN ('verified','unverified')
              AND COALESCE(extracted->>%s, '') = ''
            ORDER BY updated_at DESC LIMIT 300
        """, (col,))}
    if kind == "unverified_field":
        return {"title": f"Unverified — '{field}' check failed",
                "columns": ["lead_id", "lender"],
                "rows": _rows("""
            SELECT lead_id, lender FROM lead_results_real
            WHERE verification_status='unverified'
              AND jsonb_exists(COALESCE(outcome->'failed_fields',
                        outcome->'verification'->'failed_fields', '[]'::jsonb), %s)
            ORDER BY updated_at DESC LIMIT 300
        """, (field,))}
    if kind == "lender":
        return {"title": f"Leads for {lender}",
                "columns": ["lead_id", "status", "method"],
                "rows": _rows("""
            SELECT lead_id, verification_status AS status, COALESCE(payment_method,'') AS method
            FROM lead_results_real WHERE lender=%s ORDER BY updated_at DESC LIMIT 300
        """, (lender,))}
    if kind == "overturned_verified":
        # the caught false positives: system said verified, a human overturned it
        return {"title": "Overturned 'verified' (false positives caught)",
                "columns": ["lead_id", "corrected_status", "reviewer", "note"],
                "rows": _rows(_LATEST_REVIEWS + """
            SELECT lead_id, corrected_status, reviewer, left(COALESCE(note,''),120) AS note
            FROM latest WHERE system_status='verified' AND decision='overturned'
            ORDER BY ts DESC LIMIT 300
        """)}
    if kind == "overturned":
        return {"title": "All overturned verdicts",
                "columns": ["lead_id", "system_status", "corrected_status", "reviewer"],
                "rows": _rows(_LATEST_REVIEWS + """
            SELECT lead_id, system_status, corrected_status, reviewer
            FROM latest WHERE decision='overturned' ORDER BY ts DESC LIMIT 300
        """)}
    if kind == "unreviewed":
        # verdicts a human has NOT yet checked (review backlog) — verified/unverified only
        return {"title": "Unreviewed verdicts (backlog)",
                "columns": ["lead_id", "lender", "status"],
                "rows": _rows("""
            SELECT r.lead_id, r.lender, r.verification_status AS status
            FROM lead_results_real r
            LEFT JOIN lead_reviews_real rv ON rv.lead_id = r.lead_id
            WHERE rv.lead_id IS NULL AND r.verification_status IN ('verified','unverified')
            ORDER BY r.updated_at DESC LIMIT 300
        """)}
    return {"title": "Detail", "columns": [], "rows": []}


def _snapshot() -> dict:
    return {
        "latency": model_latency(),
        "latency_series": latency_series(),
        "throughput_series": throughput_series(),
        "stage_timings": stage_timings(),
        "queue": queue(),
        "errors": errors(),
        "ocr_cache": ocr_cache_stats(),
        "unverified_reasons": unverified_reasons(),
        "lender_funnel": lender_funnel(),
        "fillrates": extraction_fillrates(),
        "fillrate_series": fillrate_series(),
        "config_coverage": config_coverage(),
        "accuracy": review_accuracy(),
        "accuracy_series": accuracy_series(),
        "confusion": review_confusion(),
    }


def snapshot(ttl: float = 2.5, scope: str = "real") -> dict:
    """Everything the Observability view needs, in one call — briefly TTL-cached so
    concurrent polls don't each re-scan the tables, while staying near-real-time
    (the UI polls ~every 4s). scope='test' views the sandbox data. ttl=0 forces fresh."""
    def build():
        _ctx.scope = scope
        try:
            return _snapshot()
        finally:
            _ctx.scope = "real"
    if ttl <= 0:
        return build()
    return _cached(f"snapshot:{scope}", ttl, build)
