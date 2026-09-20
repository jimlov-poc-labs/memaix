# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for the approval outbox (FEATURE-WEB-UI-OUTBOX-AND-ADMIN.md, Fas C).

Approver-scoped end to end: list returns only actions THIS user may approve,
get/approve/reject return 403 for anyone else. A queued action's args carry
the full outgoing content, so visibility == approval right (THREAT-MODEL.md).
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...outbox.policy import can_approve
from ...paths import data_dir as _data_dir
from .. import routes as w


def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _get_acl():
    return w._get_acl()


def _json_401() -> JSONResponse:
    return w._json_401()


def _queue():
    from ...outbox.queue import ActionQueue

    db_path = Path(os.environ.get("MEMAIX_OUTBOX_DB", str(_data_dir() / "memaix-outbox.db")))
    return ActionQueue.for_path(db_path)


def _audit():
    from ...safety.audit import AuditLog

    db_path = Path(os.environ.get("MEMAIX_AUDIT_DB", str(_data_dir() / "memaix-audit.db")))
    return AuditLog.for_path(db_path)


def _forbidden() -> JSONResponse:
    return JSONResponse({"error": "forbidden"}, status_code=403)


def api_outbox_list(request: Request) -> JSONResponse:
    """GET /app/api/outbox?project=&status=pending → [actions this user may approve]"""
    user = _require_user(request)
    if not user:
        return _json_401()
    acl = _get_acl()
    project = request.query_params.get("project") or None
    status = request.query_params.get("status") or "pending"
    visible = set(acl.visible_projects(user))
    projects = [project] if project else sorted(visible)
    projects = [p for p in projects if p in visible]
    actions = [a for a in _queue().list(projects, status or None) if can_approve(acl, user, a)]
    return JSONResponse(actions)


def api_outbox_get(request: Request) -> JSONResponse:
    """GET /app/api/outbox/{id} → action | 404 | 403 (may not approve → may not read)"""
    user = _require_user(request)
    if not user:
        return _json_401()
    action = _queue().get(request.path_params["id"])
    if not action:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not can_approve(_get_acl(), user, action):
        return _forbidden()
    return JSONResponse(action)


def api_outbox_approve(request: Request) -> JSONResponse:
    """POST /app/api/outbox/{id}/approve → execute exactly once; 409 on race."""
    user = _require_user(request)
    if not user:
        return _json_401()
    acl = _get_acl()
    queue = _queue()
    action = queue.get(request.path_params["id"])
    if not action:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not can_approve(acl, user, action):
        return _forbidden()

    claimed = queue.claim_for_decision(action["id"], "approved", user)
    if claimed is None:
        decided = queue.get(action["id"]) or {}
        return JSONResponse(
            {"conflict": True, "decided_by": decided.get("decided_by"),
             "current_status": decided.get("status")},
            status_code=409,
        )

    from ...outbox.execute import execute_pending

    result = execute_pending(acl, claimed)
    ok = "error" not in result
    queue.record_result(action["id"], "executed" if ok else "failed", result)
    try:
        _audit().log(
            user, action["project"], f"outbox_execute:{action['tool']}", ok,
            "" if ok else str(result.get("error", "")),
        )
    except Exception:
        pass  # best effort — the decision itself is already persisted
    return JSONResponse({"ok": ok, "result": result})


async def api_outbox_reject(request: Request) -> JSONResponse:
    """POST /app/api/outbox/{id}/reject {reason} → never executes; 409 on race."""
    user = _require_user(request)
    if not user:
        return _json_401()
    acl = _get_acl()
    queue = _queue()
    action = queue.get(request.path_params["id"])
    if not action:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not can_approve(acl, user, action):
        return _forbidden()

    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str(body.get("reason", ""))

    claimed = queue.claim_for_decision(action["id"], "rejected", user, reason)
    if claimed is None:
        decided = queue.get(action["id"]) or {}
        return JSONResponse(
            {"conflict": True, "decided_by": decided.get("decided_by"),
             "current_status": decided.get("status")},
            status_code=409,
        )
    try:
        _audit().log(user, action["project"], f"outbox_reject:{action['tool']}", True, reason)
    except Exception:
        pass
    return JSONResponse({"ok": True, "status": "rejected"})
