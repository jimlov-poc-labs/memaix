# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-account project scoping — a linked mail/calendar account is usable
only by the projects its owner shared it with, per capability.

Three layers get their own section below: the TokenStore rows, the
ConnectorRegistry gate that reads them, and the account_* tools that write
them.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from memaix_gateway.acl import AccessDenied, Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.connectors.registry import (
    ConnectorAuthRequired,
    ConnectorRegistry,
    ConnectorSpec,
)
from memaix_gateway.tools import account as t_account


@pytest.fixture()
def store(tmp_path):
    return TokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key())


@pytest.fixture()
def linked(store):
    """One linked Google account, deliberately unscoped."""
    store.store("alice", "google", "a@gmail.com", {"access_token": "tok"})
    return store


def _acl():
    return Acl(
        users={
            "alice": {"grants": {"acme": "owner", "beta": "owner"}},
            "bob": {"grants": {"beta": "owner"}},
        },
        projects={
            "acme": {"vault": "/srv/vaults/acme"},
            "beta": {"vault": "/srv/vaults/beta"},
        },
    )


# ------------------------------------------------------------------
# TokenStore — the scope rows themselves
# ------------------------------------------------------------------


def test_newly_linked_account_is_allowed_nowhere(linked):
    """Opt-in: linking grants nothing until the user says where."""
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is False
    assert linked.list_scopes("alice") == []


def test_set_scopes_grants_only_the_named_projects(linked):
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", ["acme"])
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is True
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "beta") is False


def test_wildcard_grants_every_project_including_unknown_ones(linked):
    """'*' has to cover projects that don't exist yet, or a new project
    would silently start without the user's accounts."""
    linked.set_scopes("alice", "google", "a@gmail.com", "calendar", ["*"])
    assert linked.is_allowed("alice", "google", "a@gmail.com", "calendar", "beta") is True
    assert linked.is_allowed("alice", "google", "a@gmail.com", "calendar", "not-yet") is True


def test_wildcard_collapses_mixed_selection(linked):
    """['*', 'acme'] must not leave an 'acme' row that outlives the wildcard."""
    stored = linked.set_scopes("alice", "google", "a@gmail.com", "mail", ["*", "acme"])
    assert stored == ["*"]
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", [])
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is False


def test_capabilities_are_scoped_independently(linked):
    """Sharing a calendar must not share the mailbox behind it."""
    linked.set_scopes("alice", "google", "a@gmail.com", "calendar", ["acme"])
    assert linked.is_allowed("alice", "google", "a@gmail.com", "calendar", "acme") is True
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is False


def test_set_scopes_replaces_rather_than_merges(linked):
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", ["acme", "beta"])
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", ["beta"])
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is False
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "beta") is True


def test_empty_list_revokes_the_capability(linked):
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", ["acme"])
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", [])
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is False


def test_scopes_are_per_user(store):
    store.store("alice", "google", "a@gmail.com", {"access_token": "t"})
    store.store("bob", "google", "a@gmail.com", {"access_token": "t"})
    store.set_scopes("alice", "google", "a@gmail.com", "mail", ["*"])
    assert store.is_allowed("bob", "google", "a@gmail.com", "mail", "beta") is False


def test_unlinking_drops_the_scopes(linked):
    """A re-linked account must not inherit its predecessor's grants."""
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", ["*"])
    assert linked.delete("alice", "google", "a@gmail.com") is True
    linked.store("alice", "google", "a@gmail.com", {"access_token": "new"})
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is False


def test_list_scopes_folds_rows_into_one_entry_per_capability(linked):
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", ["beta", "acme"])
    assert linked.list_scopes("alice") == [
        {
            "provider": "google",
            "account": "a@gmail.com",
            "capability": "mail",
            "projects": ["acme", "beta"],
        }
    ]


# ------------------------------------------------------------------
# Backfill — existing accounts must not go dark on deploy
# ------------------------------------------------------------------


def test_backfill_grants_preexisting_accounts_everything(linked):
    inserted = linked.backfill_scopes_once(["mail", "calendar"])
    assert inserted == 2
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is True
    assert linked.is_allowed("alice", "google", "a@gmail.com", "calendar", "beta") is True


def test_backfill_runs_only_once(linked):
    linked.backfill_scopes_once(["mail", "calendar"])
    assert linked.backfill_scopes_once(["mail", "calendar"]) == 0


def test_backfill_does_not_resurrect_revoked_scopes(linked):
    """The marker is the whole point: a user who deliberately un-shared an
    account must not have it handed back on the next restart."""
    linked.backfill_scopes_once(["mail", "calendar"])
    linked.set_scopes("alice", "google", "a@gmail.com", "mail", [])
    linked.backfill_scopes_once(["mail", "calendar"])
    assert linked.is_allowed("alice", "google", "a@gmail.com", "mail", "acme") is False


