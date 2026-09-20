# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kanban board — Starlette route handlers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..acl import AccessDenied, Acl
from ..paths import data_dir as _data_dir
from ..safety.audit import AuditLog
from ..safety.rate_limit import rate_limiter
from . import store as s

_BOARD_HTML = Path(__file__).parent / "board.html"
_BOARD_HTML_RAW: str | None = None


def _board_html_with_locale(locale: str) -> str:
    global _BOARD_HTML_RAW
    if _BOARD_HTML_RAW is None:
        _BOARD_HTML_RAW = _BOARD_HTML.read_text(encoding="utf-8")
    from ..i18n import _load
    strings = _load(locale)
    inject = f'<script>window.I18N={json.dumps(strings, ensure_ascii=False)};</script>'
    return _BOARD_HTML_RAW.replace("<!--MEMAIX_I18N-->", inject, 1)
_ALLOWED_USERS: set[str] = set(
    os.environ.get("MEMAIX_ALLOWED_USERS", "alice").split(",")
)
_PASSWORD_HASH = os.environ.get("MEMAIX_LOGIN_PASSWORD_HASH", "")
_COOKIE_NAME = "memaix_board"
_COOKIE_TTL_DAYS = 1


_DEFAULT_SECRET = "dev-secret-change-me"  # nosec B105 -- sentinel we REFUSE in HTTP mode, not a credential


class BoardDisabled(Exception):
    """Board auth is misconfigured (default signing secret in HTTP mode) —
    fail closed by disabling the board rather than serving forgeable cookies."""


def _http_mode() -> bool:
    import sys
    return os.environ.get("MEMAIX_TRANSPORT") == "http" or "--http" in sys.argv


def _secret() -> bytes:
    raw = os.environ.get("HYDRA_SYSTEM_SECRET", _DEFAULT_SECRET)
    if raw == _DEFAULT_SECRET and _http_mode() and os.environ.get("MEMAIX_ALLOW_DEV_SECRET", "").lower() not in ("1", "true", "yes"):
        # In HTTP (served) mode the cookie-signing key MUST be a real secret;
        # the built-in default is public, so anyone could forge a session
        # cookie for any allowed user. Refuse rather than serve forgeable
        # auth. Set HYDRA_SYSTEM_SECRET (or MEMAIX_ALLOW_DEV_SECRET=1 for
        # local dev over http) to enable the board.
        raise BoardDisabled(
            "HYDRA_SYSTEM_SECRET is unset/default in HTTP mode — board disabled to avoid "
            "forgeable session cookies. Set a real HYDRA_SYSTEM_SECRET."
        )
    return raw.encode()[:32].ljust(32, b"0")


def _password_hash_for(user: str) -> str | None:
    """Per-user password hash (MEMAIX_LOGIN_PASSWORD_HASH_<USER>), falling back
    to the shared MEMAIX_LOGIN_PASSWORD_HASH ONLY when exactly one user is
    allowed — so a shared password can never authenticate as a *different*
    user in a multi-user board."""
    per_user = os.environ.get(f"MEMAIX_LOGIN_PASSWORD_HASH_{user.upper()}")
    if per_user:
        return per_user
    if len(_ALLOWED_USERS) == 1:
        return _PASSWORD_HASH or None
    return None


def _verify_password(user: str, provided: str) -> bool:
    password_hash = _password_hash_for(user)
    if not password_hash or ":" not in password_hash:
        return False
    salt_hex, key_hex = password_hash.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac("sha256", provided.encode(), salt, 200_000)
    return hmac.compare_digest(derived.hex(), key_hex)


def _make_cookie(user: str) -> str:
    day = int(time.time()) // 86400
    sig = hmac.new(_secret(), f"{user}:{day}".encode(), "sha256").hexdigest()[:32]
    return f"{user}:{day}:{sig}"


