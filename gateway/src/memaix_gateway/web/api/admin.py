# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for the admin read views (FEATURE-WEB-UI-OUTBOX-AND-ADMIN.md, Fas C).

Read-only in this phase: users, projects, audit log, system health. Every
handler calls _require_admin explicitly — no middleware, simpler to audit.
Write operations (grants, kill-switch) come in Fas D behind MFA.
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...paths import data_dir as _data_dir
from .. import routes as w


def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _get_acl():
    return w._get_acl()


def _require_admin(request: Request):
    """Return (user, acl) or an error JSONResponse (401/403)."""
    user = _require_user(request)
    if not user:
        return None, w._json_401()
    acl = _get_acl()
    # Kill-switch must apply to web sessions too — is_admin() alone doesn't
    # check disabled, only enforce() does. A disabled admin keeps their cookie
    # for up to 2 days and could otherwise reach admin write paths unhindered.
    if acl.is_disabled(user):
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    if not acl.is_admin(user):
        return None, JSONResponse({"error": "forbidden"}, status_code=403)
    return (user, acl), None


def _audit_log():
    from ...safety.audit import AuditLog

    db_path = Path(os.environ.get("MEMAIX_AUDIT_DB", str(_data_dir() / "memaix-audit.db")))
    return AuditLog.for_path(db_path)


def api_admin_users(request: Request) -> JSONResponse:
    """GET /app/api/admin/users → [{id, admin, disabled, grants}] (no secrets)"""
    ok, err = _require_admin(request)
    if err:
        return err
    _, acl = ok
    users = [
        {
            "id": uid,
            "admin": bool(u.get("admin", False)),
            "disabled": bool(u.get("disabled", False)),
            "grants": u.get("grants", {}),
        }
        for uid, u in sorted(acl.users.items())
    ]
    return JSONResponse(users)


def api_admin_projects(request: Request) -> JSONResponse:
    """GET /app/api/admin/projects → [{name, allow_send, outbox, users, vault}]"""
    ok, err = _require_admin(request)
    if err:
        return err
    _, acl = ok
    projects = []
    for name, p in sorted(acl.projects.items()):
        user_count = sum(1 for u in acl.users.values() if name in (u.get("grants") or {}))
        projects.append(
            {
                "name": name,
                "allow_send": bool(p.get("allow_send", False)),
                "outbox": p.get("outbox", "auto"),
                "users": user_count,
                "vault": p.get("vault", ""),
            }
        )
    return JSONResponse(projects)


def api_admin_audit(request: Request) -> JSONResponse:
    """GET /app/api/admin/audit?user=&project=&tool=&ok=&since=&offset=&limit=
    → {entries, has_more}"""
    ok, err = _require_admin(request)
    if err:
        return err
    p = request.query_params
    ok_filter: bool | None = None
    if p.get("ok") == "true":
        ok_filter = True
    elif p.get("ok") == "false":
        ok_filter = False
    try:
        limit = min(int(p.get("limit", 50)), 500)
        offset = int(p.get("offset", 0))
    except ValueError:
        return JSONResponse({"error": "bad_request"}, status_code=400)

    filters = dict(
        user=p.get("user") or None,
        project=p.get("project") or None,
        tool=p.get("tool") or None,
        ok=ok_filter,
        since=p.get("since") or None,
    )
    log = _audit_log()
    entries = log.query(limit=limit, offset=offset, **filters)
    has_more = len(log.query(limit=1, offset=offset + limit, **filters)) > 0
    return JSONResponse({"entries": entries, "has_more": has_more})


def api_admin_system(request: Request) -> JSONResponse:
    """GET /app/api/admin/system → {checks: [{name, status, message}]}"""
    ok, err = _require_admin(request)
    if err:
        return err
    from ...doctor import run_all

    try:
        checks = [c._asdict() for c in run_all()]
    except Exception as exc:
        return JSONResponse({"checks": [], "error": str(exc)}, status_code=500)
    return JSONResponse({"checks": checks})
