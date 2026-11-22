# SPDX-License-Identifier: AGPL-3.0-or-later
"""Microsoft OAuth-URL:er måste använda provider_cfg['tenant_id'] konsekvent
på alla tre ställen den förekommer (authorize, token-exchange, refresh) —
annars blir ett token utfärdat mot en tenant-endpoint ogiltigt vid refresh
mot /common (eller tvärtom). Se startkort/uppdrag 2026-11-22.

Tre saker testas:
  (a) satt tenant_id -> samma tenant används i alla tre URL:er
  (b) saknad tenant_id -> alla tre faller tillbaka till "common" identiskt
  (c) ett token mintat mot en tenant-specifik authorize-URL kan refreshas
      mot exakt samma tenant-URL (end-to-end, inte bara URL-sträng-jämförelse)
"""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from memaix_gateway import config as config_mod
from memaix_gateway import server as server_mod
from memaix_gateway.backends.token_store import TokenStore

TENANT = "11111111-2222-3333-4444-555555555555"


# ----------------------------------------------------------------------
# (a) + (b): helperna själva
# ----------------------------------------------------------------------


def test_microsoft_tenant_uses_configured_tenant_id():
    assert server_mod._microsoft_tenant({"tenant_id": TENANT}) == TENANT


def test_microsoft_tenant_falls_back_to_common_when_missing():
    assert server_mod._microsoft_tenant({}) == "common"


def test_microsoft_tenant_falls_back_to_common_when_empty_string():
    # En instans som bara fyllt i client_id/secret (gammal installation,
    # eller en medveten multi-tenant-app) ska inte få en tom sträng i URL:en.
    assert server_mod._microsoft_tenant({"tenant_id": ""}) == "common"


@pytest.mark.parametrize("provider_cfg", [{"tenant_id": TENANT}, {}])
def test_authorize_and_token_url_share_the_same_tenant_segment(provider_cfg):
    authorize = server_mod._microsoft_authorize_url(provider_cfg)
    token = server_mod._microsoft_token_url(provider_cfg)
    expected_tenant = provider_cfg.get("tenant_id") or "common"
    assert authorize == f"https://login.microsoftonline.com/{expected_tenant}/oauth2/v2.0/authorize"
    assert token == f"https://login.microsoftonline.com/{expected_tenant}/oauth2/v2.0/token"


# ----------------------------------------------------------------------
# (a) + (b) via HTTP: link_start (authorize) och link_callback (token exchange)
# ----------------------------------------------------------------------


@pytest.fixture()
def http_cfg(monkeypatch, tmp_path):
    data: dict = {"brand": {}, "memaix": {}, "acl": {}}
    monkeypatch.setattr(config_mod, "load", lambda: data)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server_mod.mcp, "_session_manager", None)
    return data["memaix"]


def _client():
    return TestClient(server_mod.build_http_app())


