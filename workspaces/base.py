"""
Base class for domain-specific workspaces.

A workspace defines its identity, routes, schema migrations, views,
and worker job-processing handlers. Adding a new workspace is a developer
activity implemented in Python code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from flask import Flask


class Workspace(ABC):
    """Abstract base class for all application workspaces."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Unique slug/identifier for the workspace (e.g. 'payment', 'legal')."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable full name (e.g. 'Payment Verification', 'Legal Notice Extraction')."""
        ...

    @property
    @abstractmethod
    def short_name(self) -> str:
        """Short label for badges & buttons (e.g. 'Payment', 'Legal')."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Brief description of the workspace's purpose."""
        ...

    @property
    @abstractmethod
    def icon(self) -> str:
        """SVG icon or icon name for navigation tabs and workspace switchers."""
        ...

    @property
    @abstractmethod
    def default_route(self) -> str:
        """Default UI endpoint or URL path for this workspace."""
        ...

    @property
    def badge(self) -> str:
        """Optional status / feature badge (e.g. 'Production', 'VLM Extractor')."""
        return ""

    def init_schema(self) -> None:
        """Idempotent database schema migration / table creation for this workspace."""
        pass

    def register_routes(self, app: Flask) -> None:
        """Register workspace-specific Flask routes / blueprints."""
        pass

    def claim_worker_job(self) -> Optional[Dict[str, Any]]:
        """Claim a single pending job from this workspace's queue, or return None."""
        return None

    def process_worker_job(self, job: Dict[str, Any]) -> str:
        """Execute processing on a claimed job and return its final status."""
        return "done"

    def complete_worker_job(self, job_id: str, status: str) -> None:
        """Mark a claimed job as complete in the database."""
        pass

    def fail_worker_job(self, job_id: str, error: str) -> None:
        """Mark a claimed job as failed / retriable in the database."""
        pass

    def purge_expired_data(self) -> Dict[str, int]:
        """Housekeeping: purge expired cache/test entries for this workspace."""
        return {}

    def to_dict(self) -> Dict[str, Any]:
        """Serialize workspace metadata for frontend consumers."""
        return {
            "id": self.id,
            "name": self.name,
            "short_name": self.short_name,
            "description": self.description,
            "icon": self.icon,
            "badge": self.badge,
            "default_route": self.default_route,
        }
