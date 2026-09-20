# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-level tests for the Microsoft Graph mail connector wiring —
per_user token refresh (server._ensure_fresh_microsoft_mail_token /
_refresh_microsoft_token) and end-to-end routing through the registry
(FEATURE-CONNECTOR-FRAMEWORK.md §7 step 6)."""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.notify.store import NotifyStore
from memaix_gateway.rules.store import RulesStore
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.timeline.store import ActionsStore
import memaix_gateway.connectors.registry as registry_mod
from memaix_gateway.connectors.registry import ConnectorRegistry, ConnectorSpec
from memaix_gateway.connectors.adapters.mail_microsoft import GraphMailAdapter


class _AutoScopedTokenStore(TokenStore):
    """TokenStore whose store() also shares the account with every project.

    This suite is about Graph routing, not access control; granting '*' on
    link preserves its pre-scoping meaning of "linked AND usable here".
    The gate itself is covered in test_account_scopes.py.
    """

    def store(self, user, provider, account, token_data):
        super().store(user, provider, account, token_data)
        for capability in ("mail", "calendar"):
            self.set_scopes(user, provider, account, capability, ["*"])


@pytest.fixture()
def token_store(tmp_path):
    return _AutoScopedTokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key())


@pytest.fixture()
def wired(tmp_path, monkeypatch, token_store):
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True)
    acl = Acl(
        users={"alice": {"grants": {"proj": "owner"}}},
        projects={"proj": {"vault": str(vault), "mailbox": {"type": "microsoft"}}},
    )
    AuditLog._clear_instances()
    monkeypatch.setattr(server, "_acl", acl)
    monkeypatch.setattr(server, "_audit", AuditLog.for_path(tmp_path / "audit.db"))
    monkeypatch.setattr(server, "_outbox_queue", ActionQueue.for_path(tmp_path / "outbox.db"))
    monkeypatch.setattr(server, "_timeline_store", ActionsStore.for_path(tmp_path / "actions.db"))
    monkeypatch.setattr(server, "_search_store", EmbeddingStore.for_path(tmp_path / "index.db"))
    monkeypatch.setattr(server, "_search_embedder", None)
    monkeypatch.setattr(server, "_search_embedder_loaded", True)
    monkeypatch.setattr(server, "_notify_store", NotifyStore.for_path(tmp_path / "notify.db"))
    monkeypatch.setattr(server, "_rules_store", RulesStore.for_path(tmp_path / "rules.db"))
    monkeypatch.setattr(server, "_token_store", token_store)
    monkeypatch.setenv("MEMAIX_USER", "alice")
    server._rate_limiter._windows.clear()
    return acl, token_store


# ------------------------------------------------------------------
# _ensure_fresh_microsoft_mail_token / _refresh_microsoft_token
# ------------------------------------------------------------------


def test_no_linked_account_is_a_noop(wired):
    server._ensure_fresh_microsoft_mail_token("alice")  # must not raise


def test_fresh_token_is_not_refreshed(wired, monkeypatch):
    _, token_store = wired
    token_store.store("alice", "microsoft", "alice@work.com", {
        "access_token": "still-good", "refresh_token": "r1",
        "expires_at": time.time() + 3600,
    })
    called = []
    monkeypatch.setattr("requests.post", lambda *a, **kw: called.append(1))

    server._ensure_fresh_microsoft_mail_token("alice")

    assert called == []
    assert token_store.load_one("alice", "microsoft", "alice@work.com")["access_token"] == "still-good"


def test_expiring_token_is_refreshed_and_restored(wired, monkeypatch):
    _, token_store = wired
    token_store.store("alice", "microsoft", "alice@work.com", {
        "access_token": "stale", "refresh_token": "r1",
        "expires_at": time.time() - 10,
    })

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "fresh-token", "expires_in": 3600}

    monkeypatch.setattr("requests.post", lambda *a, **kw: _FakeResp())

    server._ensure_fresh_microsoft_mail_token("alice")

    updated = token_store.load_one("alice", "microsoft", "alice@work.com")
    assert updated["access_token"] == "fresh-token"
    assert updated["refresh_token"] == "r1"  # preserved, Microsoft may not reissue it


def test_expired_token_without_refresh_token_marks_needs_relink(wired, monkeypatch):
    _, token_store = wired
    token_store.store("alice", "microsoft", "alice@work.com", {
        "access_token": "stale", "expires_at": time.time() - 10,
    })
    called = []
    monkeypatch.setattr("requests.post", lambda *a, **kw: called.append(1))

    server._ensure_fresh_microsoft_mail_token("alice")

    assert called == []  # no refresh_token to try with
    accounts = token_store.list_accounts("alice")
    assert accounts[0]["status"] == "needs_relink"


def test_refresh_failure_marks_needs_relink(wired, monkeypatch):
    _, token_store = wired
    token_store.store("alice", "microsoft", "alice@work.com", {
        "access_token": "stale", "refresh_token": "r1", "expires_at": time.time() - 10,
    })

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.post", _boom)

    server._ensure_fresh_microsoft_mail_token("alice")

    accounts = token_store.list_accounts("alice")
    assert accounts[0]["status"] == "needs_relink"


# ------------------------------------------------------------------
# End-to-end: email_list routes through the per_user microsoft connector
# ------------------------------------------------------------------


def test_email_list_routes_to_microsoft_graph_adapter(wired, monkeypatch):
    _, token_store = wired
    token_store.store("alice", "microsoft", "alice@work.com", {
        "access_token": "good-token", "expires_at": time.time() + 3600,
    })

    class _FakeHttp:
        def request(self, method, url, **kwargs):
            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"value": [{
                        "id": "m1", "subject": "From Graph", "isRead": True,
                        "from": {"emailAddress": {"address": "x@work.com"}},
                        "toRecipients": [], "ccRecipients": [],
                        "receivedDateTime": "2025-01-06T00:00:00Z",
                        "body": {"contentType": "text", "content": "hi"},
                    }]}
            return _Resp()

    fake_registry = ConnectorRegistry()
    fake_registry.register(
        ConnectorSpec(
            type="microsoft", capability="mail", auth="per_user",
            factory=lambda acl, project, user, resource_cfg, token: GraphMailAdapter(
                token["access_token"], _http=_FakeHttp(),
            ),
        )
    )
    monkeypatch.setattr(registry_mod, "_registry", fake_registry)

    result = server.email_list("proj")
    assert result[0]["subject"] == "From Graph"


def test_email_list_no_linked_microsoft_account_raises_auth_required(wired):
    from memaix_gateway.connectors.registry import ConnectorAuthRequired

    with pytest.raises(ConnectorAuthRequired):
        server.email_list("proj")