def _check_cookie(request: Request) -> str | None:
    raw = request.cookies.get(_COOKIE_NAME, "")
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    user, day_str, sig = parts
    try:
        day = int(day_str)
    except ValueError:
        return None
    today = int(time.time()) // 86400
    if abs(today - day) > _COOKIE_TTL_DAYS:
        return None
    try:
        secret = _secret()
    except BoardDisabled:
        return None  # fail closed — no valid session when the board is disabled
    expected = hmac.new(secret, f"{user}:{day}".encode(), "sha256").hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    if user not in _ALLOWED_USERS:
        return None
    return user


def _acl() -> Acl:
    from .. import config
    from ..acl import Acl
    cfg = config.load()
    return Acl.from_config(cfg["acl"])


def _user_projects(user: str, acl: Acl) -> list[str]:
    return acl.visible_projects(user)


def _audit() -> AuditLog:
    db_path = Path(os.environ.get("MEMAIX_AUDIT_DB", str(_data_dir() / "memaix-audit.db")))
    return AuditLog.for_path(db_path)


def _outbox():
    from ..outbox.queue import ActionQueue
    db_path = Path(os.environ.get("MEMAIX_OUTBOX_DB", str(_data_dir() / "memaix-outbox.db")))
    return ActionQueue.for_path(db_path)


def _timeline():
    from ..timeline.store import ActionsStore
    db_path = Path(os.environ.get("MEMAIX_ACTIONS_DB", str(_data_dir() / "memaix-actions.db")))
    return ActionsStore.for_path(db_path)


# Role table + visibility filter live in outbox/policy.py (single source of
# truth shared with server.py's MCP tools and the web-UI API).
from ..outbox.policy import APPROVAL_ROLE as _OUTBOX_APPROVAL_ROLE  # noqa: E402
from ..outbox.policy import can_approve as _can_approve  # noqa: E402

# ------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------


def _config_locale() -> str:
    try:
        from .. import config
        return config.load().get("memaix", {}).get("server", {}).get("locale", "en")
    except Exception:
        return "en"


async def board_login(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not rate_limiter.check(f"board_login:{username}", limit=5, window_s=600):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    if username not in _ALLOWED_USERS or not _verify_password(username, password):
        return JSONResponse({"error": "invalid credentials"}, status_code=401)

    try:
        cookie = _make_cookie(username)
    except BoardDisabled as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    resp = JSONResponse({"ok": True, "user": username})
    resp.set_cookie(
        _COOKIE_NAME, cookie,
        httponly=True, samesite="lax", secure=True, max_age=86400 * _COOKIE_TTL_DAYS,
    )
    return resp


def board_logout(request: Request) -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE_NAME)
    return resp


# ------------------------------------------------------------------
# API helpers
# ------------------------------------------------------------------


def _require_user(request: Request) -> str | None:
    return _check_cookie(request)


def _json_401() -> JSONResponse:
    return JSONResponse({"error": "not authenticated"}, status_code=401)


def _json_403() -> JSONResponse:
    return JSONResponse({"error": "access denied"}, status_code=403)


# ------------------------------------------------------------------
# API routes
# ------------------------------------------------------------------


def api_projects(request: Request) -> JSONResponse:
    user = _require_user(request)
    if not user:
        return _json_401()
    acl = _acl()
    return JSONResponse({"user": user, "projects": _user_projects(user, acl)})


