"""
Observability & metrics for the SARFAESI Legal workspace.

Everything here is a read-only aggregate over what the pipeline already stored:

  legal_leads.extracted_data → `_telemetry`        model, tokens, VLM + pipeline latency
                             → `_page_extractions` which pages were cited, and their fields
  legal_lead_documents                             files, pages, sizes, what was read
  legal_field_provenance                           typed / handwritten verdicts

The numbers answer the operational questions for this use case: what does a dossier cost in
tokens, where does the time go, how much of each dossier was actually read, how much of the
data came off handwriting, and which runs behaved differently from the rest.
"""
from __future__ import annotations

from typing import Any, Dict, List

from db import pg
from workspaces.legal.db import init_schema
from workspaces.legal.logger import PgLegalLeadLogger
from workspaces.legal.ocr import get_breaker_status
from workspaces.legal import provenance

_logger = PgLegalLeadLogger()

# per-lead telemetry, unpacked from JSONB once and reused by every query below
_LEAD_TELEMETRY = """
    WITH lead AS (
        SELECT l.lead_id, l.batch_id, l.lead_name, l.folder_name, l.account_lan,
               l.status, l.created_at, l.updated_at, l.batch_name,
               COALESCE(l.extracted_data->'_telemetry', '{{}}'::jsonb) AS t,
               COALESCE(jsonb_array_length(l.extracted_data->'_page_extractions'), 0) AS cited_pages,
               (SELECT count(*) FROM jsonb_object_keys(
                    COALESCE(l.extracted_data, '{{}}'::jsonb)) k WHERE left(k, 1) <> '_'
                    AND k NOT IN ('page_extractions','telemetry')) AS field_count,
               l.total_documents, l.processed_documents, l.failed_documents
        FROM legal_leads l
        WHERE {scope}
    ), tel AS (
        SELECT lead.*,
               NULLIF(t->>'total_tokens','')::numeric       AS total_tokens,
               NULLIF(t->>'prompt_tokens','')::numeric      AS prompt_tokens,
               NULLIF(t->>'completion_tokens','')::numeric  AS completion_tokens,
               NULLIF(t->>'vlm_latency_ms','')::numeric     AS vlm_ms,
               NULLIF(t->>'pipeline_latency_ms','')::numeric AS pipeline_ms,
               NULLIF(t->>'model','')                        AS model
        FROM lead
    )
"""


def _scope_sql(scope: str, alias: str = "l") -> str:
    if scope == "test":
        return f"{alias}.is_test = true"
    if scope == "all":
        return "TRUE"
    return f"{alias}.is_test = false"


def _rows(sql: str, scope: str, params: tuple = ()) -> List[dict]:
    with pg.pool().connection() as c:
        return c.execute(_LEAD_TELEMETRY.format(scope=_scope_sql(scope)) + sql, params).fetchall()


def _one(sql: str, scope: str, params: tuple = ()) -> dict:
    rows = _rows(sql, scope, params)
    return rows[0] if rows else {}


def _f(v) -> float:
    return round(float(v), 1) if v is not None else 0.0


def totals(scope: str) -> Dict[str, Any]:
    """The headline numbers: volume, spend, speed, coverage."""
    r = _one("""
        SELECT count(*)::int                                   AS leads,
               count(*) FILTER (WHERE total_tokens > 0)::int    AS leads_with_telemetry,
               COALESCE(sum(total_tokens), 0)::bigint           AS tokens,
               COALESCE(sum(prompt_tokens), 0)::bigint          AS prompt_tokens,
               COALESCE(sum(completion_tokens), 0)::bigint      AS completion_tokens,
               COALESCE(avg(total_tokens), 0)::numeric          AS avg_tokens,
               COALESCE(max(total_tokens), 0)::numeric          AS max_tokens,
               COALESCE(sum(vlm_ms), 0)::numeric                AS vlm_ms,
               COALESCE(avg(vlm_ms), 0)::numeric                AS avg_vlm_ms,
               COALESCE(avg(pipeline_ms), 0)::numeric           AS avg_pipeline_ms,
               COALESCE(sum(cited_pages), 0)::int               AS cited_pages,
               COALESCE(avg(cited_pages), 0)::numeric           AS avg_cited_pages,
               COALESCE(avg(field_count), 0)::numeric           AS avg_fields,
               COALESCE(sum(field_count), 0)::int               AS fields,
               COALESCE(sum(total_documents), 0)::int           AS documents,
               COALESCE(sum(processed_documents), 0)::int       AS documents_read
        FROM tel
    """, scope)
    pages = _one("""
        SELECT COALESCE(sum(d.page_count), 0)::int AS pages
        FROM legal_lead_documents d JOIN tel ON tel.lead_id = d.lead_id
    """, scope)
    out = {k: (int(v) if isinstance(v, int) else _f(v)) for k, v in (r or {}).items()}
    out["pages_in_dossiers"] = int(pages.get("pages") or 0)
    out["tokens_per_cited_page"] = round(out["tokens"] / out["cited_pages"], 1) if out.get("cited_pages") else 0.0
    out["pages_read_pct"] = (round(100.0 * out["cited_pages"] / out["pages_in_dossiers"], 1)
                             if out["pages_in_dossiers"] else None)
    return out


def latency(scope: str) -> Dict[str, Any]:
    r = _one("""
        SELECT count(*) FILTER (WHERE vlm_ms > 0)::int AS n,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY vlm_ms)::numeric      AS vlm_p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY vlm_ms)::numeric      AS vlm_p95,
               max(vlm_ms)::numeric                                               AS vlm_max,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY pipeline_ms)::numeric AS pipe_p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY pipeline_ms)::numeric AS pipe_p95
        FROM tel WHERE vlm_ms IS NOT NULL
    """, scope)
    return {k: (int(v) if k == "n" else _f(v)) for k, v in (r or {}).items()}


