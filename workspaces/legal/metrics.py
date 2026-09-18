"""
Observability for the SARFAESI Legal workspace.

The page answers five questions, in the order someone running extractions asks them:

  1. Is it running right now?          live queue, stuck dossiers, last finish, daily throughput
  2. Is the output complete & usable?  outcome mix, dossiers that finished with nothing cited,
                                       field coverage, values per dossier
  3. Where did the values come from?   typed / scanned / handwritten, per value and per dossier
  4. What does it cost?                tokens per dossier, per page read, per run, prompt share
  5. How long does it take?            model + total time percentiles, and how much of each
                                       dossier was actually read (documents & pages funnel)

Every number is a read-only aggregate over what the pipeline already stores — no extra model
calls. All sections honour the same slice (scope + run + period) except the live queue, which
is always "now". The per-dossier table is the table view behind every chart.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from db import pg
from workspaces.legal.db import init_schema
from workspaces.legal.ocr import get_breaker_status

FINISHED = ("completed", "partial", "missing_documents", "failed")
STUCK_AFTER_MIN = 20       # a dossier "processing" with no progress for this long is stuck

# one row per dossier in the slice, telemetry unpacked from JSONB once
_BASE = """
    WITH lead AS (
        SELECT l.lead_id, l.batch_id, l.batch_name, l.folder_path, l.lead_name, l.folder_name,
               CASE WHEN l.status = 'draft' THEN 'pending' ELSE l.status END AS status,
               l.created_at, l.updated_at,
               l.total_documents, l.processed_documents, l.failed_documents,
               COALESCE(l.extracted_data->'_telemetry', '{{}}'::jsonb)          AS t,
               COALESCE(l.extracted_data->'_page_extractions', '[]'::jsonb)     AS pxs,
               (SELECT count(*) FROM jsonb_object_keys(COALESCE(l.extracted_data, '{{}}'::jsonb)) k
                 WHERE left(k, 1) <> '_' AND k NOT IN ('page_extractions', 'telemetry'))  AS values_n
        FROM legal_leads l
        WHERE {where}
    ), tel AS (
        SELECT lead.*,
               jsonb_array_length(pxs)                              AS cited_pages,
               NULLIF(t->>'total_tokens', '')::numeric              AS tokens,
               NULLIF(t->>'prompt_tokens', '')::numeric             AS prompt_tokens,
               NULLIF(t->>'completion_tokens', '')::numeric         AS completion_tokens,
               NULLIF(t->>'vlm_latency_ms', '')::numeric            AS vlm_ms,
               NULLIF(t->>'pipeline_latency_ms', '')::numeric       AS pipeline_ms,
               NULLIF(t->>'model', '')                              AS model
        FROM lead
    )
