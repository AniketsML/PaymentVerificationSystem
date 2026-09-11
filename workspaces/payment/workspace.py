"""
Payment Verification Workspace implementation.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from flask import Flask

from workspaces.base import Workspace
from db import pg
from pipeline import jobs
from observability.pg_logger import PgLeadLogger
from observability.pg_dedup import PaymentDedup
from ocr.medha_client import MedhaVisionOCR, PrecomputedOCR
from pipeline.orchestrator import process_lead


class PaymentWorkspace(Workspace):
    """Workspace dedicated to loan-repayment proof & payment receipt verification."""

    @property
    def id(self) -> str:
        return "payment"

    @property
    def name(self) -> str:
        return "Payment Verification"

    @property
    def short_name(self) -> str:
        return "Payment"

    @property
    def description(self) -> str:
        return "Verify loan repayment proofs, UPI receipts, and No-Dues certificates with zero false positives"

    @property
    def icon(self) -> str:
        return """<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>"""

    @property
    def default_route(self) -> str:
        return "/ws/payment"

    @property
    def badge(self) -> str:
        return "Zero FP"

    def init_schema(self) -> None:
        pg.init_schema()

    def claim_worker_job(self) -> Optional[Dict[str, Any]]:
        return jobs.claim_one()

    def process_worker_job(self, job: Dict[str, Any]) -> str:
        row = job["row_json"] or {}
        precomputed = bool(job.get("precomputed"))
        is_test = bool(job.get("is_test"))
        ocr = PrecomputedOCR() if precomputed else MedhaVisionOCR()
        logger = PgLeadLogger()
        dedup = PaymentDedup()
        res = process_lead(
            job["lead_id"],
            job.get("lender", ""),
            job.get("image_url", ""),
            row,
            ocr,
            logger,
            skip_image_qc=precomputed,
            dedup=dedup,
            is_test=is_test,
        )
        return res["verification_status"]

    def complete_worker_job(self, job_id: str, status: str) -> None:
        jobs.complete(job_id, status)

    def fail_worker_job(self, job_id: str, error: str) -> None:
        jobs.fail(job_id, error)
