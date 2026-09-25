# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-level tests for email_* — connector-registry wiring
(FEATURE-CONNECTOR-FRAMEWORK.md Byggordning step 4: `_make_mailbox` is
resolved through the registry instead of tools/email.py building it
itself, with no behavior change for callers)."""

from __future__ import annotations

import pytest

from memaix_gateway import server
from memaix_gateway.acl import AccessDenied, Acl
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.notify.store import NotifyStore
from memaix_gateway.rules.store import RulesStore
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.timeline.store import ActionsStore
import memaix_gateway.connectors.registry as registry_mod
from memaix_gateway.connectors.registry import ConnectorRegistry, ConnectorSpec


class _Msg:
    def __init__(self, uid, subject, text="body"):
        self.uid = uid
        self.subject = subject
        self.from_ = "sender@example.com"
        self.to = ["me@example.com"]
        self.cc = []
        self.date_str = "2025-01-06"
        self.flags = ()
        self.text = text
        self.html = ""


class _FakeFolder:
    def set(self, name):
        pass


class _FakeMailbox:
    def __init__(self):
        self.folder = _FakeFolder()
        self._msgs = [_Msg("1", "Hello there")]
        self.appended = []

    def fetch(self, criteria="ALL", *, mark_seen=False, limit=None):
        if criteria.startswith("UID "):
            uid = criteria.split(" ", 1)[1]
            return [m for m in self._msgs if m.uid == uid]
        if criteria.startswith("TEXT "):
            needle = criteria.split('"')[1]
            return [m for m in self._msgs if needle in m.text]
        return list(self._msgs)[:limit] if limit else list(self._msgs)

    def append(self, message, folder="INBOX", dt=None, flag_set=None):
        self.appended.append((message, flag_set, folder))


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True)
    acl = Acl(
        users={
            "alice": {"grants": {"proj": "owner"}},
            "bob": {"grants": {"proj": "reader"}},
        },
        projects={"proj": {"vault": str(vault), "mailbox": {"host": "imap.example.com", "user": "me@example.com"}}},
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
    monkeypatch.setenv("MEMAIX_USER", "alice")
    server._rate_limiter._windows.clear()

    backend = _FakeMailbox()
    fake_registry = ConnectorRegistry()
    fake_registry.register(ConnectorSpec(type="imap", capability="mail", auth="shared", factory=lambda *a: backend))
    monkeypatch.setattr(registry_mod, "_registry", fake_registry)
    return vault, acl, backend


def test_email_list_routes_through_connector_registry(wired):
    result = server.email_list("proj")
    assert result[0]["subject"] == "Hello there"


def test_email_read_routes_through_connector_registry(wired):
    result = server.email_read("proj", "1")
    assert result["subject"] == "Hello there"
    assert result["body"] == "body"


def test_email_search_routes_through_connector_registry(wired):
    result = server.email_search("proj", "body")
    assert len(result) == 1


def test_email_create_draft_routes_through_connector_registry(wired):
    _, _, backend = wired
    result = server.email_create_draft("proj", "to@x.com", "Subj", "body")
    assert result["status"] == "draft_created"
    assert len(backend.appended) == 1


def test_email_create_draft_idempotency_key_survives_a_failed_append(wired, tmp_path, monkeypatch):
    """A failed append is not cached, so a retry with the same key really
    retries; once it lands, a further retry returns the cached result
    without a second draft."""
    from memaix_gateway.safety.idempotency import IdempotencyStore

    monkeypatch.setattr(server, "_idempotency_store", IdempotencyStore.for_path(tmp_path / "idem.db"))
    _, _, backend = wired
    real_append = backend.append
    calls = {"n": 0}

    def flaky_append(message, folder="INBOX", dt=None, flag_set=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("imap timeout")
        return real_append(message, folder=folder, dt=dt, flag_set=flag_set)

    backend.append = flaky_append

    with pytest.raises(TimeoutError):
        server.email_create_draft("proj", "to@x.com", "Subj", "body", idempotency_key="k1")
    first = server.email_create_draft("proj", "to@x.com", "Subj", "body", idempotency_key="k1")
    again = server.email_create_draft("proj", "to@x.com", "Subj", "body", idempotency_key="k1")

    assert first == again == {"status": "draft_created", "subject": "Subj", "account": "me@example.com"}
    assert len(backend.appended) == 1
    _, flag_set, folder = backend.appended[0]
    assert (folder, flag_set) == ("Drafts", ("\\Draft",))


def test_email_read_missing_message_raises(wired):
    with pytest.raises(FileNotFoundError):
        server.email_read("proj", "nope")


def test_email_list_reader_denied(wired, monkeypatch):
    monkeypatch.setenv("MEMAIX_USER", "bob")
    with pytest.raises(AccessDenied):
        server.email_list("proj")


def test_email_list_unconfigured_project_raises_and_is_audited(wired, monkeypatch):
    acl2 = Acl(users={"alice": {"grants": {"other": "owner"}}}, projects={"other": {"vault": "/tmp/x"}})
    monkeypatch.setattr(server, "_acl", acl2)
    with pytest.raises(ValueError):
        server.email_list("other")
    tools = [e["tool"] for e in server._get_audit().tail(5)]
    assert "email_list" in tools


def test_email_list_is_audited(wired):
    server.email_list("proj")
    tools = [e["tool"] for e in server._get_audit().tail(5)]
    assert "email_list" in tools
