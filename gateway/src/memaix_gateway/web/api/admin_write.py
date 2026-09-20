# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin WRITE operations (MEX-025 Fas D): kill-switch, grants, project fields.

Every handler requires, in order: authenticated user → admin flag → valid MFA
cookie. Writes go through AclWriter (atomic, backed up), are audit-logged as
admin_* (never with secrets in detail), and finish with server.reload_acl()
so the change is live immediately.

Lockout guards (enforced here, the write path — acl.enforce stays dumb):
- an admin cannot disable themselves            → 409 self_disable
- the last enabled admin cannot be disabled     → 409 last_admin
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...acl import ROLES
from ...paths import data_dir as _data_dir
from .. import routes as w
from .mfa import mfa_verified

# Project fields the UI may write — everything else in acl.yaml stays
# hand-edited (vaults, backends, secrets are not click-editable by design).
_EDITABLE_PROJECT_FIELDS = {"allow_send", "outbox"}


def _require_admin_mfa(request: Request):
    """(user, acl) or an error response. Order: 401 → 403 admin → 403 mfa."""
    user = w._require_user(request)
    if not user:
        return None, w._json_401()
    acl = w._get_acl()
    if acl.is_disabled(user):
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    if not acl.is_admin(user):
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    if not mfa_verified(request, user):
        return None, JSONResponse({"error": "mfa_required"}, status_code=403)
    return (user, acl), None


def _acl_writer():
    from ... import config
    from ..acl_writer import AclWriter

    return AclWriter(config.CONFIG_DIR / "acl.yaml")


def _audit():
    from ...safety.audit import AuditLog

    db_path = Path(os.environ.get("MEMAIX_AUDIT_DB", str(_data_dir() / "memaix-audit.db")))
    return AuditLog.for_path(db_path)


def _reload():
    from ...server import reload_acl

    return reload_acl()


async def api_admin_set_disabled(request: Request) -> JSONResponse:
    """PATCH /app/api/admin/users/{uid} {disabled: bool} — the kill-switch."""
    ok, err = _require_admin_mfa(request)
    if err:
        return err
    user, acl = ok
    uid = request.path_params["uid"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    disabled = body.get("disabled")
    if not isinstance(disabled, bool):
        return JSONResponse({"error": "disabled must be a bool"}, status_code=400)
    if uid not in acl.users:
        return JSONResponse({"error": "not_found"}, status_code=404)

    if disabled:
        if uid == user:
            return JSONResponse({"error": "self_disable"}, status_code=409)
        enabled_admins = [
            u for u, d in acl.users.items()
            if d.get("admin") and not d.get("disabled") and u != uid
        ]
        if acl.is_admin(uid) and not enabled_admins:
            return JSONResponse({"error": "last_admin"}, status_code=409)

    _acl_writer().set_user_disabled(uid, disabled)
    _reload()
    _audit().log(user, "-", "admin_set_disabled", True, f"{uid}: disabled={disabled}")
    return JSONResponse({"ok": True, "user": uid, "disabled": disabled})


async def api_admin_set_grants(request: Request) -> JSONResponse:
    """PATCH /app/api/admin/users/{uid}/grants {grants: {project: role}}"""
    ok, err = _require_admin_mfa(request)
    if err:
        return err
    user, acl = ok
    uid = request.path_params["uid"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    grants = body.get("grants")
    if not isinstance(grants, dict):
        return JSONResponse({"error": "grants must be an object"}, status_code=400)
    if uid not in acl.users:
        return JSONResponse({"error": "not_found"}, status_code=404)
    for project, role in grants.items():
        if project not in acl.projects:
            return JSONResponse({"error": f"unknown project: {project}"}, status_code=400)
        if role not in ROLES:
            return JSONResponse({"error": f"unknown role: {role}"}, status_code=400)

    old = acl.grants(uid)
    _acl_writer().set_grants(uid, grants)
    _reload()
    _audit().log(user, "-", "admin_set_grants", True, f"{uid}: {old} → {grants}")
    return JSONResponse({"ok": True, "user": uid, "grants": grants})


async def api_admin_set_project_field(request: Request) -> JSONResponse:
    """PATCH /app/api/admin/projects/{project} {key, value}"""
    ok, err = _require_admin_mfa(request)
    if err:
        return err
    user, acl = ok
    project = request.path_params["project"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    key = body.get("key")
    value = body.get("value")
    if key not in _EDITABLE_PROJECT_FIELDS:
        return JSONResponse(
            {"error": f"key must be one of {sorted(_EDITABLE_PROJECT_FIELDS)}"}, status_code=400
        )
    if key == "allow_send" and not isinstance(value, bool):
        return JSONResponse({"error": "allow_send must be a bool"}, status_code=400)
    if key == "outbox" and value not in ("auto", "review"):
        return JSONResponse({"error": "outbox must be 'auto' or 'review'"}, status_code=400)
    if project not in acl.projects:
        return JSONResponse({"error": "not_found"}, status_code=404)

    old = acl.projects.get(project, {}).get(key)
    _acl_writer().set_project_field(project, key, value)
    _reload()
    _audit().log(user, project, "admin_set_project_field", True, f"{key}: {old} → {value}")
    return JSONResponse({"ok": True, "project": project, "key": key, "value": value})
