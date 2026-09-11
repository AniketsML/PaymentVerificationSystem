"""
Workspaces package - extensible multi-workspace architecture.

Each workspace encapsulates its domain-specific data models, OCR prompts,
pipelines, queues, storage, APIs, and views, while sharing the central
authentication, database connection pool, and unified design system.
"""
from __future__ import annotations

from workspaces.base import Workspace
from workspaces.registry import (
    register_workspace,
    get_workspace,
    get_all_workspaces,
    get_workspace_ids,
    init_all_schemas,
    register_all_routes,
)
from workspaces.payment.workspace import PaymentWorkspace
from workspaces.legal.workspace import LegalWorkspace

# Register standard built-in workspaces
register_workspace(PaymentWorkspace())
register_workspace(LegalWorkspace())

__all__ = [
    "Workspace",
    "PaymentWorkspace",
    "LegalWorkspace",
    "register_workspace",
    "get_workspace",
    "get_all_workspaces",
    "get_workspace_ids",
    "init_all_schemas",
    "register_all_routes",
]