def api_board(request: Request) -> JSONResponse:
    user = _require_user(request)
    if not user:
        return _json_401()

    project = request.query_params.get("project", "")
    sprint_filter = request.query_params.get("sprint", "")
    acl = _acl()

    if project not in _user_projects(user, acl):
        return _json_403()

    vault_str = acl.resource(project, "vault")
    if not vault_str:
        return JSONResponse({"error": "no vault for project"}, status_code=404)
    vault = Path(vault_str)

    cards = s.list_backlog(vault)

    # Sprint filter
    active_sprint_id = None
    if sprint_filter:
        sprints, detected_active = s.list_sprints(vault)
        if sprint_filter == "active":
            active_sprint_id = detected_active
            if active_sprint_id:
                sprint_items: list = next(
                    (sp["items"] for sp in sprints if sp["id"] == active_sprint_id), []
                )
                cards = [c for c in cards if c["id"] in sprint_items]
        else:
            sprint_items_set: set[str] = set()
            for sp in sprints:
                if sp["id"] == sprint_filter:
                    sprint_items_set = set(sp["items"])
                    active_sprint_id = sp["id"]
                    break
            if not sprint_items_set and sprint_filter:
                return JSONResponse({"error": f"unknown sprint: {sprint_filter}"}, status_code=400)
            if sprint_items_set:
                cards = [c for c in cards if c["id"] in sprint_items_set]

    # Group into columns, sort by value desc then updated_at desc
    col_map: dict[str, list[dict]] = {key: [] for key, _ in s.COLUMNS}
    for card in cards:
        st = card.get("status", "inbox")
        if st in col_map:
            col_map[st].append(card)
        else:
            col_map["inbox"].append(card)

    def _sort_key(c: dict):
        v = c.get("value") or 0
        return (-v, c.get("updated_at", ""))

    columns = [
        {
            "key":   key,
            "label": label,
            "muted": key == "rejected",
            "cards": sorted(col_map[key], key=_sort_key),
        }
        for key, label in s.COLUMNS
    ]

    return JSONResponse({
        "project":        project,
        "sprint":         active_sprint_id or sprint_filter or None,
        "columns":        columns,
        "total_cards":    len(cards),
    })


def api_sprints(request: Request) -> JSONResponse:
    user = _require_user(request)
    if not user:
        return _json_401()

    project = request.query_params.get("project", "")
    acl = _acl()
    if project not in _user_projects(user, acl):
        return _json_403()

    vault_str = acl.resource(project, "vault")
    if not vault_str:
        return JSONResponse({"sprints": [], "active": None})

    sprints, active = s.list_sprints(Path(vault_str))
    return JSONResponse({"sprints": sprints, "active": active})


def api_item(request: Request) -> JSONResponse:
    user = _require_user(request)
    if not user:
        return _json_401()

    item_id = request.path_params["id"]
    project = request.query_params.get("project", "")
    acl = _acl()
    if project not in _user_projects(user, acl):
        return _json_403()

    vault_str = acl.resource(project, "vault")
    if not vault_str:
        return JSONResponse({"error": "no vault"}, status_code=404)

    item = s.get_item(Path(vault_str), item_id)
    if item is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(item)


async def api_item_patch(request: Request) -> JSONResponse:
    user = _require_user(request)
    if not user:
        return _json_401()

    item_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    project = body.get("project", "")
    new_status = body.get("status", "")
    expected_version = body.get("expected_version")
    acl = _acl()

    if project not in _user_projects(user, acl):
        return _json_403()

    # Moving a card is a status transition — same authority as the MCP tool
    # backlog_set_status, which requires owner. Readers/collaborators may view
    # the board but must not mutate item state through it.
    try:
        acl.enforce(user, project, "owner")
    except AccessDenied:
        return _json_403()

    vault_str = acl.resource(project, "vault")
    if not vault_str:
        return JSONResponse({"error": "no vault"}, status_code=404)

    try:
        card = s.write_status(Path(vault_str), item_id, new_status, expected_version)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    if card.get("conflict"):
        return JSONResponse(card, status_code=409)

    old = card.pop("_old_status", "?")
    try:
        _audit().log(user, project, "board_move", True, f"{item_id}: {old} → {new_status}")
    except Exception:
        pass

    if old != "?" and old != new_status:
        try:
            _timeline().record(
                user, project, "board_move",
                f"Flyttade {item_id}: {old} → {new_status}",
                {"tool": "board_move", "args": {"item_id": item_id, "status": old}},
            )
        except Exception:
            pass  # best-effort — must never break the actual move

    return JSONResponse({"ok": True, "item": card})


