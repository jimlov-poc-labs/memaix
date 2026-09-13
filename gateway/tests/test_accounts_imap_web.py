# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web tests for the IMAP per-user link form
(POST /app/api/accounts/link-imap) — FEATURE-CONNECTOR-FRAMEWORK.md, the
non-OAuth credential-link path for a per-user IMAP mailbox.

Same rig pattern as test_web_mvp_api.py's accounts_rig (auth bypassed via
web_routes_mod._require_user, token store faked). The load-bearing
assertions here are the "password never leaks" ones AGENTS.md §2/§3
requires explicitly for this feature."""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.web import routes as web_routes_mod


class _FakeTokenStore:
    def __init__(self):
        self.records = {}

    def list_accounts(self, user):
        return [
            {"provider": p, "account": a, "status": "active", "scopes": []}
            for (u, p, a) in self.records
            if u == user
        ]

    def store(self, user, provider, account, data):
        self.records[(user, provider, account)] = data

    def delete(self, user, provider, account):
        return self.records.pop((user, provider, account), None) is not None

    def load_one(self, user, provider, account):
        return self.records.get((user, provider, account))


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    acl = Acl(
        users={
            "alice": {"grants": {"proj": "owner"}},
            "bob": {"grants": {"proj": "reader"}},
        },
        projects={"proj": {"vault": str(vault)}},
    )
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    current = {"user": "alice"}
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: current["user"])

    store = _FakeTokenStore()
    from memaix_gateway.web.api import accounts as api_accounts_mod
    monkeypatch.setattr(api_accounts_mod, "_token_store", lambda: store)
    monkeypatch.setattr(api_accounts_mod, "_public_url", lambda: "https://mcp.example.com")

    app = Starlette(routes=web_routes_mod.web_routes)
    return TestClient(app), store, current


SECRET_PASSWORD = "s3cret-super-secret-value"


def _link_body(**overrides):
    body = {
        "account_email": "alice@personal.example.com",
        "host": "imap.personal.example.com",
        "user": "alice",
        "password": SECRET_PASSWORD,
    }
    body.update(overrides)
    return body


def test_link_imap_requires_auth(rig, monkeypatch):
    client, _, _ = rig
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    from memaix_gateway.web.api import accounts as api_accounts_mod
    monkeypatch.setattr(api_accounts_mod, "_require_user", lambda request: None)
    resp = client.post("/app/api/accounts/link-imap", json=_link_body())
    assert resp.status_code == 401


def test_link_imap_stores_credential_and_returns_no_password(rig):
    client, store, _ = rig
    resp = client.post("/app/api/accounts/link-imap", json=_link_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"ok": True, "provider": "imap", "account": "alice@personal.example.com"}
    assert SECRET_PASSWORD not in str(data)

    stored = store.load_one("alice", "imap", "alice@personal.example.com")
    assert stored["password"] == SECRET_PASSWORD  # stored server-side, encrypted at rest by the real TokenStore
    assert stored["host"] == "imap.personal.example.com"


def test_link_imap_with_port(rig):
    client, store, _ = rig
    resp = client.post("/app/api/accounts/link-imap", json=_link_body(port=1993))
    assert resp.status_code == 200
    stored = store.load_one("alice", "imap", "alice@personal.example.com")
    assert stored["port"] == 1993


def test_link_imap_missing_field_is_400_and_does_not_store(rig):
    client, store, _ = rig
    resp = client.post("/app/api/accounts/link-imap", json=_link_body(password=""))
    assert resp.status_code == 400
    assert store.records == {}


def test_link_imap_bad_json_is_400(rig):
    client, _, _ = rig
    resp = client.post(
        "/app/api/accounts/link-imap", content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_link_imap_error_response_never_contains_password(rig):
    client, _, _ = rig
    resp = client.post("/app/api/accounts/link-imap", json=_link_body(host=""))
    assert resp.status_code == 400
    assert SECRET_PASSWORD not in resp.text


def test_linked_imap_account_then_appears_in_accounts_list(rig):
    client, _, _ = rig
    client.post("/app/api/accounts/link-imap", json=_link_body())
    accounts = client.get("/app/api/accounts").json()
    imap_entries = [a for a in accounts if a["provider"] == "imap"]
    assert len(imap_entries) == 1
    assert imap_entries[0]["account"] == "alice@personal.example.com"
    assert not imap_entries[0].get("readonly")  # unlike project-shared IMAP, this one is unlinkable
    assert SECRET_PASSWORD not in str(accounts)


def test_linked_imap_account_can_be_unlinked(rig):
    client, store, _ = rig
    client.post("/app/api/accounts/link-imap", json=_link_body())
    resp = client.delete("/app/api/accounts/imap?account=alice@personal.example.com")
    assert resp.status_code == 200
    assert store.load_one("alice", "imap", "alice@personal.example.com") is None


def test_account_link_url_for_imap_points_to_settings_form(rig):
    client, _, _ = rig
    resp = client.get("/app/api/accounts/link/imap")
    assert resp.status_code == 200
    assert "/app/settings" in resp.json()["url"]


def test_password_never_appears_in_server_logs(rig, caplog):
    client, _, _ = rig
    with caplog.at_level(logging.DEBUG):
        client.post("/app/api/accounts/link-imap", json=_link_body())
    for record in caplog.records:
        assert SECRET_PASSWORD not in record.getMessage()
