# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP-transportens Starlette-app: handlers, rutter, lifespan och middleware.

Utbrutet ur server.build_http_app (Sonar S3776). Modulen läses in först när
build_http_app anropas, så den kan importera server utan cirkelberoende.
Allt som tester eller drift byter ut på server-modulen (_get_acl, _get_rules,
_rate_limiter, _get_token_store, logger m.fl.) slås upp i anropsögonblicket
via ``_server`` — aldrig bundet vid import."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from html import escape as _html_escape
from urllib.parse import urlencode, urlparse

from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import config
from . import server as _server
from .server import _DEFAULT_ISSUER, _DEFAULT_PUBLIC_URL, _GOOGLE_TOKEN_URI

_HYDRA = "http://hydra:4444"
_REGISTER_PATH = "/oauth2/register"
_DCR_URL = _HYDRA + _REGISTER_PATH
_ISSUER = "issuer"
_ERROR = "error"
_AUTH = "auth"
_SERVER_ERROR = "server_error"
_CLIENT_ID = "client_id"
_STATE = "state"
_RESOURCE_URL = "resource_server_url"
_GOOGLE = "google"
_MICROSOFT = "microsoft"

_PROVIDER_AUTH_URLS = {
    _GOOGLE: "https://accounts.google.com/o/oauth2/v2/auth",
    _MICROSOFT: "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
}
_PROVIDER_TOKEN_URLS = {
    _GOOGLE: _GOOGLE_TOKEN_URI,
    _MICROSOFT: "https://login.microsoftonline.com/common/oauth2/v2.0/token",
}


# ----------------------------------------------------------------------
# Små hjälpare
# ----------------------------------------------------------------------


def _json_error(code: str, status: int) -> JSONResponse:
    return JSONResponse({_ERROR: code}, status_code=status)


def _memaix_cfg(cfg: dict) -> dict:
    return cfg.get("memaix", {})


def _auth_cfg() -> dict:
    return _memaix_cfg(config.load()).get(_AUTH, {})


def _issuer_or_default() -> str:
    return _auth_cfg().get(_ISSUER, _DEFAULT_ISSUER).rstrip("/")


async def _json_body(request: Request):
    try:
        return await request.json()
    except Exception:
        return {}


def _passthrough_authorization(request: Request) -> dict:
    if "authorization" in request.headers:
        return {"Authorization": request.headers["authorization"]}
    return {}


# ----------------------------------------------------------------------
# Custom HTTP handlers
# ----------------------------------------------------------------------


def health_handler(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "memaix"})


def protected_resource_handler(request: Request) -> JSONResponse:
    """RFC 9728 protected resource metadata.

    FastMCP auto-generates this from AuthSettings.resource_server_url, but
    Pydantic's AnyHttpUrl always adds a trailing slash.  That creates a
    mismatch when claude.ai validates the JWT aud claim against the
    connector URL (typically typed without a trailing slash).  We override
    it here so the resource value is canonical without a trailing slash,
    which matches both forms.
    """
    auth_cfg = _auth_cfg()
    issuer = auth_cfg.get(_ISSUER, "https://mcp.example.com/").rstrip("/")
    resource = auth_cfg.get(_RESOURCE_URL, issuer + "/").rstrip("/")
    return JSONResponse(
        {
            "resource": resource,
            "authorization_servers": [issuer + "/"],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def as_metadata_handler(request: Request) -> JSONResponse:
    """Serve OAuth AS metadata with registration_endpoint injected.

    Hydra v2 doesn't advertise registration_endpoint in its discovery
    document even when DCR is enabled — this handler proxies Hydra's
    openid-configuration and adds the missing field.
    """
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_HYDRA}/.well-known/openid-configuration",
                timeout=5.0,
            )
            metadata = resp.json()
    except Exception:
        # Fallback: return minimal metadata so discovery doesn't hard-fail
        metadata = {_ISSUER: _auth_cfg().get(_ISSUER, _DEFAULT_ISSUER)}

    issuer = metadata.get(_ISSUER, _DEFAULT_ISSUER).rstrip("/")
    metadata["registration_endpoint"] = issuer + _REGISTER_PATH
    return JSONResponse(metadata)


