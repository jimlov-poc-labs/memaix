# SPDX-License-Identifier: AGPL-3.0-or-later
"""TOTP MFA for admin write operations (MEX-025 Fas D).

Design (per the reconciled FEATURE-WEB-UI-PHASE2.md):
- MFA session lives in a signed cookie (memaix_mfa = user:ts:sig, HMAC with
  HYDRA_SYSTEM_SECRET, TTL 8h) — no server-side session state.
- Enrollment carries the PENDING secret in a short-lived signed cookie
  (memaix_mfa_enroll, TTL 10 min) between setup-start and setup-confirm, so
  the secret survives between the two requests without server state.
- The confirmed secret is stored as a file: ref (file:<dir>/totp_<user>,
  mode 0600) — env: refs are only read at process start, a runtime-written
  secret must be live immediately. acl.yaml gets users.<uid>.totp_secret_ref.
- Verify attempts are rate-limited (5 per 10 min per user).
"""

from __future__ import annotations

import hmac as _hmac
import os
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...safety.rate_limit import rate_limiter
from .. import routes as w
from .. import totp as _totp

_MFA_COOKIE = "memaix_mfa"
_ENROLL_COOKIE = "memaix_mfa_enroll"
_MFA_TTL_S = 8 * 3600
_ENROLL_TTL_S = 10 * 60


def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _get_acl():
    return w._get_acl()


def _secret_key() -> bytes:
    from ...board.routes import _secret

    return _secret()


def _secrets_dir() -> Path:
    from ... import config

    return Path(os.environ.get("MEMAIX_SECRETS_DIR", str(config.CONFIG_DIR / "secrets")))


def _acl_writer():
    from ... import config
    from ..acl_writer import AclWriter

    return AclWriter(config.CONFIG_DIR / "acl.yaml")


def _sign(payload: str) -> str:
    return _hmac.new(_secret_key(), payload.encode(), "sha256").hexdigest()[:32]


def _make_signed_cookie(*parts: str) -> str:
    payload = ":".join(parts)
    return f"{payload}:{_sign(payload)}"


def _read_signed_cookie(request: Request, name: str, n_parts: int, ttl_s: int) -> list[str] | None:
    """Verify HMAC + TTL; parts[-1] must be the issue timestamp (unix)."""
    raw = request.cookies.get(name, "")
    pieces = raw.split(":")
    if len(pieces) != n_parts + 1:
        return None
    payload, sig = ":".join(pieces[:-1]), pieces[-1]
    if not _hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        issued = int(pieces[n_parts - 1])
    except ValueError:
        return None
    if time.time() - issued > ttl_s:
        return None
    return pieces[:-1]


def mfa_verified(request: Request, user: str) -> bool:
    """True if the request carries a valid, unexpired MFA cookie for *user*."""
    parts = _read_signed_cookie(request, _MFA_COOKIE, 2, _MFA_TTL_S)
    return bool(parts and parts[0] == user)


def _totp_ref(acl, user: str) -> str | None:
    return acl.users.get(user, {}).get("totp_secret_ref")


def _load_totp_secret(acl, user: str) -> str | None:
    ref = _totp_ref(acl, user)
    if not ref:
        return None
    from ... import config

    try:
        return config.secret(ref)
    except Exception:
        return None


def _rate_limited(user: str) -> bool:
    return not rate_limiter.check(f"mfa:{user}", limit=5, window_s=600)


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------


def api_mfa_status(request: Request) -> JSONResponse:
    """GET /app/api/admin/mfa → {enrolled, verified}"""
    user = _require_user(request)
    if not user:
        return w._json_401()
    acl = _get_acl()
    if not acl.is_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(
        {"enrolled": bool(_totp_ref(acl, user)), "verified": mfa_verified(request, user)}
    )


def api_mfa_setup_start(request: Request) -> JSONResponse:
    """POST /app/api/admin/mfa/setup/start → {otpauth_uri, secret}

    Generates a pending secret and carries it in a signed short-TTL cookie so
    the confirm step can verify against the SAME secret (no server state)."""
    user = _require_user(request)
    if not user:
        return w._json_401()
    acl = _get_acl()
    if not acl.is_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    # Re-enrollment without an existing MFA cookie would let someone with only a
    # password rotate their own TOTP secret and mint an 8h admin cookie — MFA
    # would never actually be a second factor. Require the current second factor
    # before replacing it. First-time enrollment (no secret yet) is exempt.
    if _totp_ref(acl, user) and not mfa_verified(request, user):
        return JSONResponse({"error": "mfa_required"}, status_code=403)

    secret = _totp.generate_secret()
    resp = JSONResponse(
        {"otpauth_uri": _totp.otpauth_uri(user, secret), "secret": secret}
    )
    resp.set_cookie(
        _ENROLL_COOKIE, _make_signed_cookie(user, secret, str(int(time.time()))),
        httponly=True, samesite="strict", secure=True, max_age=_ENROLL_TTL_S,
    )
    return resp


async def api_mfa_setup_confirm(request: Request) -> JSONResponse:
    """POST /app/api/admin/mfa/setup {code} — verify against the pending
    secret, persist it as a file: ref, update acl.yaml, reload the Acl."""
    user = _require_user(request)
    if not user:
        return w._json_401()
    acl = _get_acl()
    if not acl.is_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if _rate_limited(user):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    parts = _read_signed_cookie(request, _ENROLL_COOKIE, 3, _ENROLL_TTL_S)
    if not parts or parts[0] != user:
        return JSONResponse({"error": "no_pending_enrollment"}, status_code=400)
    pending_secret = parts[1]

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if not _totp.verify(pending_secret, str(body.get("code", "")), time.time()):
        return JSONResponse({"error": "wrong_code"}, status_code=401)

    # Persist: secret file (0600) + file: ref in acl.yaml + live reload.
    secrets_dir = _secrets_dir()
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secret_path = secrets_dir / f"totp_{user}"
    secret_path.write_text(pending_secret, encoding="utf-8")
    os.chmod(secret_path, 0o600)
    _acl_writer().set_user_field(user, "totp_secret_ref", f"file:{secret_path}")
    from ...server import reload_acl

    reload_acl()

    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_ENROLL_COOKIE)
    return resp


async def api_mfa_verify(request: Request) -> JSONResponse:
    """POST /app/api/admin/mfa/verify {code} — set the 8h MFA cookie."""
    user = _require_user(request)
    if not user:
        return w._json_401()
    acl = _get_acl()
    if not acl.is_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if _rate_limited(user):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    secret = _load_totp_secret(acl, user)
    if not secret:
        return JSONResponse({"error": "not_enrolled"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if not _totp.verify(secret, str(body.get("code", "")), time.time()):
        return JSONResponse({"error": "wrong_code"}, status_code=401)

    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        _MFA_COOKIE, _make_signed_cookie(user, str(int(time.time()))),
        httponly=True, samesite="strict", secure=True, max_age=_MFA_TTL_S,
    )
    return resp
