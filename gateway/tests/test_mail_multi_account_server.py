# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-account mail tests — server._mail_backend/_with_mail_backend
migrated from registry.get() to registry.get_all() (FEATURE-CONNECTOR-
FRAMEWORK.md, IMAP per-user + multiple mail sources per user).

Single-source degenerate behavior is proven unchanged by
test_email_server.py and test_mail_microsoft_server.py staying green
UNMODIFIED (not re-tested here). This file only covers what's NEW: 2+
sources merged, UID routing on read, and per-user isolation when linked
accounts differ between users."""

from __future__ import annotations

import pytest

from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.connectors.adapters.mail_imap_user import build_mailbox
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.notify.store import NotifyStore
from memaix_gateway.rules.store import RulesStore
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.timeline.store import ActionsStore
from cryptography.fernet import Fernet
import memaix_gateway.connectors.registry as registry_mod


class _Msg:
    def __init__(self, uid, subject, text="body", seen=False):
        self.uid = uid
        self.subject = subject
        self.from_ = "sender@example.com"
        self.to = ["me@example.com"]
        self.cc = []
        self.date_str = "2025-01-06"
        self.flags = ()
        self.seen = seen
        self.text = text
        self.html = ""


class _FakeFolder:
    def set(self, name):
        pass


class _FakeMailbox:
    """A minimal imap_tools.MailBox stand-in, one per mail source.

    fetch() returns FRESH _Msg copies every call (like a real IMAP server
    would return a freshly-parsed message each round trip) — MultiMailBackend
    rewrites .uid on whatever it fetches to prefix it with the source label,
    so returning the same object twice would leak that mutation back into
    this fixture's own message list."""

    def __init__(self, msgs):
        self.folder = _FakeFolder()
        self._msgs = msgs
        self.appended = []

    def _copy(self, m):
        return _Msg(m.uid, m.subject, text=m.text, seen=m.seen)

    def fetch(self, criteria="ALL", *, mark_seen=False, limit=None):
        if criteria.startswith("UID "):
            uid = criteria.split(" ", 1)[1]
            return [self._copy(m) for m in self._msgs if m.uid == uid]
        if criteria.startswith("BODY "):
            needle = criteria.split('"')[1]
            return [self._copy(m) for m in self._msgs if needle in m.text]
        msgs = [self._copy(m) for m in self._msgs]
        return msgs[:limit] if limit else msgs

    def append(self, msg_bytes, flags, *, folder):
        self.appended.append((msg_bytes, flags, folder))

    def logout(self):
        pass


class _AutoScopedTokenStore(TokenStore):
    """TokenStore whose store() also shares the account with every project.

    These tests are about merging mail across sources, not about access
    control, and they predate project scoping — for them `store()` has
    always meant "linked AND usable here". Granting '*' on link keeps that
    meaning without threading a set_scopes call through fifteen call sites.
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
        users={
            "alice": {"grants": {"proj": "owner"}},
            "bob": {"grants": {"proj": "owner"}},
        },
        projects={
            "proj": {
                "vault": str(vault),
                "mailbox": {"host": "imap.shared.example.com", "user": "shared@example.com"},
            }
        },
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


def _register_imap_user_and_shared(monkeypatch, shared_mailbox, personal_mailboxes: dict):
    """personal_mailboxes: {host -> _FakeMailbox}. Wires the real registry's
    imap (shared) + imap_user (per_user) specs against fakes, so this
    exercises the actual catalog.py wiring end to end, not a hand-rolled
    fake registry like test_email_server.py's."""
    import memaix_gateway.tools.email as t_email
    import memaix_gateway.connectors.adapters.mail_imap_user as t_imap_user

    monkeypatch.setattr(t_email, "_make_mailbox", lambda acl, project: shared_mailbox)
    monkeypatch.setattr(
        t_imap_user, "build_mailbox", lambda token: personal_mailboxes[token["host"]]
    )
    # Force a fresh default_registry() (catalog.register_defaults) rather
    # than whatever prior test left behind.
    monkeypatch.setattr(registry_mod, "_registry", None)


# ------------------------------------------------------------------
# Two linked IMAP accounts (no shared mailbox involved) → merged list
# ------------------------------------------------------------------


def test_two_linked_imap_accounts_merge_in_email_list(wired, monkeypatch):
    acl, token_store = wired
    # Project has a shared mailbox too — both must appear, not just the per-user ones.
    shared = _FakeMailbox([_Msg("1", "From shared inbox")])
    personal_a = _FakeMailbox([_Msg("1", "From personal A")])
    personal_b = _FakeMailbox([_Msg("1", "From personal B")])
    _register_imap_user_and_shared(
        monkeypatch, shared,
        {"imap.a.example.com": personal_a, "imap.b.example.com": personal_b},
    )
    token_store.store("alice", "imap", "alice@a.example.com", {
        "host": "imap.a.example.com", "user": "alice", "password": "pw",
    })
    token_store.store("alice", "imap", "alice@b.example.com", {
        "host": "imap.b.example.com", "user": "alice", "password": "pw",
    })

    result = server.email_list("proj")
    subjects = {m["subject"] for m in result}
    assert subjects == {"From shared inbox", "From personal A", "From personal B"}
    # Every UID is prefixed with its source label so email_read can route it.
    assert all("|" in m["id"] for m in result)