async def dcr_handler(request: Request) -> JSONResponse:
    """Proxy DCR to Hydra, injecting resource audience so JWTs include aud claim.

    Hydra issues JWTs without aud unless the client's audience list explicitly
    contains the resource URL. This handler ensures every dynamically registered
    client is whitelisted for https://mcp.example.com (with and without trailing
    slash) before forwarding to Hydra's public DCR endpoint.
    """
    return await _dcr_vidare(request, "POST", _DCR_URL)


async def dcr_klient_handler(request: Request) -> Response:
    """RFC 7592-hanteringen av en registrerad klient (GET/PUT/DELETE).

    Hydra kräver klientens registration access token; den följer med i
    Authorization-huvudet. PUT får samma spärr och audience som POST —
    annars kunde en klient registrera sig som authorization_code och
    sedan byta till client_credentials.
    """
    import httpx
    client_id = request.path_params[_CLIENT_ID]
    url = f"{_DCR_URL}/{client_id}"
    if request.method == "PUT":
        return await _dcr_vidare(request, "PUT", url)
    headers = _passthrough_authorization(request)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(request.method, url, headers=headers, timeout=10.0)
    except Exception as exc:
        _server.logger.warning("DCR proxy error: %s", exc)
        return _json_error(_SERVER_ERROR, 500)
    if request.method == "GET" and resp.status_code == 200:
        return JSONResponse(_server._stada_dcr_svar(resp.json()), status_code=200)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


async def _dcr_vidare(request: Request, method: str, url: str) -> JSONResponse:
    import httpx
    body = await _json_body(request)

    skal = _server._dcr_avvisning(body)
    if skal:
        _server.logger.warning("DCR nekad (%s): %s", method, skal)
        return JSONResponse(
            {_ERROR: "invalid_client_metadata", "error_description": skal},
            status_code=400,
        )

    issuer = _issuer_or_default()
    resource_urls = [f"{issuer}/", issuer]
    existing = body.get("audience") or []
    body["audience"] = list({*existing, *resource_urls})

    headers = {"Content-Type": "application/json"}
    if method == "PUT":
        headers.update(_passthrough_authorization(request))
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(method, url, json=body, headers=headers, timeout=10.0)
            return JSONResponse(_server._stada_dcr_svar(resp.json()), status_code=resp.status_code)
    except Exception as exc:
        _server.logger.warning("DCR proxy error: %s", exc)
        return _json_error(_SERVER_ERROR, 500)


def _provider_settings(provider: str) -> tuple[dict, str, str]:
    """(providerns config, publik URL, redirect-URI) för en OAuth-koppling."""
    memaix_cfg = _memaix_cfg(config.load())
    provider_cfg = memaix_cfg.get("oauth_providers", {}).get(provider, {})
    public_url = memaix_cfg.get("server", {}).get("public_url", _DEFAULT_PUBLIC_URL)
    return provider_cfg, public_url, f"{public_url.rstrip('/')}/link/{provider}/callback"


def link_start(request: Request) -> "RedirectResponse | JSONResponse":
    """Start OAuth flow for a provider."""
    provider = request.path_params["provider"]
    state = request.query_params.get(_STATE, "")

    if provider not in _PROVIDER_AUTH_URLS:
        return _json_error("unknown_provider", 400)

    provider_cfg, _public_url, redirect_uri = _provider_settings(provider)
    params = {
        _CLIENT_ID: provider_cfg.get(_CLIENT_ID, ""),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        _STATE: state,
        "scope": " ".join(provider_cfg.get("scopes", [])),
        "access_type": "offline",
        "prompt": "consent",
    }
    return RedirectResponse(_PROVIDER_AUTH_URLS[provider] + "?" + urlencode(params))


async def _exchange_code(token_url: str, form: dict) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=10) as http_client:
        resp = await http_client.post(token_url, data=form)
    resp.raise_for_status()
    return resp.json()


