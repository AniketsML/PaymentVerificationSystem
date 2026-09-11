"""
Observability & metrics for the SARFAESI Legal workspace.
OCR route distribution, phase timings, property verification rates.
"""
from __future__ import annotations
from typing import Any, Dict
from db import pg
from workspaces.legal.db import init_schema
from workspaces.legal.logger import PgLegalLeadLogger
from workspaces.legal.ocr import get_breaker_status

_logger = PgLegalLeadLogger()


def snapshot(scope: str = "real") -> Dict[str, Any]:
    init_schema()
    counts = _logger.status_counts(scope=scope)
    doc_types = _logger.document_type_counts(scope=scope)
    where = "WHERE l.is_test=false" if scope == "real" else ("WHERE l.is_test=true" if scope == "test" else "")

    with pg.pool().connection() as c:
        # OCR route distribution
        ocr_rows = c.execute(
            f"SELECT COALESCE(NULLIF(d.ocr_route,''),'unknown') as route, count(*) as n "
            f"FROM legal_lead_documents d JOIN legal_leads l ON d.lead_id=l.lead_id "
            f"{where} AND d.processing_status='processed' GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()

        # Phase timing averages
        phase_rows = c.execute(
            f"SELECT e.stage, round(avg(e.ms)::numeric, 1) as avg_ms, count(*) as n "
            f"FROM legal_processing_events e JOIN legal_leads l ON e.lead_id=l.lead_id "
            f"{where} AND e.ms > 0 GROUP BY e.stage ORDER BY e.stage"
        ).fetchall()

        # Property verification status distribution
        prop_rows = c.execute(
            f"SELECT COALESCE(NULLIF(r.property_verification_status,''),'pending') as pv, count(*) as n "
            f"FROM legal_lead_results r {where.replace('l.is_test', 'r.is_test')} GROUP BY 1"
        ).fetchall()

    return {
        "workspace": "legal",
        "scope": scope,
        "lead_counts": counts,
        "document_types": doc_types,
        "ocr_routes": [{"route": r["route"], "n": r["n"]} for r in ocr_rows],
        "phase_timings": [{"stage": r["stage"], "avg_ms": float(r["avg_ms"]), "n": r["n"]} for r in phase_rows],
        "property_verification": [{"status": r["pv"], "n": r["n"]} for r in prop_rows],
        "circuit_breaker": get_breaker_status(),
    }