def test_backfill_does_not_cover_accounts_linked_afterwards(linked):
    """Opt-in only makes sense if the migration is a one-time amnesty."""
    linked.backfill_scopes_once(["mail", "calendar"])
    linked.store("alice", "imap", "later@example.com", {"host": "h", "user": "u", "password": "p"})
    assert linked.is_allowed("alice", "imap", "later@example.com", "mail", "acme") is False


# ------------------------------------------------------------------
# ConnectorRegistry — the gate every mail/calendar tool passes through
# ------------------------------------------------------------------


def _registry():
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="google", capability="calendar", auth="per_user",
            factory=lambda acl, project, user, cfg, token: f"cal:{token['access_token']}",
        ),
        ConnectorSpec(
            type="imap_user", capability="mail", auth="per_user", provider="imap",
            factory=lambda acl, project, user, cfg, token: f"mail:{token['host']}",
        ),
    )
    return registry


def test_capabilities_lists_every_registered_capability():
    assert _registry().capabilities() == ["calendar", "mail"]


def test_get_all_hides_an_unscoped_account(linked):
    assert _registry().get_all(_acl(), linked, "acme", "calendar", "alice") == []


def test_get_all_shows_a_scoped_account_only_in_that_project(linked):
    linked.set_scopes("alice", "google", "a@gmail.com", "calendar", ["acme"])
    registry = _registry()
    assert registry.get_all(_acl(), linked, "acme", "calendar", "alice") == [
        ("google:a@gmail.com", "cal:tok")
    ]
    assert registry.get_all(_acl(), linked, "beta", "calendar", "alice") == []


def test_get_all_gate_is_per_capability(store):
    """Two accounts, one shared for mail and one for calendar — each
    capability sees only its own."""
    store.store("alice", "google", "a@gmail.com", {"access_token": "tok"})
    store.store("alice", "imap", "a@example.com", {"host": "h", "user": "u", "password": "p"})
    store.set_scopes("alice", "google", "a@gmail.com", "calendar", ["acme"])
    registry = _registry()
    assert registry.get_all(_acl(), store, "acme", "calendar", "alice") == [
        ("google:a@gmail.com", "cal:tok")
    ]
    assert registry.get_all(_acl(), store, "acme", "mail", "alice") == []


def test_get_all_keeps_shared_project_resources_unscoped(store):
    """acl.yaml resources belong to the project, not the user — the scope
    gate must not touch them, or a project's own mailbox would vanish."""
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="caldav", capability="calendar", auth="shared",
            factory=lambda acl, project, user, cfg, token: f"shared:{cfg['url']}",
        )
    )
    acl = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/v", "calendar": {"type": "caldav", "url": "https://x"}}},
    )
    assert registry.get_all(acl, store, "acme", "calendar", "alice") == [
        ("caldav:acme", "shared:https://x")
    ]


def test_get_raises_auth_required_for_an_unscoped_account(linked):
    acl = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/v", "calendar": {"type": "google"}}},
    )
    with pytest.raises(ConnectorAuthRequired):
        _registry().get(acl, linked, "acme", "calendar", "alice")


# ------------------------------------------------------------------
# account_* tools
# ------------------------------------------------------------------


def test_account_scope_set_round_trips(linked):
    result = t_account.account_scope_set(
        _acl(), "alice", "google", "a@gmail.com", "mail", ["acme"], linked
    )
    assert result["projects"] == ["acme"]
    assert t_account.account_scope_list(_acl(), "alice", linked) == [
        {
            "provider": "google",
            "account": "a@gmail.com",
            "capability": "mail",
            "projects": ["acme"],
        }
    ]


def test_account_scope_set_rejects_a_project_you_cannot_reach(store):
    """Otherwise it'd be a quiet way to stage a source in someone else's
    project."""
    store.store("bob", "google", "b@gmail.com", {"access_token": "tok"})
    with pytest.raises(AccessDenied):
        t_account.account_scope_set(
            _acl(), "bob", "google", "b@gmail.com", "mail", ["acme"], store
        )


def test_account_scope_set_rejects_an_unlinked_account(store):
    with pytest.raises(FileNotFoundError):
        t_account.account_scope_set(
            _acl(), "alice", "google", "ghost@gmail.com", "mail", ["acme"], store
        )


def test_account_scope_set_rejects_an_unknown_provider(linked):
    with pytest.raises(ValueError):
        t_account.account_scope_set(
            _acl(), "alice", "carrier-pigeon", "a@gmail.com", "mail", ["acme"], linked
        )


def test_account_list_reports_scopes_per_capability(linked):
    t_account.account_scope_set(
        _acl(), "alice", "google", "a@gmail.com", "calendar", ["*"], linked
    )
    (account,) = t_account.account_list(_acl(), "alice", linked)
    assert account["scopes_by_capability"] == {"calendar": ["*"]}


def test_account_list_reports_no_scopes_for_a_fresh_account(linked):
    (account,) = t_account.account_list(_acl(), "alice", linked)
    assert account["scopes_by_capability"] == {}