def _link_success_html(provider: str, account_email: str, public_url: str) -> str:
    # HTML-escape values that ultimately derive from an IdP claim
    # (account_email) or the provider string before embedding them in the
    # success page — an attacker-controlled claim must not inject markup.
    provider_label = _html_escape({_GOOGLE: "Google", _MICROSOFT: "Microsoft"}.get(provider, provider.title()))
    account_email = _html_escape(account_email or "")
    board_url = public_url.rstrip("/") + "/board"
    return f"""<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Konto kopplat — Memaix</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f1117; color: #e2e8f0;
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
    }}
    .card {{
      background: #1a1f2e; border: 1px solid #2d3748; border-radius: 12px;
      padding: 2.5rem 3rem; max-width: 420px; width: 90%; text-align: center;
    }}
    .icon {{
      width: 56px; height: 56px; border-radius: 50%;
      background: #1a3a2a; border: 2px solid #38a169;
      display: flex; align-items: center; justify-content: center;
      margin: 0 auto 1.5rem;
      font-size: 1.6rem;
    }}
    h1 {{ font-size: 1.25rem; font-weight: 600; margin-bottom: .5rem; color: #f7fafc; }}
    .provider {{ color: #68d391; font-weight: 600; }}
    .account {{
      margin: 1.25rem auto 0;
      background: #0f1117; border: 1px solid #2d3748; border-radius: 8px;
      padding: .6rem 1rem; font-size: .85rem; color: #a0aec0;
      word-break: break-all;
    }}
    .account span {{ color: #e2e8f0; }}
    .actions {{ margin-top: 2rem; display: flex; gap: .75rem; justify-content: center; flex-wrap: wrap; }}
    a.btn {{
      display: inline-block; padding: .55rem 1.25rem; border-radius: 8px;
      font-size: .875rem; font-weight: 500; text-decoration: none; cursor: pointer;
    }}
    a.btn-primary {{ background: #2b6cb0; color: #fff; }}
    a.btn-primary:hover {{ background: #2c5282; }}
    a.btn-ghost {{ border: 1px solid #2d3748; color: #a0aec0; }}
    a.btn-ghost:hover {{ background: #2d3748; color: #e2e8f0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✓</div>
    <h1><span class="provider">{provider_label}</span> kopplat</h1>
    <p style="color:#a0aec0;font-size:.9rem;margin-top:.4rem">Ditt konto är länkat och redo att användas.</p>
    <div class="account">Inloggad som <span>{account_email or "okänt konto"}</span></div>
    <div class="actions">
      <a class="btn btn-primary" href="{board_url}">Tillbaka till board</a>
      <a class="btn btn-ghost" href="javascript:window.close()">Stäng fliken</a>
    </div>
  </div>
</body>
</html>"""


async def link_callback(request: Request) -> Response:
    """Handle OAuth callback: exchange code for tokens and store them."""
    provider = request.path_params["provider"]
    code = request.query_params.get("code", "")
    state = request.query_params.get(_STATE, "")
    error = request.query_params.get(_ERROR, "")

    if error:
        return _json_error(error, 400)

    from .tools.account import validate_state
    pending = validate_state(state)
    if not pending:
        return _json_error("invalid_or_expired_state", 400)

    user_id = pending["user_id"]
    provider_cfg, public_url, redirect_uri = _provider_settings(provider)
    token_url = _PROVIDER_TOKEN_URLS.get(provider, "")
    client_secret = config.secret(provider_cfg.get("client_secret_ref", "")) or ""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        _CLIENT_ID: provider_cfg.get(_CLIENT_ID, ""),
        "client_secret": client_secret,
    }
    try:
        token_data = await _exchange_code(token_url, form)
    except Exception as exc:
        # Don't echo the raw exception (can carry internal URLs / response
        # fragments) back to the caller — log it, return a generic error.
        _server.logger.warning("OAuth token exchange failed for provider %s: %s", provider, exc)
        return _json_error("token_exchange_failed", 500)

    account_email = token_data.get("email", "") or _server._get_account_email(provider, token_data)
    _server._get_token_store().store(user_id, provider, account_email, _server._stamp_expiry(token_data))
    return HTMLResponse(_link_success_html(provider, account_email, public_url))


def _webhook_event(token: str, body: dict) -> dict:
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {
        "type": "webhook", "project": None, "id": f"webhook:{token}:{digest}",
        "payload": {**body, "token": token},
    }