def test_link_start_microsoft_uses_configured_tenant(http_cfg):
    http_cfg["oauth_providers"] = {"microsoft": {"client_id": "cid", "tenant_id": TENANT}}
    r = _client().get("/link/microsoft", follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?")


def test_link_start_microsoft_without_tenant_id_falls_back_to_common(http_cfg):
    http_cfg["oauth_providers"] = {"microsoft": {"client_id": "cid"}}
    r = _client().get("/link/microsoft", follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")


def _fake_async_client(monkeypatch, responder):
    calls: list = []

    class _FakeResp:
        def __init__(self, status, data):
            self.status_code = status
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kw):
            calls.append((url, data))
            return responder(url, data)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return calls


def test_link_callback_microsoft_token_exchange_uses_configured_tenant(http_cfg, monkeypatch):
    from memaix_gateway.tools import account

    monkeypatch.setattr(
        account, "validate_state",
        lambda s: {"user_id": "u1", "provider": "microsoft"} if s == "good" else None,
    )
    store_calls: list = []
    monkeypatch.setattr(server_mod, "_get_token_store", lambda: type(
        "S", (), {
            "load_one": lambda self, *a: None,
            "store": lambda self, *a: store_calls.append(a),
        },
    )())
    monkeypatch.setattr(config_mod, "secret", lambda ref: "sekret")
    http_cfg["oauth_providers"] = {
        "microsoft": {"client_id": "cid", "client_secret_ref": "env:X", "tenant_id": TENANT},
    }
    http_cfg["server"] = {"public_url": "https://pub.example/"}

    calls = _fake_async_client(monkeypatch, lambda url, data: type(
        "R", (), {"status_code": 200, "raise_for_status": lambda self: None,
                   "json": lambda self: {"access_token": "tok", "expires_in": 100}},
    )())

    r = _client().get("/link/microsoft/callback?code=thecode&state=good")
    assert r.status_code == 200
    assert calls[0][0] == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"


def test_link_callback_microsoft_token_exchange_falls_back_to_common(http_cfg, monkeypatch):
    from memaix_gateway.tools import account

    monkeypatch.setattr(
        account, "validate_state",
        lambda s: {"user_id": "u1", "provider": "microsoft"} if s == "good" else None,
    )
    monkeypatch.setattr(server_mod, "_get_token_store", lambda: type(
        "S", (), {
            "load_one": lambda self, *a: None,
            "store": lambda self, *a: None,
        },
    )())
    monkeypatch.setattr(config_mod, "secret", lambda ref: "sekret")
    http_cfg["oauth_providers"] = {"microsoft": {"client_id": "cid", "client_secret_ref": "env:X"}}
    http_cfg["server"] = {"public_url": "https://pub.example/"}

    calls = _fake_async_client(monkeypatch, lambda url, data: type(
        "R", (), {"status_code": 200, "raise_for_status": lambda self: None,
                   "json": lambda self: {"access_token": "tok", "expires_in": 100}},
    )())

    r = _client().get("/link/microsoft/callback?code=thecode&state=good")
    assert r.status_code == 200
    assert calls[0][0] == "https://login.microsoftonline.com/common/oauth2/v2.0/token"


# ----------------------------------------------------------------------
# (c) end-to-end: refresh måste träffa SAMMA tenant-URL som authorize gjorde
# ----------------------------------------------------------------------


@pytest.fixture()
def token_store(tmp_path):
    return TokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key())


def test_refresh_hits_the_same_tenant_url_authorize_was_minted_against(
    http_cfg, token_store, monkeypatch,
):
    """Simulerar hela resan: /link/microsoft byggs mot tenant-URL X, token
    sparas, och en efterföljande refresh (_refresh_microsoft_token) måste
    POSTa mot exakt samma tenant-URL X — inte /common, inte en annan tenant."""
    http_cfg["oauth_providers"] = {
        "microsoft": {"client_id": "cid", "client_secret_ref": "env:X", "tenant_id": TENANT},
    }
    monkeypatch.setattr(config_mod, "secret", lambda ref: "sekret")

    # Steg 1: authorize-URL som användaren faktiskt skickades till.
    authorize_url = server_mod._microsoft_authorize_url(http_cfg["oauth_providers"]["microsoft"])
    assert authorize_url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"

    # Steg 2: token "utfärdat" mot den tenanten, sparat i store.
    token_store.store("alice", "microsoft", "alice@work.com", {
        "access_token": "stale", "refresh_token": "r1", "expires_at": time.time() - 10,
    })

    # Steg 3: refresh måste POSTa mot samma tenant-URL som authorize gav.
    posted_urls: list = []

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "fresh", "expires_in": 3600}

    def _fake_post(url, *a, **kw):
        posted_urls.append(url)
        return _FakeResp()

    monkeypatch.setattr("requests.post", _fake_post)

    cfg = config_mod.load()
    result = server_mod._refresh_microsoft_token(
        cfg, token_store, "alice", "alice@work.com",
        token_store.load_one("alice", "microsoft", "alice@work.com"),
    )

    assert result == "fresh"
    assert posted_urls == [authorize_url.replace("/authorize", "/token")]
    assert posted_urls[0] == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