def api_activity(request: Request) -> JSONResponse:
    user = _require_user(request)
    if not user:
        return _json_401()

    project = request.query_params.get("project", "")
    since = request.query_params.get("since", "")
    acl = _acl()

    try:
        events = _audit().tail(limit=50)
    except Exception:
        return JSONResponse({"events": []})

    if project:
        # Only show events for projects this user can read
        allowed = set(_user_projects(user, acl))
        events = [e for e in events if e["project"] == project and project in allowed]
    else:
        allowed = set(_user_projects(user, acl))
        events = [e for e in events if e["project"] in allowed]

    if since:
        events = [e for e in events if e["ts"] > since]

    return JSONResponse({"events": events})


# ------------------------------------------------------------------
# Outbox routes (FEATURE-APPROVAL-OUTBOX.md)
# ------------------------------------------------------------------


def api_outbox_list(request: Request) -> JSONResponse:
    user = _require_user(request)
    if not user:
        return _json_401()

    acl = _acl()
    project = request.query_params.get("project", "")
    status = request.query_params.get("status", "pending")
    visible = set(_user_projects(user, acl))
    projects = [project] if project else sorted(visible)
    projects = [p for p in projects if p in visible]

    # Approver-scoped like the MCP outbox_list: a queued action's args carry
    # the full email body/recipients, so only someone who could actually
    # approve it may see it — a reader must not read pending email content.
    actions = [a for a in _outbox().list(projects, status or None) if _can_approve(acl, user, a)]
    return JSONResponse({"actions": actions})


async def api_outbox_decide(request: Request) -> JSONResponse:
    user = _require_user(request)
    if not user:
        return _json_401()

    action_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    decision = body.get("decision", "")
    if decision not in ("approve", "reject"):
        return JSONResponse({"error": "decision must be 'approve' or 'reject'"}, status_code=400)

    acl = _acl()
    outbox = _outbox()
    action = outbox.get(action_id)
    if action is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if action["project"] not in _user_projects(user, acl):
        return _json_403()

    need = _OUTBOX_APPROVAL_ROLE.get(action["tool"], "owner")
    try:
        acl.enforce(user, action["project"], need)
    except AccessDenied:
        return _json_403()

    if decision == "reject":
        reason = body.get("reason", "")
        claimed = outbox.claim_for_decision(action_id, "rejected", user, reason)
        if claimed is None:
            return JSONResponse({"conflict": True}, status_code=409)
        try:
            _audit().log(user, action["project"], f"outbox_reject:{action['tool']}", True, reason)
        except Exception:
            pass
        return JSONResponse({"ok": True, "status": "rejected"})

    claimed = outbox.claim_for_decision(action_id, "approved", user)
    if claimed is None:
        return JSONResponse({"conflict": True}, status_code=409)

    from ..outbox.execute import execute_pending
    result = execute_pending(acl, claimed)
    ok = "error" not in result
    outbox.record_result(action_id, "executed" if ok else "failed", result)
    try:
        _audit().log(
            user, action["project"], f"outbox_execute:{action['tool']}", ok,
            "" if ok else str(result.get("error", "")),
        )
    except Exception:
        pass
    return JSONResponse({"ok": ok, "result": result})


# ------------------------------------------------------------------
# Route table
# ------------------------------------------------------------------

# NOTE: the /board page route moved to web/routes.py as a 301 → /app/board
# (FEATURE-WEB-UI-FOUNDATION.md); the board UI itself is served embedded at
# /app/board/frame. All /board/api/* and auth routes below are unchanged.
board_routes = [
    Route("/board/auth/login",      board_login,      methods=["POST"]),
    Route("/board/auth/logout",     board_logout,     methods=["POST"]),
    Route("/board/api/projects",    api_projects,     methods=["GET"]),
    Route("/board/api/board",       api_board,        methods=["GET"]),
    Route("/board/api/sprints",     api_sprints,      methods=["GET"]),
    Route("/board/api/item/{id}",   api_item,         methods=["GET"]),
    Route("/board/api/item/{id}",   api_item_patch,   methods=["PATCH"]),
    Route("/board/api/activity",    api_activity,     methods=["GET"]),
    Route("/board/api/outbox",      api_outbox_list,  methods=["GET"]),
    Route("/board/api/outbox/{id}", api_outbox_decide, methods=["POST"]),
]
