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


# ---------------------------------------------------------------------------
# Karakteriseringstester (Sonar S3776-saneringen av get_all): förgreningarna
# som de äldre testerna inte nådde, plus kvirkar som låses.
# ---------------------------------------------------------------------------

def _spec(type_, auth, capability="calendar", provider=None):
    return ConnectorSpec(
        type=type_, capability=capability, auth=auth, provider=provider,
        factory=lambda acl, project, user, cfg, token: (type_, cfg.get("url"), token),
    )


def _google_store(*emails, scopes=None):
    return _FakeTokenStore(
        accounts={"alice": [{"provider": "google", "account": e} for e in emails]},
        tokens={("alice", "google", e): {"email": e} for e in emails},
        scopes=scopes,
    )


def test_get_all_extras_only_when_base_type_has_no_spec():
    reg = ConnectorRegistry()
    reg.register(_spec("caldav", "shared"))
    acl = _acl({"type": "nosuch", "sources": [{"type": "caldav", "url": "u1"}]})
    # base spec saknas -> inga basadaptrar, men extrakällan löses ändå
    assert reg.get_all(acl, _FakeTokenStore(), "acme", "calendar", "alice") == [
        ("caldav:0", ("caldav", "u1", None))
    ]


def test_get_all_extra_inherits_base_type_and_label_index_counts_skipped():
    reg = ConnectorRegistry()
    reg.register(_spec("caldav", "shared"))
    acl = _acl({
        "type": "caldav", "url": "base",
        "sources": [
            {"type": "unknown", "url": "skipped"},
            {"url": "u1"},
            {"url": "u2", "label": "Work"},
            {"url": "u3", "label": ""},
        ],
    })
    result = reg.get_all(acl, _FakeTokenStore(), "acme", "calendar", "alice")
    assert result == [
        ("caldav:acme", ("caldav", "base", None)),
        ("caldav:1", ("caldav", "u1", None)),
        ("Work", ("caldav", "u2", None)),
        ("caldav:3", ("caldav", "u3", None)),
    ]


def test_get_all_sources_none_or_missing_is_ignored():
    reg = ConnectorRegistry()
    reg.register(_spec("caldav", "shared"))
    for cfg in ({"type": "caldav", "sources": None}, {"type": "caldav", "sources": []}, {"type": "caldav"}):
        assert len(reg.get_all(_acl(cfg), _FakeTokenStore(), "acme", "calendar", "alice")) == 1


def test_get_all_extra_per_user_uses_first_scoped_account_only():
    reg = ConnectorRegistry()
    reg.register(_spec("caldav", "shared"))
    reg.register(_spec("google", "per_user"))
    acl = _acl({"type": "caldav", "url": "b", "sources": [{"type": "google", "label": "G"}]})
    store = _google_store("a@x", "b@x")
    result = reg.get_all(acl, store, "acme", "calendar", "alice")
    # extrakällan ger bara första kontot; svepet tar sedan alla (google var inte hanterad)
    assert result == [
        ("caldav:acme", ("caldav", "b", None)),
        ("G", ("google", None, {"email": "a@x"})),
        ("google:a@x", ("google", "b", {"email": "a@x"})),
        ("google:b@x", ("google", "b", {"email": "b@x"})),
    ]


def test_get_all_extra_per_user_skipped_without_account_or_token():
    reg = ConnectorRegistry()
    reg.register(_spec("caldav", "shared"))
    reg.register(_spec("google", "per_user"))
    acl = _acl({"type": "caldav", "url": "b", "sources": [{"type": "google"}]})
    # inga konton alls
    assert reg.get_all(acl, _FakeTokenStore(), "acme", "calendar", "alice") == [
        ("caldav:acme", ("caldav", "b", None))
    ]
    # konto utan token
    store = _FakeTokenStore(accounts={"alice": [{"provider": "google", "account": "a@x"}]})
    assert reg.get_all(acl, store, "acme", "calendar", "alice") == [("caldav:acme", ("caldav", "b", None))]


def test_get_all_extra_per_user_respects_scope():
    reg = ConnectorRegistry()
    reg.register(_spec("caldav", "shared"))
    reg.register(_spec("google", "per_user"))
    acl = _acl({"type": "caldav", "url": "b", "sources": [{"type": "google"}]})
    store = _google_store("a@x", scopes={("google", "a@x", "calendar"): ["other"]})
    assert reg.get_all(acl, store, "acme", "calendar", "alice") == [("caldav:acme", ("caldav", "b", None))]
    store = _google_store("a@x", scopes={("google", "a@x", "calendar"): ["acme"]})
    assert len(reg.get_all(acl, store, "acme", "calendar", "alice")) == 3


