# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for the action timeline + undo (MEX-025 Fas D).

Reuses the real primitives: ActionsStore.list for the feed and
timeline.undo.undo for reversal — the SAME role rule as the MCP tool
timeline_undo (the undo function enforces the role the original action
required). No duplicated authority logic in this layer."""

from __future__ import annotations

import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...acl import AccessDenied
from .. import routes as w


def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _timeline_store():
    from ...timeline.store import ActionsStore

    db_path = Path(os.environ.get("MEMAIX_ACTIONS_DB", "/tmp/memaix-actions.db"))  # nosec B108 -- same default as board/routes
    return ActionsStore.for_path(db_path)


def api_timeline(request: Request) -> JSONResponse:
    """GET /app/api/timeline?project=X&limit=50 → [actions, newest first]"""
    user = _require_user(request)
    if not user:
        return w._json_401()
    acl = w._get_acl()
    project = request.query_params.get("project") or None
    try:
        limit = min(int(request.query_params.get("limit", 50)), 200)
    except ValueError:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    visible = set(acl.visible_projects(user))
    projects = [project] if project else sorted(visible)
    projects = [p for p in projects if p in visible]
    return JSONResponse(_timeline_store().list(projects, limit))


def api_timeline_undo(request: Request) -> JSONResponse:
    """POST /app/api/timeline/{id}/undo → undo result; 403/404/409/422 mapped."""
    user = _require_user(request)
    if not user:
        return w._json_401()
    acl = w._get_acl()
    from ...timeline.undo import undo

    try:
        result = undo(_timeline_store(), acl, user, request.path_params["id"])
    except FileNotFoundError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    if result.get("conflict"):
        return JSONResponse(result, status_code=409)
    if result.get("error") == "irreversible":
        return JSONResponse(result, status_code=422)
    return JSONResponse(result)
