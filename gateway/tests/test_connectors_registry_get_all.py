# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for connectors.registry.ConnectorRegistry.get_all — memaix-src card
4daa20e2 (multi-source resolution, added alongside the existing single-
source get())."""

from __future__ import annotations

from memaix_gateway.acl import Acl
from memaix_gateway.connectors.registry import ConnectorRegistry, ConnectorSpec


class _FakeTokenStore:
    def __init__(self, accounts=None, tokens=None, scopes=None):
        self._accounts = accounts or {}
        self._tokens = tokens or {}
        # scopes=None means default-allow, so the tests that predate project
        # scoping keep asserting resolution behaviour only. Pass an explicit
        # {(provider, account, capability): [projects]} map to exercise the gate.
        self._scopes = scopes

    def list_accounts(self, user: str) -> list[dict]:
        return self._accounts.get(user, [])

    def load_one(self, user: str, provider: str, account: str):
        return self._tokens.get((user, provider, account))

    def is_allowed(self, user, provider, account, capability, project) -> bool:
        if self._scopes is None:
            return True
        granted = self._scopes.get((provider, account, capability), [])
        return project in granted or "*" in granted


def _acl(calendar_cfg):
    return Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/srv/vaults/acme", "calendar": calendar_cfg}},
    )


def test_get_all_returns_empty_list_when_resource_unconfigured():
    registry = ConnectorRegistry()
    acl = Acl(users={"alice": {"grants": {"acme": "owner"}}}, projects={"acme": {"vault": "/x"}})
    assert registry.get_all(acl, _FakeTokenStore(), "acme", "calendar", "alice") == []


def test_get_all_shared_type_returns_single_adapter():
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="caldav", capability="calendar", auth="shared",
            factory=lambda acl, project, user, cfg, token: f"caldav-adapter:{cfg['url']}",
        )
    )
    acl = _acl({"type": "caldav", "url": "https://x/cal"})
    result = registry.get_all(acl, _FakeTokenStore(), "acme", "calendar", "alice")
    assert result == [("caldav:acme", "caldav-adapter:https://x/cal")]


def test_get_all_per_user_returns_one_adapter_per_linked_account():
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="google", capability="calendar", auth="per_user",
            factory=lambda acl, project, user, cfg, token: f"google-adapter:{token['email']}",
        )
    )
    acl = _acl({"type": "google", "auth": "per_user"})
    store = _FakeTokenStore(
        accounts={
            "alice": [
                {"provider": "google", "account": "a@gmail.com"},
                {"provider": "google", "account": "b@gmail.com"},
            ]
        },
        tokens={
            ("alice", "google", "a@gmail.com"): {"email": "a@gmail.com"},
            ("alice", "google", "b@gmail.com"): {"email": "b@gmail.com"},
        },
    )
    result = registry.get_all(acl, store, "acme", "calendar", "alice")
    assert len(result) == 2
    labels = {label for label, _ in result}
    assert labels == {"google:a@gmail.com", "google:b@gmail.com"}


def test_get_all_per_user_skips_other_users_and_other_providers():
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="google", capability="calendar", auth="per_user", factory=lambda a, p, u, c, t: t)
    )
    acl = _acl({"type": "google", "auth": "per_user"})
    store = _FakeTokenStore(
        accounts={
            "alice": [{"provider": "microsoft", "account": "a@x.com"}],
            "bob": [{"provider": "google", "account": "bob@gmail.com"}],
        }
    )
    assert registry.get_all(acl, store, "acme", "calendar", "alice") == []


def test_get_all_includes_extra_sources_list():
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="google", capability="calendar", auth="per_user", factory=lambda a, p, u, c, t: "google")
    )
    registry.register(
        ConnectorSpec(
            type="caldav", capability="calendar", auth="shared",
            factory=lambda a, p, u, c, t: f"caldav:{c['url']}",
        )
    )
    acl = _acl({
        "type": "google", "auth": "per_user",
        "sources": [{"type": "caldav", "label": "Delad projektkalender", "url": "https://team/cal"}],
    })
    store = _FakeTokenStore(
        accounts={"alice": [{"provider": "google", "account": "a@gmail.com"}]},
        tokens={("alice", "google", "a@gmail.com"): {}},
    )
    result = registry.get_all(acl, store, "acme", "calendar", "alice")
    assert ("google:a@gmail.com", "google") in result
    assert ("Delad projektkalender", "caldav:https://team/cal") in result


def test_get_all_extra_source_unknown_type_is_skipped_not_raised():
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="caldav", capability="calendar", auth="shared", factory=lambda a, p, u, c, t: "caldav")
    )
    acl = _acl({"type": "caldav", "url": "https://x", "sources": [{"type": "unknown_type"}]})
    result = registry.get_all(acl, _FakeTokenStore(), "acme", "calendar", "alice")
    assert result == [("caldav:acme", "caldav")]


def test_get_all_extra_source_per_user_without_linked_account_is_skipped():
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="caldav", capability="calendar", auth="shared", factory=lambda a, p, u, c, t: "caldav")
    )
    registry.register(
        ConnectorSpec(type="microsoft", capability="calendar", auth="per_user", factory=lambda a, p, u, c, t: t)
    )
    acl = _acl({"type": "caldav", "url": "https://x", "sources": [{"type": "microsoft"}]})
    result = registry.get_all(acl, _FakeTokenStore(), "acme", "calendar", "alice")
    assert result == [("caldav:acme", "caldav")]


def test_get_all_per_user_sweep_finds_google_when_base_type_is_caldav():
    """Google is linked but acl.yaml still says caldav — the sweep must pick it up."""
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="caldav", capability="calendar", auth="shared", factory=lambda a, p, u, c, t: "caldav")
    )
    registry.register(
        ConnectorSpec(
            type="google", capability="calendar", auth="per_user",
            factory=lambda a, p, u, c, t: f"google:{t['email']}",
        )
    )
    acl = _acl({"url": "https://x/cal"})  # no type key — defaults to caldav
    store = _FakeTokenStore(
        accounts={"alice": [{"provider": "google", "account": "a@gmail.com"}]},
        tokens={("alice", "google", "a@gmail.com"): {"email": "a@gmail.com"}},
    )
    result = registry.get_all(acl, store, "acme", "calendar", "alice")
    labels = {label for label, _ in result}
    assert "caldav:acme" in labels
    assert "google:a@gmail.com" in labels


def test_get_all_per_user_sweep_finds_ical_secret_when_linked():
    """iCal-secret linked user sees it in sources even when base type is caldav."""
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="caldav", capability="calendar", auth="shared", factory=lambda a, p, u, c, t: "caldav")
    )
    registry.register(
        ConnectorSpec(
            type="ical_secret", capability="calendar", auth="per_user", provider="ical_secret",
            factory=lambda a, p, u, c, t: f"ical:{t['ical_url']}",
        )
    )
    acl = _acl({"url": "https://x/cal"})
    store = _FakeTokenStore(
        accounts={"alice": [{"provider": "ical_secret", "account": "ical_secret"}]},
        tokens={("alice", "ical_secret", "ical_secret"): {"ical_url": "https://cal.example/secret.ics"}},
    )
    result = registry.get_all(acl, store, "acme", "calendar", "alice")
    labels = {label for label, _ in result}
    assert "ical_secret:ical_secret" in labels


def test_get_all_per_user_sweep_works_without_resource_cfg():
    """Even without any calendar resource in acl.yaml, per_user sweep finds linked accounts."""
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(type="google", capability="calendar", auth="per_user", factory=lambda a, p, u, c, t: "google")
    )
    acl = Acl(users={"alice": {"grants": {"acme": "owner"}}}, projects={"acme": {"vault": "/x"}})
    store = _FakeTokenStore(
        accounts={"alice": [{"provider": "google", "account": "a@gmail.com"}]},
        tokens={("alice", "google", "a@gmail.com"): {}},
    )
    result = registry.get_all(acl, store, "acme", "calendar", "alice")
    assert result == [("google:a@gmail.com", "google")]
