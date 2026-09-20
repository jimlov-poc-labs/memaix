# SPDX-License-Identifier: AGPL-3.0-or-later
"""A known-dead credential must not be handed to an adapter.

`needs_relink` is set only after a refresh has already been attempted and
failed, so it means "this credential is dead", not "this credential is
old". Resolving it anyway buys a guaranteed 401 at request time.

Found in production, not in review: mrjimlov@gmail.com's refresh token had
been revoked, and `email_list` for a project with THREE working mailboxes
returned a bare 401 — one expired account had become a total mail outage.

Uses a real TokenStore because the assertions are about the status
lifecycle (mark_needs_relink sets it, store clears it), which a fake would
simply be asserting about itself.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from memaix_gateway.acl import Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.connectors.registry import (
    ConnectorAuthRequired,
    ConnectorRegistry,
    ConnectorSpec,
)


@pytest.fixture()
def rig(tmp_path):
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="google_mail", capability="mail", auth="per_user", provider="google",
            factory=lambda acl, project, user, cfg, token: f"gmail:{token['who']}",
        )
    )
    acl = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": str(tmp_path / "v")}},
    )
    store = TokenStore.for_path(tmp_path / "t.db", Fernet.generate_key())
    return registry, acl, store


def _link(store, account, projects=("acme",)):
    store.store("alice", "google", account, {"who": account})
    store.set_scopes("alice", "google", account, "mail", list(projects))


def _accounts(results):
    return sorted(label.split(":", 1)[1] for label, _ in results)


def test_a_healthy_account_still_resolves(rig):
    """Guards the fix against over-filtering — the whole suite would still
    pass if the new clause dropped everything."""
    registry, acl, store = rig
    _link(store, "good@gmail.com")

    assert _accounts(registry.get_all(acl, store, "acme", "mail", "alice")) == ["good@gmail.com"]


def test_a_flagged_account_is_not_resolved(rig):
    registry, acl, store = rig
    _link(store, "dead@gmail.com")
    store.mark_needs_relink("alice", "google", "dead@gmail.com")

    assert registry.get_all(acl, store, "acme", "mail", "alice") == []


def test_one_dead_account_does_not_take_down_its_healthy_siblings(rig):
    """The regression that motivated the fix. get_all merges sources, so
    before this, a single revoked token made the whole listing raise — the
    two working mailboxes became unreachable through no fault of their own."""
    registry, acl, store = rig
    for a in ("good@gmail.com", "dead@gmail.com", "alsogood@gmail.com"):
        _link(store, a)
    store.mark_needs_relink("alice", "google", "dead@gmail.com")

    resolved = _accounts(registry.get_all(acl, store, "acme", "mail", "alice"))

    assert resolved == ["alsogood@gmail.com", "good@gmail.com"]


def test_get_raises_relink_rather_than_returning_a_doomed_adapter(rig):
    """The single-source path trades a bare upstream 401 for the error the
    UI already knows how to act on."""
    registry, acl, store = rig
    acl = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/v", "mailbox": {"type": "google_mail"}}},
    )
    _link(store, "dead@gmail.com")
    store.mark_needs_relink("alice", "google", "dead@gmail.com")

    with pytest.raises(ConnectorAuthRequired):
        registry.get(acl, store, "acme", "mail", "alice")


def test_relinking_restores_the_account(rig):
    """TokenStore.store resets status to 'active', so the drop is a
    consequence of the flag and not a one-way door."""
    registry, acl, store = rig
    _link(store, "back@gmail.com")
    store.mark_needs_relink("alice", "google", "back@gmail.com")
    store.store("alice", "google", "back@gmail.com", {"who": "back@gmail.com"})

    assert _accounts(registry.get_all(acl, store, "acme", "mail", "alice")) == ["back@gmail.com"]


def test_relinking_does_not_silently_widen_scope(rig):
    """store() deliberately leaves scopes alone, so coming back from
    needs_relink restores exactly the projects that were granted before —
    re-linking is a credential repair, not a fresh consent."""
    registry, acl, store = rig
    _link(store, "back@gmail.com", projects=["acme"])
    store.mark_needs_relink("alice", "google", "back@gmail.com")
    store.store("alice", "google", "back@gmail.com", {"who": "back@gmail.com"})

    assert store.list_scopes("alice") == [
        {
            "provider": "google",
            "account": "back@gmail.com",
            "capability": "mail",
            "projects": ["acme"],
        }
    ]
