"""
Central registry of application workspaces.

Provides methods for discovering, querying, initializing, and routing workspaces.
"""
from __future__ import annotations

from typing import Dict, List, Optional
from flask import Flask

from workspaces.base import Workspace

_REGISTRY: Dict[str, Workspace] = {}


def register_workspace(ws: Workspace) -> None:
    """Register a workspace instance."""
    if not isinstance(ws, Workspace):
        raise TypeError(f"Expected Workspace instance, got {type(ws)}")
    _REGISTRY[ws.id] = ws


def get_workspace(ws_id: str) -> Optional[Workspace]:
    """Retrieve a workspace by its unique ID."""
    return _REGISTRY.get(ws_id)


def get_all_workspaces() -> List[Workspace]:
    """Return all registered workspaces in registration order."""
    return list(_REGISTRY.values())


def get_workspace_ids() -> List[str]:
    """Return list of all registered workspace IDs."""
    return list(_REGISTRY.keys())


def init_all_schemas() -> None:
    """Initialize database schemas for all registered workspaces."""
    for ws in _REGISTRY.values():
        ws.init_schema()


def register_all_routes(app: Flask) -> None:
    """Register HTTP routes for all registered workspaces with Flask."""
    for ws in _REGISTRY.values():
        ws.register_routes(app)