def test_get_all_base_per_user_skips_accounts_without_token_and_unscoped():
    reg = ConnectorRegistry()
    reg.register(_spec("google", "per_user"))
    acl = _acl({"type": "google"})
    store = _FakeTokenStore(
        accounts={"alice": [
            {"provider": "google", "account": "none@x"},
            {"provider": "google", "account": "ok@x"},
            {"provider": "google", "account": "hidden@x"},
            {"provider": "other", "account": "o@x"},
        ]},
        tokens={
            ("alice", "google", "ok@x"): {"email": "ok@x"},
            ("alice", "google", "hidden@x"): {"email": "hidden@x"},
        },
        scopes={
            ("google", "none@x", "calendar"): ["acme"],
            ("google", "ok@x", "calendar"): ["*"],
            ("google", "hidden@x", "calendar"): ["elsewhere"],
        },
    )
    assert reg.get_all(acl, store, "acme", "calendar", "alice") == [
        ("google:ok@x", ("google", None, {"email": "ok@x"}))
    ]


def test_get_all_sweep_without_resource_cfg_passes_empty_cfg():
    reg = ConnectorRegistry()
    reg.register(_spec("google", "per_user"))
    reg.register(_spec("caldav", "shared"))
    acl = Acl(users={"alice": {"grants": {"acme": "owner"}}}, projects={"acme": {"vault": "/x"}})
    result = reg.get_all(acl, _google_store("a@x"), "acme", "calendar", "alice")
    # shared-typer sveps aldrig; per_user ger adapter med tom cfg
    assert result == [("google:a@x", ("google", None, {"email": "a@x"}))]


def test_get_all_sweep_skips_other_capability_and_handled_base_type():
    reg = ConnectorRegistry()
    reg.register(_spec("google", "per_user"))
    reg.register(_spec("ical", "per_user"))
    reg.register(_spec("imap", "per_user", capability="mail"))
    acl = _acl({"type": "google"})
    store = _FakeTokenStore(
        accounts={"alice": [
            {"provider": "google", "account": "a@x"},
            {"provider": "ical", "account": "secret"},
        ]},
        tokens={("alice", "google", "a@x"): {"e": 1}, ("alice", "ical", "secret"): {"e": 2}},
    )
    result = reg.get_all(acl, store, "acme", "calendar", "alice")
    # google hanteras en gång (bas), ical via svep, mail-specen ignoreras
    assert [label for label, _ in result] == ["google:a@x", "ical:secret"]


def test_get_all_extra_plus_sweep_can_duplicate_a_type_today():
    """Kvirk som låses (ändra inte): en per_user-typ som bara finns som extrakälla
    läggs inte i handled_types, så svepet ger den en gång till."""
    reg = ConnectorRegistry()
    reg.register(_spec("caldav", "shared"))
    reg.register(_spec("ical", "per_user"))
    acl = _acl({"type": "caldav", "url": "b", "sources": [{"type": "ical", "label": "X"}]})
    store = _FakeTokenStore(
        accounts={"alice": [{"provider": "ical", "account": "s"}]},
        tokens={("alice", "ical", "s"): {"t": 1}},
    )
    labels = [label for label, _ in reg.get_all(acl, store, "acme", "calendar", "alice")]
    assert labels == ["caldav:acme", "X", "ical:s"]


def test_get_all_provider_override_is_used_for_accounts():
    reg = ConnectorRegistry()
    reg.register(_spec("gcal", "per_user", provider="google"))
    acl = _acl({"type": "gcal"})
    result = reg.get_all(acl, _google_store("a@x"), "acme", "calendar", "alice")
    assert result == [("gcal:a@x", ("gcal", None, {"email": "a@x"}))]


def test_get_all_mail_uses_mailbox_key_and_imap_default_type():
    reg = ConnectorRegistry()
    reg.register(_spec("imap", "shared", capability="mail"))
    acl = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/x", "mailbox": {"url": "m"}}},
    )
    assert reg.get_all(acl, _FakeTokenStore(), "acme", "mail", "alice") == [
        ("imap:acme", ("imap", "m", None))
    ]


def test_get_all_calendar_default_type_is_caldav():
    reg = ConnectorRegistry()
    reg.register(_spec("caldav", "shared"))
    assert reg.get_all(_acl({"url": "u"}), _FakeTokenStore(), "acme", "calendar", "alice") == [
        ("caldav:acme", ("caldav", "u", None))
    ]
