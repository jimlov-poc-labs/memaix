# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal Hydra login/consent-app för Memaix.

Hanterar:
  GET  /login    — visa inloggningsformulär
  POST /login    — verifiera lösenord, godkänn eller neka hos Hydra
  GET  /consent  — auto-godkänn (single-user, trusted client)

Säkra standarder:
  - Lösenord verifieras med PBKDF2-HMAC-SHA256 (200 000 iterationer)
  - Hydra challenge valideras innan formuläret visas
  - Inget session state lagras i appen — Hydra håller session
"""

from __future__ import annotations

import os
import sys

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import approved_clients
import auth

sys.path.insert(0, "/app/i18n_pkg")
try:
    from memaix_gateway.i18n import get_translator, locale_from_request as _locale_from_req
    _I18N_AVAILABLE = True
except ImportError:
    _I18N_AVAILABLE = False


def _t_for_request(request: Request):
    if not _I18N_AVAILABLE:
        return lambda k: k, "en"
    locale = _locale_from_req(
        request.headers.get("Accept-Language"),
        os.environ.get("MEMAIX_LOCALE", "en"),
    )
    return get_translator(locale), locale

HYDRA_ADMIN = os.environ.get("HYDRA_ADMIN_URL", "http://hydra:4445")
ALLOWED_USERS: set[str] = set(
    os.environ.get("MEMAIX_ALLOWED_USERS", "alice").split(",")
)
# Shared fallback hash (single-user backwards-compat). Format: salt_hex:pbkdf2_hex
_SHARED_HASH = os.environ.get("MEMAIX_LOGIN_PASSWORD_HASH", "")

# Per-user hashes from acl.yaml (users.<id>.password_hash). Loaded at startup.
_ACL_PATH = os.environ.get("MEMAIX_ACL_CONFIG", "/app/config/acl.yaml")
_PER_USER_HASHES: dict[str, str] = auth.load_per_user_hashes(_ACL_PATH)
_LOGIN_TEMPLATE = "login.html"

app = FastAPI(title="Memaix login")
templates = Jinja2Templates(directory="/app/templates")


def _verify_password(user: str, provided: str) -> bool:
    """Verify a login password. Security logic lives in auth.py (unit-tested);
    this wires it to the app's runtime config (allowed users, shared/per-user
    hashes)."""
    return auth.verify_password(
        user,
        provided,
        allowed_users=ALLOWED_USERS,
        per_user_hashes=_PER_USER_HASHES,
        shared_hash=_SHARED_HASH,
    )


def _hydra_get(path: str, challenge_name: str, challenge: str) -> dict:
    r = requests.get(
        f"{HYDRA_ADMIN}{path}",
        params={challenge_name: challenge},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


def _hydra_accept(path: str, challenge_name: str, challenge: str, body: dict) -> str:
    r = requests.put(
        f"{HYDRA_ADMIN}{path}",
        params={challenge_name: challenge},
        json=body,
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["redirect_to"]


def _hydra_reject(path: str, challenge_name: str, challenge: str, reason: str) -> str:
    r = requests.put(
        f"{HYDRA_ADMIN}{path}",
        params={challenge_name: challenge},
        json={"error": "access_denied", "error_description": reason},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["redirect_to"]


# ------------------------------------------------------------------
# Login
# ------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, login_challenge: str = ""):
    if not login_challenge:
        return HTMLResponse("<p>Ogiltig förfrågan (login_challenge saknas).</p>", status_code=400)
    try:
        info = _hydra_get("/admin/oauth2/auth/requests/login", "login_challenge", login_challenge)
    except Exception as exc:
        return HTMLResponse(f"<p>Hydra-fel: {exc}</p>", status_code=502)

    # Hydra's "skip" is scoped to the BROWSER, not to (identity, client) — a
    # second client logging in as someone else in the same browser would
    # otherwise silently inherit whichever identity Hydra remembers. Only
    # honour skip when this exact client has previously completed the
    # password form for this exact subject (memaix-src 4c8f32fe).
    client_id = (info.get("client") or {}).get("client_id", "")
    if info.get("skip") and approved_clients.is_approved(info["subject"], client_id):
        redirect = _hydra_accept(
            "/admin/oauth2/auth/requests/login/accept",
            "login_challenge", login_challenge,
            {"subject": info["subject"]},
        )
        return RedirectResponse(redirect, status_code=303)

    t, locale = _t_for_request(request)
    return templates.TemplateResponse(
        request, _LOGIN_TEMPLATE,
        {"challenge": login_challenge, "error": "", "t": t, "locale": locale},
    )


@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    login_challenge: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    t, locale = _t_for_request(request)
    if not auth.login_rate_limiter.check(username, limit=5, window_s=600):
        return templates.TemplateResponse(
            request, _LOGIN_TEMPLATE,
            {"challenge": login_challenge, "error": t("login_error_credentials"), "t": t, "locale": locale},
            status_code=429,
        )
    if username not in ALLOWED_USERS or not _verify_password(username, password):
        return templates.TemplateResponse(
            request, _LOGIN_TEMPLATE,
            {"challenge": login_challenge, "error": t("login_error_credentials"), "t": t, "locale": locale},
            status_code=401,
        )

    try:
        info = _hydra_get("/admin/oauth2/auth/requests/login", "login_challenge", login_challenge)
        client_id = (info.get("client") or {}).get("client_id", "")
        approved_clients.approve(username, client_id)
        redirect = _hydra_accept(
            "/admin/oauth2/auth/requests/login/accept",
            "login_challenge", login_challenge,
            {
                "subject": username,
                # Real remember-me again (30 days), safe now that skip is
                # gated on approved_clients (identity + client_id), not just
                # Hydra's browser-scoped session — see login_get above and
                # memaix-src 4c8f32fe. A prior stopgap set remember_for=0 to
                # kill the browser-scoped leak, but that meant Hydra never
                # created a session row, so its own cleanup call
                # (DeleteLoginSession) 404'd on every fresh login. remember=
                # True + a real remember_for avoids that crash entirely.
                "remember": True,
                "remember_for": 86400 * 30,
            },
        )
    except Exception as exc:
        return HTMLResponse(f"<p>Hydra-fel: {exc}</p>", status_code=502)

    return RedirectResponse(redirect, status_code=303)


# ------------------------------------------------------------------
# Consent — auto-godkänn för ägare (single-user setup)
# ------------------------------------------------------------------


@app.get("/consent")
async def consent_get(consent_challenge: str = ""):
    if not consent_challenge:
        return HTMLResponse("<p>Ogiltig förfrågan (consent_challenge saknas).</p>", status_code=400)

    try:
        info = _hydra_get(
            "/admin/oauth2/auth/requests/consent",
            "consent_challenge", consent_challenge,
        )
    except Exception as exc:
        return HTMLResponse(f"<p>Hydra-fel: {exc}</p>", status_code=502)

    subject = info.get("subject", "")
    if subject not in ALLOWED_USERS:
        redirect = _hydra_reject(
            "/admin/oauth2/auth/requests/consent",
            "consent_challenge", consent_challenge,
            "user not in allowed list",
        )
        return RedirectResponse(redirect, status_code=303)

    requested_scope = info.get("requested_scope", [])

    # Hydra v2.2 doesn't map the `resource` param to requested_access_token_audience
    # automatically, so we parse it from request_url and grant it explicitly.
    # Include both trailing-slash variants so the JWT aud matches regardless of
    # whether claude.ai compares against the connector URL (no slash) or the
    # RFC 8707 resource indicator it sends (with slash).
    from urllib.parse import parse_qs, urlparse as _urlparse
    _rurl = info.get("request_url", "")
    _resource = parse_qs(_urlparse(_rurl).query).get("resource", [])
    _resource_both = []
    for r in _resource:
        _resource_both.append(r)
        _resource_both.append(r.rstrip("/") if r.endswith("/") else r + "/")
    audience = list(set(_resource_both) | set(info.get("requested_access_token_audience") or []))

    redirect = _hydra_accept(
        "/admin/oauth2/auth/requests/consent/accept",
        "consent_challenge", consent_challenge,
        {
            "grant_scope": requested_scope,
            "grant_access_token_audience": audience,
            "remember": True,
            "remember_for": 86400 * 30,
            "session": {
                "id_token": {
                    "email": f"{info.get('subject', 'alice')}@personal.example.com",
                    "name": info.get("subject", "alice"),
                }
            },
        },
    )
    return RedirectResponse(redirect, status_code=303)


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "service": "memaix-login"}