"""


class Slice:
    """scope + optional run + optional period, as a WHERE clause over legal_leads `l`."""

    def __init__(self, scope: str = "real", batch_id: Optional[str] = None, days: Optional[int] = None):
        self.scope, self.batch_id, self.days = scope, batch_id, days
        clauses: List[str] = []
        self.params: List[Any] = []
        if scope == "test":
            clauses.append("l.is_test = true")
        elif scope != "all":
            clauses.append("l.is_test = false")
        if batch_id:
            clauses.append("l.batch_id = %s")
            self.params.append(batch_id)
        if days:
            clauses.append("l.updated_at > now() - make_interval(days => %s)")
            self.params.append(int(days))
        self.where = " AND ".join(clauses) or "TRUE"

    def rows(self, sql: str, extra: Tuple = ()) -> List[dict]:
        with pg.pool().connection() as c:
            return c.execute(_BASE.format(where=self.where) + sql, tuple(self.params) + tuple(extra)).fetchall()

    def one(self, sql: str, extra: Tuple = ()) -> dict:
        r = self.rows(sql, extra)
        return r[0] if r else {}


def _num(v, digits: int = 1):
    if v is None:
        return None
    f = float(v)
    return int(f) if f.is_integer() else round(f, digits)


def _run_name(batch_name, folder_path, fallback) -> str:
    from workspaces.legal.routes import extract_run_folder_name   # routes imports this module
    return extract_run_folder_name(batch_name=batch_name, folder_path=folder_path, fallback=fallback)


# ── 1. is it running right now? ───────────────────────────────────────────────
def live(scope: str) -> Dict[str, Any]:
    where = "l.is_test = true" if scope == "test" else ("TRUE" if scope == "all" else "l.is_test = false")
    with pg.pool().connection() as c:
        r = c.execute(f"""
            SELECT count(*) FILTER (WHERE l.status IN ('pending','draft'))::int AS queued,
                   count(*) FILTER (WHERE l.status = 'processing')::int       AS processing,
                   count(*) FILTER (WHERE l.status = 'processing'
                                    AND l.updated_at < now() - make_interval(mins => %s))::int AS stuck,
                   count(*) FILTER (WHERE l.status = 'paused')::int           AS paused,
                   max(l.updated_at) FILTER (WHERE l.status IN ('completed','partial','missing_documents','failed'))
                                                                              AS last_finished
            FROM legal_leads l WHERE {where}
        """, (STUCK_AFTER_MIN,)).fetchone() or {}
    r["last_finished"] = r["last_finished"].isoformat() if r.get("last_finished") else None
    r["stuck_after_min"] = STUCK_AFTER_MIN
    return r


def per_day(s: Slice, days: int) -> List[dict]:
    return [{"day": r["day"], "n": r["n"]} for r in s.rows("""
        SELECT to_char(d, 'YYYY-MM-DD') AS day,
               (SELECT count(*) FROM tel WHERE status IN ('completed','partial','missing_documents','failed')
                                           AND date_trunc('day', updated_at) = d)::int AS n
        FROM generate_series(date_trunc('day', now()) - make_interval(days => %s), date_trunc('day', now()),
                             interval '1 day') d
        ORDER BY d
    """, (max(1, days - 1),))]


# ── 2. is the output complete & usable? ───────────────────────────────────────
def outcomes(s: Slice) -> Dict[str, Any]:
    r = s.one("""
        SELECT count(*) FILTER (WHERE status = 'completed')::int         AS completed,
               count(*) FILTER (WHERE status = 'partial')::int           AS partial,
               count(*) FILTER (WHERE status = 'missing_documents')::int AS missing_documents,
               count(*) FILTER (WHERE status = 'failed')::int            AS failed,
               count(*) FILTER (WHERE status = 'completed' AND cited_pages = 0)::int AS completed_empty,
               count(*)::int                                             AS dossiers
        FROM tel
    """)
    r["finished"] = sum(r.get(k, 0) for k in FINISHED)
    empty_ids = s.rows("SELECT lead_id FROM tel WHERE status = 'completed' AND cited_pages = 0 ORDER BY updated_at DESC LIMIT 200")
    r["completed_empty_ids"] = [x["lead_id"] for x in empty_ids]
    return r


def field_coverage(s: Slice, limit: int = 16) -> List[dict]:
    """How often each extracted field was actually found, among dossiers that finished."""
    rows = s.rows("""
        , fin AS (SELECT * FROM tel WHERE status IN ('completed','partial','missing_documents','failed'))
        SELECT k AS field, count(*)::int AS n,
               round(100.0 * count(*) / NULLIF((SELECT count(*) FROM fin), 0), 1)::float AS pct
        FROM fin, LATERAL jsonb_object_keys(
            COALESCE((SELECT l2.extracted_data FROM legal_leads l2 WHERE l2.lead_id = fin.lead_id), '{}'::jsonb)) k
        WHERE left(k, 1) <> '_' AND k NOT IN ('page_extractions', 'telemetry')
        GROUP BY k ORDER BY n DESC, k LIMIT %s
    """, (limit,))
    return rows


def values_histogram(s: Slice) -> List[dict]:
    bins = [("0", 0, 0), ("1–3", 1, 3), ("4–6", 4, 6), ("7–9", 7, 9), ("10–14", 10, 14), ("15+", 15, 10 ** 6)]
    r = s.one("SELECT " + ", ".join(
        f"count(*) FILTER (WHERE status IN ('completed','partial','missing_documents','failed') "
        f"AND values_n BETWEEN {lo} AND {hi})::int AS b{i}" for i, (_, lo, hi) in enumerate(bins)) + " FROM tel")
    return [{"bin": label, "n": r.get(f"b{i}", 0)} for i, (label, _, _) in enumerate(bins)]


# ── 3. where did the values come from? ────────────────────────────────────────
def script_mix(s: Slice) -> Dict[str, Any]:
    """Value-level and dossier-level typed / scanned / handwritten, from the provenance cache.
    Dossiers not yet analysed are reported as such, never guessed."""
    rows = s.rows("""
        SELECT p.tag, p.counts
        FROM tel LEFT JOIN legal_field_provenance p ON p.lead_id = tel.lead_id
        WHERE tel.status IN ('completed','partial','missing_documents','failed')
    """)
    values = {"typed": 0, "scanned": 0, "handwritten": 0, "unknown": 0}
    dossiers = {"typed": 0, "scanned": 0, "handwritten": 0, "unknown": 0, "none": 0, "not_checked": 0}
    for r in rows:
        if not r.get("tag"):
            dossiers["not_checked"] += 1
            continue
        dossiers[r["tag"] if r["tag"] in dossiers else "unknown"] += 1
        for k, n in (r.get("counts") or {}).items():
            key = "typed" if k == "printed" else k
            values[key if key in values else "unknown"] += int(n or 0)
    return {"values": values, "dossiers": dossiers}


# ── 4. what does it cost? ─────────────────────────────────────────────────────
def tokens(s: Slice) -> Dict[str, Any]:
    r = s.one("""
        SELECT COALESCE(sum(tokens), 0)::bigint             AS total,
               COALESCE(sum(prompt_tokens), 0)::bigint      AS prompt,
               COALESCE(sum(completion_tokens), 0)::bigint  AS completion,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY tokens) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY tokens) AS p95,
               max(tokens)                                  AS max,
               count(*) FILTER (WHERE tokens > 0)::int      AS measured,
               COALESCE(sum(cited_pages) FILTER (WHERE tokens > 0), 0)::int AS pages_cited
        FROM tel
    """)
    return {
        "total": int(r.get("total") or 0), "prompt": int(r.get("prompt") or 0),
        "completion": int(r.get("completion") or 0),
        "p50": _num(r.get("p50"), 0), "p95": _num(r.get("p95"), 0), "max": _num(r.get("max"), 0),
        "measured": r.get("measured", 0),
        "per_page": round(int(r["total"]) / r["pages_cited"], 0) if r.get("pages_cited") else None,
        "prompt_share": round(100.0 * int(r["prompt"]) / int(r["total"]), 1) if r.get("total") else None,
    }


def per_run(s: Slice, limit: int = 10) -> List[dict]:
    rows = s.rows("""
        SELECT batch_id, max(batch_name) AS batch_name, max(folder_path) AS folder_path,
               max(folder_name) AS any_folder, min(created_at) AS started_at,
               count(*)::int                              AS dossiers,
               COALESCE(sum(prompt_tokens), 0)::bigint     AS prompt,
               COALESCE(sum(completion_tokens), 0)::bigint AS completion,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY pipeline_ms) AS p50_ms,
               count(*) FILTER (WHERE status = 'completed')::int AS completed
        FROM tel GROUP BY batch_id ORDER BY started_at DESC LIMIT %s
    """, (limit,))
    return [{
        "batch_id": r["batch_id"],
        "name": _run_name(r["batch_name"], r["folder_path"], r["any_folder"]),
        "started_at": r["started_at"].isoformat() if r.get("started_at") else None,
        "dossiers": r["dossiers"], "completed": r["completed"],
        "prompt": int(r["prompt"]), "completion": int(r["completion"]),
        "p50_ms": _num(r.get("p50_ms"), 0),
    } for r in rows]


# ── 5. how long does it take, and how much is read? ───────────────────────────
def timing(s: Slice) -> Dict[str, Any]:
    r = s.one("""
        SELECT min(vlm_ms) AS vlm_min, percentile_cont(0.5) WITHIN GROUP (ORDER BY vlm_ms) AS vlm_p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY vlm_ms) AS vlm_p95, max(vlm_ms) AS vlm_max,
               min(pipeline_ms) AS all_min, percentile_cont(0.5) WITHIN GROUP (ORDER BY pipeline_ms) AS all_p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY pipeline_ms) AS all_p95, max(pipeline_ms) AS all_max,
               count(*) FILTER (WHERE pipeline_ms > 0)::int AS measured
        FROM tel WHERE pipeline_ms > 0
    """)
    return {k: (_num(v, 0) if k != "measured" else v) for k, v in r.items()}


def reading(s: Slice) -> Dict[str, Any]:
    """Documents & pages: in the dossiers → in documents that were read → actually cited."""
    d = s.one("""
        , docs AS (SELECT d.* FROM legal_lead_documents d JOIN tel ON tel.lead_id = d.lead_id)
        , cited AS (SELECT DISTINCT tel.lead_id, px->>'document_id' AS doc, px->>'page_number' AS page
                    FROM tel, jsonb_array_elements(tel.pxs) px)
        SELECT (SELECT count(*) FROM docs)::int                                         AS docs_total,
               (SELECT count(*) FROM docs WHERE processing_status = 'processed')::int  AS docs_read,
               (SELECT count(DISTINCT doc) FROM cited)::int                             AS docs_cited,
               (SELECT COALESCE(sum(page_count), 0) FROM docs)::int                     AS pages_total,
               (SELECT COALESCE(sum(page_count), 0) FROM docs WHERE processing_status = 'processed')::int AS pages_read,
               (SELECT count(*) FROM cited)::int                                        AS pages_cited,
               (SELECT COALESCE(round(sum(file_size_bytes) / 1048576.0, 1), 0) FROM docs)::float AS mb
    """)
    return d


# ── the table view behind every chart ─────────────────────────────────────────
def per_dossier(s: Slice, limit: int = 500) -> List[dict]:
    rows = s.rows("""
        SELECT tel.lead_id, COALESCE(NULLIF(tel.folder_name, ''), tel.lead_name, tel.lead_id) AS name,
               tel.batch_id, tel.batch_name, tel.folder_path, tel.status,
               tel.values_n, tel.cited_pages, tel.total_documents, tel.processed_documents,
               COALESCE(tel.tokens, 0)::int            AS tokens,
               COALESCE(tel.prompt_tokens, 0)::int     AS prompt_tokens,
               COALESCE(tel.completion_tokens, 0)::int AS completion_tokens,
               tel.vlm_ms, tel.pipeline_ms, p.tag AS script_tag, tel.updated_at
        FROM tel LEFT JOIN legal_field_provenance p ON p.lead_id = tel.lead_id
        ORDER BY tel.updated_at DESC LIMIT %s
    """, (limit,))
    names: Dict[str, str] = {}
    out = []
    for r in rows:
        if r["batch_id"] not in names:
            names[r["batch_id"]] = _run_name(r["batch_name"], r["folder_path"], None)
        out.append({
            "lead_id": r["lead_id"], "name": r["name"], "run": names[r["batch_id"]],
            "status": r["status"], "values": r["values_n"], "cited_pages": r["cited_pages"],
            "documents": r["total_documents"], "documents_read": r["processed_documents"],
            "tokens": r["tokens"], "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "vlm_ms": _num(r["vlm_ms"], 0), "pipeline_ms": _num(r["pipeline_ms"], 0),
            "script_tag": r["script_tag"],
            "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
        })
    return out


def runs_for_filter(scope: str) -> List[dict]:
    where = "is_test = true" if scope == "test" else ("TRUE" if scope == "all" else "is_test = false")
    with pg.pool().connection() as c:
        rows = c.execute(f"""
            SELECT batch_id, max(batch_name) AS batch_name, max(folder_path) AS folder_path,
                   max(folder_name) AS any_folder, min(created_at) AS started_at, count(*)::int AS n
            FROM legal_leads WHERE {where} GROUP BY batch_id ORDER BY started_at DESC LIMIT 50
        """).fetchall()
    return [{"batch_id": r["batch_id"], "name": _run_name(r["batch_name"], r["folder_path"], r["any_folder"]),
             "dossiers": r["n"], "started_at": r["started_at"].isoformat() if r.get("started_at") else None}
            for r in rows]


def snapshot(scope: str = "real", batch_id: Optional[str] = None, days: Optional[int] = None) -> Dict[str, Any]:
    init_schema()
    s = Slice(scope, batch_id, days)
    window = days if days else 14
    return {
        "workspace": "legal",
        "slice": {"scope": scope, "batch_id": batch_id, "days": days},
        "live": live(scope),
        "per_day": per_day(Slice(scope, batch_id, None), min(window, 60)),
        "outcomes": outcomes(s),
        "field_coverage": field_coverage(s),
        "values_histogram": values_histogram(s),
        "script_mix": script_mix(s),
        "tokens": tokens(s),
        "per_run": per_run(s),
        "timing": timing(s),
        "reading": reading(s),
        "per_dossier": per_dossier(s),
        "runs": runs_for_filter(scope),
        "circuit_breaker": get_breaker_status(),
    }
