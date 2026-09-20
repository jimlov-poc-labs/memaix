# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for account linking + calendar mode — thin layer over
tools/account.py and tools/calendar.py."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...acl import AccessDenied
from ...tools import account as t_acc
from ...tools import calendar as t_cal
from .. import routes as w


# Resolved through the routes module at call time (not import-time binding) so
# there is exactly one auth/acl seam — tests patch web.routes and every api
# module follows.
def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _get_acl():
    return w._get_acl()


def _json_401() -> JSONResponse:
    return w._json_401()


def _token_store():
    from ...server import _get_token_store

    return _get_token_store()


def _public_url() -> str:
    from ... import config

    return config.load().get("memaix", {}).get("server", {}).get("public_url", "")


def _capabilities_for(provider: str) -> list[str]:
    from ...connectors.registry import default_registry

    return default_registry().capabilities_for_provider(provider)


def api_accounts_list(request: Request) -> JSONResponse:
    """GET /app/api/accounts → [{provider, account, status, scopes, readonly?, project?}]

    account_list(...) (token store) returns everything the user has linked
    via account_link/account_link_imap, OAuth or not — this now includes
    per-user IMAP mailboxes (provider='imap', unlinkable like any other
    entry). Project-shared IMAP mailboxes (acl.yaml's static `mailbox`
    resource) are a *different* thing — a project config, not a per-user
    link — so they're read separately below and returned as readonly
    entries the UI can display without offering an unlink action.
    """
    user = _require_user(request)
    if not user:
        return _json_401()
    acl = _get_acl()
    oauth = t_acc.account_list(acl, user, _token_store())

    # `capabilities` is what the account COULD be shared for;
    # `scopes_by_capability` (already on each entry) is what it IS shared
    # for. The settings UI needs both to draw an unchecked box — an empty
    # scope list against a non-empty capability list is precisely the
    # "linked but shared with nothing" state opt-in is supposed to produce.
    for entry in oauth:
        entry["capabilities"] = _capabilities_for(entry.get("provider", ""))

    imap: list[dict] = []
    for proj in acl.visible_projects(user):
        mailbox = acl.resource(proj, "mailbox")
        if isinstance(mailbox, dict) and mailbox.get("user"):
            imap.append({
                "provider": "imap",
                "account": mailbox["user"],
                "status": "configured",
                "project": proj,
                "readonly": True,
            })

    return JSONResponse(oauth + imap)


def api_accounts_link(request: Request) -> JSONResponse:
    """GET /app/api/accounts/link/{provider} → {url} (opened in a new window)"""
    user = _require_user(request)
    if not user:
        return _json_401()
    provider = request.path_params["provider"]
    try:
        result = t_acc.account_link(_get_acl(), user, provider, _public_url())
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"url": result.get("link_url", "")})


def api_accounts_unlink(request: Request) -> JSONResponse:
    """DELETE /app/api/accounts/{provider}?account=X → {ok}"""
    user = _require_user(request)
    if not user:
        return _json_401()
    provider = request.path_params["provider"]
    account = request.query_params.get("account", "")
    try:
        result = t_acc.account_unlink(_get_acl(), user, provider, account, _token_store())
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(result)


async def api_accounts_link_imap(request: Request) -> JSONResponse:
    """POST /app/api/accounts/link-imap {account_email, host, user, password, port?}
    → {ok, provider, account}

    Cookie-authed, non-OAuth link path for a per-user IMAP mailbox
    (FEATURE-CONNECTOR-FRAMEWORK.md — IMAP as per_user, imap_user connector
    type). The password is read from the request body here and handed
    straight to tools/account.account_link_imap for encrypted storage — it
    is never logged (no log line in this handler touches `body`) and never
    echoed back: the JSON response only ever contains {ok, provider,
    account}, matching what account_link_imap itself returns.
    """
    user = _require_user(request)
    if not user:
        return _json_401()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)

    account_email = body.get("account_email", "")
    host = body.get("host", "")
    imap_user = body.get("user", "")
    password = body.get("password", "")
    port = body.get("port")

    try:
        result = t_acc.account_link_imap(
            user, account_email, host, imap_user, password, _token_store(), port=port,
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def api_accounts_scope_set(request: Request) -> JSONResponse:
    """POST /app/api/accounts/scopes {provider, account, capability, projects}
    → {ok, provider, account, capability, projects}

    `projects` REPLACES the grant set for that one capability: [] revokes
    it entirely, ["*"] means every project the user can currently see.
    Other capabilities on the same account are untouched — that separation
    is the whole point, so sharing a calendar never hands over the mailbox
    behind the same OAuth token.

    Each named project is checked against the caller's own access in
    tools/account.account_scope_set, so this cannot be used to stage a
    source into a project the caller can't reach themselves.
    """
    user = _require_user(request)
    if not user:
        return _json_401()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)

    projects = body.get("projects")
    if not isinstance(projects, list) or any(not isinstance(p, str) for p in projects):
        return JSONResponse({"error": "projects must be a list of strings"}, status_code=400)

    try:
        result = t_acc.account_scope_set(
            _get_acl(), user,
            body.get("provider", ""), body.get("account", ""),
            body.get("capability", ""), projects, _token_store(),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(result)


def api_calendar_mode_get(request: Request) -> JSONResponse:
    """GET /app/api/settings/calendar-mode?project=X → {active_mode, details, available_modes}"""
    user = _require_user(request)
    if not user:
        return _json_401()
    project = request.query_params.get("project", "")
    try:
        status = t_cal.get_status(user, project, _get_acl(), _token_store())
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(status)


async def api_calendar_mode_set(request: Request) -> JSONResponse:
    """POST /app/api/settings/calendar-mode {project, mode, ical_url?, calendar_id?}"""
    user = _require_user(request)
    if not user:
        return _json_401()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    try:
        result = t_cal.setup_mode(
            _get_acl(), user, body.get("project", ""), body.get("mode", ""),
            _token_store(), _public_url(),
            ical_url=body.get("ical_url"), calendar_id=body.get("calendar_id"),
        )
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)