def per_lead(scope: str, limit: int = 60) -> List[dict]:
    """The per-dossier table: tokens, latency, pages cited, fields — heaviest first."""
    rows = _rows("""
        SELECT lead_id, COALESCE(NULLIF(folder_name,''), lead_name, lead_id) AS name,
               batch_id, COALESCE(batch_name,'') AS batch_name, status, model,
               COALESCE(total_tokens,0)::int      AS tokens,
               COALESCE(prompt_tokens,0)::int     AS prompt_tokens,
               COALESCE(completion_tokens,0)::int AS completion_tokens,
               COALESCE(vlm_ms,0)::numeric        AS vlm_ms,
               COALESCE(pipeline_ms,0)::numeric   AS pipeline_ms,
               cited_pages, field_count, total_documents, processed_documents,
               to_char(updated_at, 'DD Mon HH24:MI') AS updated_at
        FROM tel ORDER BY COALESCE(total_tokens,0) DESC, updated_at DESC LIMIT %s
    """, scope, (limit,))
    for r in rows:
        r["vlm_ms"] = _f(r["vlm_ms"])
        r["pipeline_ms"] = _f(r["pipeline_ms"])
    return rows


def per_run(scope: str, limit: int = 12) -> List[dict]:
    """Cost and speed per run — the unit the team actually compares."""
    rows = _rows("""
        SELECT batch_id,
               COALESCE(NULLIF(max(batch_name),''), 'Untitled run') AS run_name,
               count(*)::int                              AS leads,
               COALESCE(sum(total_tokens),0)::bigint      AS tokens,
               COALESCE(sum(prompt_tokens),0)::bigint     AS prompt_tokens,
               COALESCE(sum(completion_tokens),0)::bigint AS completion_tokens,
               COALESCE(avg(total_tokens),0)::numeric     AS avg_tokens,
               COALESCE(avg(vlm_ms),0)::numeric           AS avg_vlm_ms,
               COALESCE(sum(cited_pages),0)::int          AS cited_pages,
               COALESCE(avg(field_count),0)::numeric      AS avg_fields,
               min(created_at)                            AS started_at
        FROM tel GROUP BY batch_id ORDER BY started_at DESC LIMIT %s
    """, scope, (limit,))
    for r in rows:
        r["avg_tokens"] = _f(r["avg_tokens"])
        r["avg_vlm_ms"] = _f(r["avg_vlm_ms"])
        r["avg_fields"] = _f(r["avg_fields"])
        r["started_at"] = r["started_at"].isoformat() if r.get("started_at") else None
    return rows


def field_coverage(scope: str, limit: int = 14) -> List[dict]:
    """How often each extracted field was actually found — the drift / prompt-quality signal."""
    rows = _rows("""
        SELECT k AS field, count(*)::int AS n,
               round(100.0 * count(*) / NULLIF((SELECT count(*) FROM tel), 0), 1)::float AS pct
        FROM tel, LATERAL jsonb_object_keys(
            COALESCE((SELECT l2.extracted_data FROM legal_leads l2 WHERE l2.lead_id = tel.lead_id), '{}'::jsonb)) k
        WHERE left(k, 1) <> '_' AND k NOT IN ('page_extractions','telemetry')
        GROUP BY k ORDER BY n DESC LIMIT %s
    """, scope, (limit,))
    return rows


def model_mix(scope: str) -> List[dict]:
    return _rows("""
        SELECT COALESCE(NULLIF(model,''), 'unknown') AS model, count(*)::int AS n,
               COALESCE(sum(total_tokens),0)::bigint AS tokens
        FROM tel GROUP BY 1 ORDER BY n DESC
    """, scope)


def throughput(scope: str, days: int = 14) -> List[dict]:
    rows = _rows("""
        SELECT to_char(date_trunc('day', updated_at), 'DD Mon') AS t,
               count(*)::int                         AS leads,
               COALESCE(sum(total_tokens),0)::bigint AS tokens
        FROM tel WHERE updated_at > now() - make_interval(days => %s)
        GROUP BY date_trunc('day', updated_at) ORDER BY date_trunc('day', updated_at)
    """, scope, (days,))
    return rows


def document_read_rate(scope: str) -> Dict[str, Any]:
    """Documents ingested vs actually read — with prompt scoping, most are deliberately skipped."""
    r = _one("""
        SELECT count(*)::int AS documents,
               count(*) FILTER (WHERE d.processing_status = 'processed')::int AS read,
               count(*) FILTER (WHERE d.processing_status = 'failed')::int    AS failed,
               COALESCE(sum(d.page_count), 0)::int                            AS pages,
               COALESCE(round(sum(d.file_size_bytes) / 1048576.0, 1), 0)::float AS mb
        FROM legal_lead_documents d JOIN tel ON tel.lead_id = d.lead_id
    """, scope)
    return r or {}


def snapshot(scope: str = "real") -> Dict[str, Any]:
    init_schema()
    return {
        "workspace": "legal",
        "scope": scope,
        "lead_counts": _logger.status_counts(scope=scope),
        "document_types": _logger.document_type_counts(scope=scope),
        "totals": totals(scope),
        "latency": latency(scope),
        "per_lead": per_lead(scope),
        "per_run": per_run(scope),
        "field_coverage": field_coverage(scope),
        "model_mix": model_mix(scope),
        "throughput": throughput(scope),
        "documents": document_read_rate(scope),
        "script_mix": provenance.tag_counts(scope),
        "circuit_breaker": get_breaker_status(),
    }
