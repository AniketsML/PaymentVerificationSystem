"""
SARFAESI Legal Lead Document Processing Workspace.
"""
from __future__ import annotations
from typing import Any, Dict, Optional
from flask import Flask
from workspaces.base import Workspace
from workspaces.legal.db import init_schema, purge_expired_legal_test_data
from workspaces.legal.jobs import claim_one_lead, complete_lead, fail_lead
from workspaces.legal.logger import PgLegalLeadLogger
from workspaces.legal.pipeline import process_lead
from workspaces.legal.routes import legal_bp


class LegalWorkspace(Workspace):
    @property
    def id(self) -> str: return "legal"
    @property
    def name(self) -> str: return "SARFAESI Lead Processing"
    @property
    def short_name(self) -> str: return "Legal"
    @property
    def description(self) -> str: return "SARFAESI Section 13(2) Loan Dossier Document Extraction"
    @property
    def icon(self) -> str:
        return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>'
    @property
    def default_route(self) -> str: return "/ws/legal"
    @property
    def badge(self) -> str: return "SARFAESI"

    def init_schema(self) -> None: init_schema()
    def register_routes(self, app: Flask) -> None: app.register_blueprint(legal_bp)

    def claim_worker_job(self) -> Optional[Dict[str, Any]]:
        return claim_one_lead()

    def process_worker_job(self, job: Dict[str, Any]) -> str:
        lead_id = job["lead_id"]
        lead_name = job.get("lead_name", "")
        folder_name = job.get("folder_name", "")
        documents = job.get("documents", [])
        is_test = bool(job.get("is_test"))
        extraction_prompt = job.get("extraction_prompt", "")
        logger = PgLegalLeadLogger()
        from workspaces.legal.ocr import LegalVLMClient
        ocr = LegalVLMClient()
        res = process_lead(
            lead_id, lead_name, folder_name, documents, ocr, logger,
            is_test=is_test, extraction_prompt=extraction_prompt,
        )
        return res.get("status", "failed")

    def complete_worker_job(self, job_id: str, status: str) -> None: complete_lead(job_id, status)
    def fail_worker_job(self, job_id: str, error: str) -> None: fail_lead(job_id, error)
    def purge_expired_data(self) -> Dict[str, int]: return purge_expired_legal_test_data()