async def rule_webhook(request: Request) -> JSONResponse:
    """Inbound trigger for webhook-type automation rules (FEATURE-AUTOMATION-RULES.md §6).

    The token is itself the shared secret (a random 'token' generated when
    the rule was created), compared in constant time (rules/match.py).
    Rate-limited per client IP so the token can't be brute-forced.

    Token resolution order:
      1. X-Webhook-Token header (preferred — keeps secret out of server logs)
      2. URL path param /hooks/{token} (backward-compatible, deprecated)
    """
    # Unauthenticated endpoint — rate-limit per client IP so a valid token
    # can't be guessed by volume (30 attempts / 60 s).
    client_ip = request.client.host if request.client else "unknown"
    if not _server._rate_limiter.check(f"webhook:{client_ip}", limit=30, window_s=60):
        return _json_error("rate_limited", 429)

    # Prefer header over URL path so the token never appears in access logs.
    token = request.headers.get("X-Webhook-Token") or request.path_params.get("token", "")
    if not token:
        return _json_error("missing webhook token", 401)
    event = _webhook_event(token, await _json_body(request))
    from .rules.engine import evaluate
    results = evaluate(_server._get_rules(), _server._get_acl(), event)
    if not results:
        return _json_error("no matching enabled rule for this token", 404)
    return JSONResponse({"ok": True, "matched": len(results)})


# ----------------------------------------------------------------------
# Rutter
# ----------------------------------------------------------------------


def custom_routes() -> list:
    """Egna rutter i den ordning FastMCP lägger dem efter sina inbyggda."""
    from .board.routes import board_routes
    from .booking.routes import booking_routes
    from .web.routes import web_routes

    return [
        Route("/health", health_handler),
        Route("/.well-known/oauth-authorization-server", as_metadata_handler),
        Route(_REGISTER_PATH, dcr_handler, methods=["POST"]),
        Route(f"{_REGISTER_PATH}/{{client_id}}", dcr_klient_handler, methods=["GET", "PUT", "DELETE"]),
        Route("/link/{provider}", link_start),
        Route("/link/{provider}/callback", link_callback),
        Route("/hooks", rule_webhook, methods=["POST"]),
        Route("/hooks/{token}", rule_webhook, methods=["POST"]),
        *board_routes,
        *web_routes,
        *booking_routes,
    ]


# ----------------------------------------------------------------------
# MCP-inställningar
# ----------------------------------------------------------------------


def configure_mcp_settings(cfg: dict) -> None:
    """Auth, DNS-rebinding-skydd och monteringsväg för FastMCP."""
    mcp = _server.mcp
    auth_cfg = _memaix_cfg(cfg).get(_AUTH, {})

    if auth_cfg.get(_ISSUER):
        from mcp.server.auth.settings import AuthSettings

        from .auth.token import HydraTokenVerifier
        verifier = HydraTokenVerifier.from_config(cfg)
        mcp.settings.auth = AuthSettings(
            issuer_url=auth_cfg[_ISSUER],
            resource_server_url=auth_cfg.get(_RESOURCE_URL, auth_cfg[_ISSUER]),
        )
        mcp._token_verifier = verifier

    # FastMCP's DNS rebinding protection defaults to allowed_hosts=[] when binding
    # to 0.0.0.0 (as opposed to localhost), which causes 421 for every real hostname.
    # Explicitly allow the public host extracted from resource_server_url.
    from mcp.server.transport_security import TransportSecuritySettings
    pub_host = urlparse(
        auth_cfg.get(_RESOURCE_URL, auth_cfg.get(_ISSUER, ""))
    ).netloc or "mcp.example.com"
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[pub_host],
    )

    # Mount at root so claude.ai finds the endpoint at the connector URL directly.
    mcp.settings.streamable_http_path = "/"


# ----------------------------------------------------------------------
# Bakgrundsloopar i lifespan
# ----------------------------------------------------------------------


def _brief_loop():
    """Proactive-brief scheduler (FEATURE-PROACTIVE-BRIEF.md §7)."""
    from .notify.deliver import deliver as deliver_brief
    from .notify.scheduler import scheduler_loop

    def deliver_for_user(user, prefs, now):
        deliver_brief(
            _server._get_notify(), _server._get_acl(), config.load(), user, prefs,
            now=now, tools=_server._brief_tools_for_user(),
        )

    return scheduler_loop(_server._get_notify(), deliver_for_user)


