# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal TOTP (RFC 6238, SHA-1/6 digits/30s) — stdlib only.

Deliberately not pyotp: the algorithm is ~20 lines, and every new dependency
is supply-chain surface (SECURITY.md). Compatible with Google Authenticator,
Aegis, 1Password etc. via the standard otpauth:// URI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

_PERIOD = 30
_DIGITS = 6


def generate_secret() -> str:
    """Return a new random base32 secret (160 bits, RFC 4226 recommendation)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(key: bytes, counter: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()  # noqa: S324 -- RFC 6238 mandates SHA-1  # nosec B324  # NOSONAR
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** _DIGITS)).zfill(_DIGITS)


def _decode_secret(secret: str) -> bytes:
    padded = secret.strip().upper().replace(" ", "")
    padded += "=" * (-len(padded) % 8)
    return base64.b32decode(padded)


def totp_at(secret: str, unix_time: float) -> str:
    """The 6-digit code for *secret* at *unix_time*."""
    return _hotp(_decode_secret(secret), int(unix_time) // _PERIOD)


def verify(secret: str, code: str, unix_time: float, window: int = 1) -> bool:
    """Constant-time verify of *code* against ±window periods around now."""
    if not code or not code.strip().isdigit():
        return False
    provided = code.strip()
    counter = int(unix_time) // _PERIOD
    key = _decode_secret(secret)
    ok = False
    for delta in range(-window, window + 1):
        # Check every candidate (no early exit) to keep timing flat.
        if hmac.compare_digest(_hotp(key, counter + delta), provided):
            ok = True
    return ok


def otpauth_uri(user: str, secret: str, issuer: str = "Memaix") -> str:
    """otpauth:// URI for authenticator apps (paste or render as QR)."""
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(user)}"
        f"?secret={secret}&issuer={quote(issuer)}&digits={_DIGITS}&period={_PERIOD}"
    )
