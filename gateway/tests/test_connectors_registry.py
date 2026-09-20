# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for connectors.registry — FEATURE-CONNECTOR-FRAMEWORK.md §5."""

from __future__ import annotations

import pytest

from memaix_gateway.acl import Acl
from memaix_gateway.connectors.registry import (
    ConnectorAuthRequired,
    ConnectorRegistry,
    ConnectorSpec,
)


class _FakeTokenStore:
    def __init__(self, accounts: dict[str, list[dict]] | None = None, tokens: dict | None = None):
        self._accounts = accounts or {}
        self._tokens = tokens or {}

    def list_accounts(self, user: str) -> list[dict]:
        return self._accounts.get(user, [])

    def load_one(self, user: str, provider: str, account: str):
        return self._tokens.get((user, provider, account))

    def is_allowed(self, user, provider, account, capability, project) -> bool:
        return True  # project scoping is covered in test_account_scopes.py


@pytest.fixture()
def acl():
    return Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={
            "acme": {
                "vault": "/srv/vaults/acme",
                "mailbox": {"host": "imap.example.com", "user": "acme@example.com"},
                "calendar": {"type": "google", "auth": "per_user"},
            },
            "bare": {"vault": "/srv/vaults/bare"},
        },
    )


def test_default_type_used_when_resource_has_none(acl):
    registry = ConnectorRegistry()
    built = []
    registry.register(
        ConnectorSpec(
            type="imap", capability="mail", auth="shared",
            factory=lambda acl, project, user, resource_cfg, token: built.append(resource_cfg) or "adapter",
        )
    )
    result = registry.get(acl, _FakeTokenStore(), "acme", "mail", "alice")
    assert result == "adapter"
    assert built[0]["host"] == "imap.example.com"


def test_unknown_type_raises_value_error(acl):
    registry = ConnectorRegistry()
    with pytest.raises(ValueError, match="unknown connector type"):
        registry.get(acl, _FakeTokenStore(), "acme", "mail", "alice")


def test_missing_resource_raises_value_error(acl):
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="imap", capability="mail", auth="shared", factory=lambda *a: "x")
    )
    with pytest.raises(ValueError, match="no mail configured"):
        registry.get(acl, _FakeTokenStore(), "bare", "mail", "alice")


def test_per_user_without_linked_account_raises_auth_required(acl):
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="google", capability="calendar", auth="per_user", factory=lambda *a: "x")
    )
    with pytest.raises(ConnectorAuthRequired) as exc_info:
        registry.get(acl, _FakeTokenStore(), "acme", "calendar", "alice")
    assert exc_info.value.capability == "calendar"
    assert exc_info.value.type == "google"


def test_per_user_with_linked_account_passes_resolved_token(acl):
    registry = ConnectorRegistry()
    seen_tokens = []
    registry.register(
        ConnectorSpec(
            type="google", capability="calendar", auth="per_user",
            factory=lambda acl, project, user, resource_cfg, token: seen_tokens.append(token) or "adapter",
        )
    )
    store = _FakeTokenStore(
        accounts={"alice": [{"provider": "google", "account": "alice@gmail.com"}]},
        tokens={("alice", "google", "alice@gmail.com"): {"access_token": "tok123"}},
    )
    result = registry.get(acl, store, "acme", "calendar", "alice")
    assert result == "adapter"
    assert seen_tokens[0] == {"access_token": "tok123"}


def test_per_user_spec_can_override_provider_name(acl):
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="google", capability="calendar", auth="per_user", provider="google_workspace",
            factory=lambda acl, project, user, resource_cfg, token: token,
        )
    )
    store = _FakeTokenStore(
        accounts={"alice": [{"provider": "google_workspace", "account": "a@x.com"}]},
        tokens={("alice", "google_workspace", "a@x.com"): {"ok": True}},
    )
    result = registry.get(acl, store, "acme", "calendar", "alice")
    assert result == {"ok": True}


def test_register_is_idempotent_by_capability_and_type(acl):
    registry = ConnectorRegistry()
    registry.register(ConnectorSpec(type="imap", capability="mail", auth="shared", factory=lambda *a: "first"))
    registry.register(ConnectorSpec(type="imap", capability="mail", auth="shared", factory=lambda *a: "second"))
    result = registry.get(acl, _FakeTokenStore(), "acme", "mail", "alice")
    assert result == "second"  # last registration for the same (capability, type) wins