def test_single_linked_imap_account_alongside_shared_mailbox_both_present(wired, monkeypatch):
    acl, token_store = wired
    shared = _FakeMailbox([_Msg("1", "Shared msg")])
    personal = _FakeMailbox([_Msg("1", "Personal msg")])
    _register_imap_user_and_shared(monkeypatch, shared, {"imap.a.example.com": personal})
    token_store.store("alice", "imap", "alice@a.example.com", {
        "host": "imap.a.example.com", "user": "alice", "password": "pw",
    })

    result = server.email_list("proj")
    subjects = {m["subject"] for m in result}
    assert subjects == {"Shared msg", "Personal msg"}  # not deduplicated away


# ------------------------------------------------------------------
# email_read routes a labeled UID back to the correct source
# ------------------------------------------------------------------


def test_email_read_routes_labeled_uid_to_correct_source(wired, monkeypatch):
    acl, token_store = wired
    shared = _FakeMailbox([_Msg("1", "Shared msg", text="shared body")])
    personal = _FakeMailbox([_Msg("1", "Personal msg", text="personal body")])
    _register_imap_user_and_shared(monkeypatch, shared, {"imap.a.example.com": personal})
    token_store.store("alice", "imap", "alice@a.example.com", {
        "host": "imap.a.example.com", "user": "alice", "password": "pw",
    })

    listing = server.email_list("proj")
    personal_entry = next(m for m in listing if m["subject"] == "Personal msg")
    shared_entry = next(m for m in listing if m["subject"] == "Shared msg")

    read_personal = server.email_read("proj", personal_entry["id"])
    assert read_personal["body"] == "personal body"

    read_shared = server.email_read("proj", shared_entry["id"])
    assert read_shared["body"] == "shared body"


def test_email_search_merges_across_sources(wired, monkeypatch):
    acl, token_store = wired
    shared = _FakeMailbox([_Msg("1", "S1", text="needle here")])
    personal = _FakeMailbox([_Msg("1", "P1", text="needle here too")])
    _register_imap_user_and_shared(monkeypatch, shared, {"imap.a.example.com": personal})
    token_store.store("alice", "imap", "alice@a.example.com", {
        "host": "imap.a.example.com", "user": "alice", "password": "pw",
    })

    result = server.email_search("proj", "needle")
    assert len(result) == 2


# ------------------------------------------------------------------
# Isolation: a user with no linked account never sees another user's mailbox
# ------------------------------------------------------------------


def test_user_without_linked_imap_account_sees_only_shared_mailbox(wired, monkeypatch):
    acl, token_store = wired
    shared = _FakeMailbox([_Msg("1", "Shared only")])
    personal = _FakeMailbox([_Msg("1", "Alice private")])
    _register_imap_user_and_shared(monkeypatch, shared, {"imap.a.example.com": personal})
    token_store.store("alice", "imap", "alice@a.example.com", {
        "host": "imap.a.example.com", "user": "alice", "password": "pw",
    })

    monkeypatch.setenv("MEMAIX_USER", "bob")
    result = server.email_list("proj")
    subjects = {m["subject"] for m in result}
    assert subjects == {"Shared only"}
    assert "Alice private" not in subjects


def test_bobs_linked_account_never_appears_for_alice(wired, monkeypatch):
    acl, token_store = wired
    shared = _FakeMailbox([_Msg("1", "Shared only")])
    bobs_personal = _FakeMailbox([_Msg("1", "Bob private")])
    _register_imap_user_and_shared(monkeypatch, shared, {"imap.bob.example.com": bobs_personal})
    token_store.store("bob", "imap", "bob@bob.example.com", {
        "host": "imap.bob.example.com", "user": "bob", "password": "pw",
    })

    # alice has no linked imap account — must not see bob's.
    result = server.email_list("proj")
    subjects = {m["subject"] for m in result}
    assert subjects == {"Shared only"}
    assert "Bob private" not in subjects


def test_token_store_queries_are_scoped_to_the_calling_user(wired):
    """Direct proof the token-store query underneath email routing filters
    on memaix_user — not just an assertion on tool output."""
    _, token_store = wired
    token_store.store("alice", "imap", "alice@a.example.com", {
        "host": "imap.a.example.com", "user": "alice", "password": "pw",
    })
    token_store.store("bob", "imap", "bob@b.example.com", {
        "host": "imap.b.example.com", "user": "bob", "password": "pw",
    })
    alice_accounts = token_store.list_accounts("alice")
    bob_accounts = token_store.list_accounts("bob")
    assert [a["account"] for a in alice_accounts] == ["alice@a.example.com"]
    assert [a["account"] for a in bob_accounts] == ["bob@b.example.com"]


# ------------------------------------------------------------------
# email_create_draft: multi-source still appends to exactly one mailbox
# ------------------------------------------------------------------


def test_email_create_draft_with_multi_source_appends_to_first_source_only(wired, monkeypatch):
    acl, token_store = wired
    shared = _FakeMailbox([])
    personal = _FakeMailbox([])
    _register_imap_user_and_shared(monkeypatch, shared, {"imap.a.example.com": personal})
    token_store.store("alice", "imap", "alice@a.example.com", {
        "host": "imap.a.example.com", "user": "alice", "password": "pw",
    })

    server.email_create_draft("proj", "to@x.com", "Subj", "body")
    total_appends = len(shared.appended) + len(personal.appended)
    assert total_appends == 1
