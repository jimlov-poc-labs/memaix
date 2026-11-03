# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared TLS context for outbound SMTP: certificate and hostname verified."""

from __future__ import annotations

import ssl


def verified_context() -> ssl.SSLContext:
    """Default context with verification stated explicitly, never weakened."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