def _calendar_sync_loop():
    """Calendar cache/sync loop (memaix-src card 4daa20e2)."""
    from .connectors.calendar_cache import calendar_sync_loop

    return calendar_sync_loop(_server._get_acl, _server._get_token_store)


def _consent_purge_loop():
    """Booking-consent purge loop (memaix-src card 01cf3b74)."""
    from .booking.purge import consent_purge_loop

    return consent_purge_loop(_server._get_acl, _server._resolve_calendar_dav)


def _reminders_loop():
    """Meeting-reminders loop (memaix-src card ecffcb5b)."""
    from .booking.links import get_link
    from .booking.reminders import reminder_loop

    return reminder_loop(_server._get_acl, get_link)


# (config-sektion, flagga, loop-fabrik) — ordningen avgör lindningsordningen.
_BACKGROUND_LOOPS = (
    ("brief", "enabled", _brief_loop),
    ("calendar_sync", "enabled", _calendar_sync_loop),
    ("booking", "purge_enabled", _consent_purge_loop),
    ("booking", "reminders_enabled", _reminders_loop),
)


def _wrap_lifespan_with_task(starlette_app, make_coro) -> None:
    """Starlette dropped add_event_handler(); wrap the router's lifespan
    context manager so the task starts/stops alongside FastMCP's own."""
    prior = starlette_app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(make_coro())
        try:
            async with prior(app) as state:
                yield state
        finally:
            task.cancel()

    starlette_app.router.lifespan_context = lifespan


def install_background_loops(starlette_app, cfg: dict) -> None:
    for section, flag, make_coro in _BACKGROUND_LOOPS:
        if _memaix_cfg(cfg).get(section, {}).get(flag, True):
            _wrap_lifespan_with_task(starlette_app, make_coro)


# ----------------------------------------------------------------------
# Middleware
# ----------------------------------------------------------------------


class _BookingCorsBypass:
    """/book/* handles CORS itself, scoped to jimlov.se (booking/routes.py
    _cors_headers), including its own OPTIONS preflight responses. The
    app-wide CORSMiddleware only knows claude.ai — left in place it would
    intercept the booking preflight and 400 it before ever reaching
    booking/routes.py, since Starlette's CORSMiddleware answers every
    Access-Control-Request-Method OPTIONS itself instead of delegating.
    Route /book/* around it entirely rather than widening the claude.ai
    allowlist, which would apply CORS to every other route too."""

    def __init__(self, plain_app, cors_app):
        self._plain_app = plain_app
        self._cors_app = cors_app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        is_booking = path.startswith(("/book/", "/booking/"))
        if scope["type"] == "http" and is_booking:
            await self._plain_app(scope, receive, send)
        else:
            await self._cors_app(scope, receive, send)


def wrap_middleware(starlette_app):
    # Browsers hitting the bare domain get the web UI, not the MCP 401 JSON.
    from .web.routes import BrowserRootRedirect

    starlette_app = BrowserRootRedirect(starlette_app)

    # Wrap with CORS so claude.ai browser requests aren't blocked.
    cors_wrapped = CORSMiddleware(
        app=starlette_app,
        allow_origins=["https://claude.ai", "https://api.claude.ai"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "mcp-session-id"],
        expose_headers=["mcp-session-id"],
    )
    return _BookingCorsBypass(starlette_app, cors_wrapped)


def assemble_http_app():
    """Build the Starlette app with Bearer-auth for HTTP transport."""
    cfg = config.load()
    configure_mcp_settings(cfg)

    mcp = _server.mcp
    mcp._custom_starlette_routes = custom_routes()
    starlette_app = mcp.streamable_http_app()

    # FastMCP appends custom_starlette_routes AFTER its built-in routes, so the
    # auto-generated /.well-known/oauth-protected-resource route wins.  Prepend
    # our handler into the Starlette router routes list so it matches first.
    starlette_app.router.routes.insert(
        0, Route("/.well-known/oauth-protected-resource", protected_resource_handler)
    )

    install_background_loops(starlette_app, cfg)
    return wrap_middleware(starlette_app)
