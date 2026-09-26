# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dynamisk registrering får bara ge klienter för authorization_code-flödet.

Hydra v2.2 kan inte själv begränsa grant types vid DCR och saknar initial
access token, så spärren sitter i gatewayn."""

from __future__ import annotations

from memaix_gateway.server import _dcr_avvisning

# Så registrerar sig claude.ai och Claude Code — avläst från admin/clients
# på qronkclawd 2026-09-26. Får aldrig nekas.
CLAUDE_AI = {
    "client_name": "Claude",
    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "client_secret_post",
}
CLAUDE_CODE = {
    "client_name": "Claude Code (memaix)",
    "redirect_uris": ["http://localhost:3118/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


def test_claude_ai_och_claude_code_slapps_igenom():
    assert _dcr_avvisning(CLAUDE_AI) is None
    assert _dcr_avvisning(CLAUDE_CODE) is None


def test_utelamnade_listor_slapps_igenom():
    """Hydra sätter själv authorization_code/code när de saknas."""
    assert _dcr_avvisning({"redirect_uris": ["https://claude.ai/cb"]}) is None
    assert _dcr_avvisning({}) is None


def test_client_credentials_nekas():
    skal = _dcr_avvisning({**CLAUDE_AI, "grant_types": ["client_credentials"]})
    assert skal and "client_credentials" in skal


def test_client_credentials_nekas_aven_bredvid_tillatna():
    skal = _dcr_avvisning({**CLAUDE_AI, "grant_types": ["authorization_code", "client_credentials"]})
    assert skal and "client_credentials" in skal


def test_ovriga_grants_nekas():
    for grant in ("implicit", "password", "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "urn:ietf:params:oauth:grant-type:device_code"):
        assert _dcr_avvisning({"grant_types": [grant]}), grant


def test_implicit_response_types_nekas():
    assert _dcr_avvisning({"response_types": ["token"]})
    assert _dcr_avvisning({"response_types": ["code", "id_token"]})


def test_felaktiga_typer_nekas():
    assert _dcr_avvisning({"grant_types": "client_credentials"})
    assert _dcr_avvisning({"grant_types": [None]})
    assert _dcr_avvisning(["client_credentials"])
    assert _dcr_avvisning(None)
