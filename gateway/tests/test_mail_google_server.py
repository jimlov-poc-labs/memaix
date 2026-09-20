# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gmail as a per-user mail source, end to end through server.email_list.

The adapter's own translation is covered in test_mail_google_adapter.py;
what matters here is the wiring: a linked Google account becomes a mail
source only when its owner has shared it for `mail`, and its access token
is refreshed before the registry loads it.
"""

from __future__ import annotations

import base64
import time

import pytest
from cryptography.fernet import Fernet

from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.connectors import registry as registry_mod
from memaix_gateway.notify.store import NotifyStore
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.rules.store import RulesStore
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.timeline.store import ActionsStore


def _link_gmail(token_store, account: str, user: str = "alice") -> None:
    """A linked Google account as it actually looks once stored.

    The absolute `expires_at` is the point: without it the freshness check
    reads the token as expired on every request and tries to refresh it,
    which in these tests fails (there is no Google) and flags the account
    needs_relink — so the mailbox under test would vanish for reasons that
    have nothing to do with what the test is asserting.
    """
    token_store.store(user, "google", account, {
        "access_token": "tok", "refresh_token": "r1",
        "expires_at": time.time() + 3600,
    })


@pytest.fixture()
def token_store(tmp_path):
    return TokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key())


@pytest.fixture()
def wired(tmp_path, monkeypatch, token_store):
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True)
    acl = Acl(
        users={"alice": {"grants": {"proj": "owner", "other": "owner"}}},
        projects={
            "proj": {"vault": str(vault)},
            "other": {"vault": str(vault)},
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
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("MEMAIX_USER", "alice")
    server._rate_limiter._windows.clear()
    return acl, token_store


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def _fake_gmail(subject: str):
    """Patch requests.request so the real GmailAdapter talks to a fake API."""

    def _request(method, url, **kwargs):
        if url.endswith("/messages"):
            return _FakeResponse({"messages": [{"id": "g1"}]})
        return _FakeResponse(
            {
                "id": "g1",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": subject},
                        {"name": "From", "value": "someone@example.com"},
                        {"name": "Date", "value": "Mon, 6 Jan 2025 10:00:00 +0000"},
                    ],
                    "body": {"data": _b64("body")},
                },
            }
        )

    return _request


# ------------------------------------------------------------------
# The scope gate, end to end
# ------------------------------------------------------------------


def test_linked_but_unshared_google_account_is_not_a_mail_source(wired, monkeypatch):
    """Opt-in, proven at the tool boundary: linking alone must not expose
    the mailbox.

    The project has no acl.yaml mailbox either, so once the gate has
    filtered the unshared account away there is genuinely nothing left to
    resolve — hence the "no mailbox configured" ValueError rather than
    ConnectorAuthRequired. Without the gate this call would SUCCEED (the
    per-user sweep in registry.get_all picks up every linked account of a
    per_user provider), which is exactly what makes the raise meaningful."""
    _, token_store = wired
    _link_gmail(token_store, "a@gmail.com")
    monkeypatch.setattr("requests.request", _fake_gmail("Should not appear"))

    with pytest.raises(ValueError, match="no mail configured"):
        server.email_list("proj")


def test_shared_google_account_appears_in_email_list(wired, monkeypatch):
    _, token_store = wired
    _link_gmail(token_store, "a@gmail.com")
    token_store.set_scopes("alice", "google", "a@gmail.com", "mail", ["proj"])
    monkeypatch.setattr("requests.request", _fake_gmail("From Gmail"))

    result = server.email_list("proj")

    assert [m["subject"] for m in result] == ["From Gmail"]


def test_sharing_with_one_project_does_not_leak_into_another(wired, monkeypatch):
    _, token_store = wired
    _link_gmail(token_store, "a@gmail.com")
    token_store.set_scopes("alice", "google", "a@gmail.com", "mail", ["proj"])
    monkeypatch.setattr("requests.request", _fake_gmail("From Gmail"))

    assert [m["subject"] for m in server.email_list("proj")] == ["From Gmail"]
    with pytest.raises(ValueError, match="no mail configured"):
        server.email_list("other")


def test_calendar_scope_alone_does_not_expose_the_mailbox(wired, monkeypatch):
    """The per-capability promise: sharing a Google calendar must not hand
    over the Gmail account behind it."""
    _, token_store = wired
    _link_gmail(token_store, "a@gmail.com")
    token_store.set_scopes("alice", "google", "a@gmail.com", "calendar", ["proj"])
    monkeypatch.setattr("requests.request", _fake_gmail("Should not appear"))

    with pytest.raises(ValueError, match="no mail configured"):
        server.email_list("proj")


# ------------------------------------------------------------------
# A Gmail-only project — no acl.yaml `mailbox` at all
# ------------------------------------------------------------------


def test_gmail_only_project_reports_an_empty_inbox_label(wired, monkeypatch):
    """`inbox` names the acl.yaml mailbox a message arrived in. There isn't
    one here, so it degrades to absent rather than taking the whole call
    down — the account's own address already labels the source."""
    _, token_store = wired
    _link_gmail(token_store, "a@gmail.com")
    token_store.set_scopes("alice", "google", "a@gmail.com", "mail", ["proj"])
    monkeypatch.setattr("requests.request", _fake_gmail("From Gmail"))

    (msg,) = server.email_list("proj")

    assert "inbox" not in msg


def test_gmail_only_project_can_create_a_draft(wired, monkeypatch):
    """The From header is left to Gmail, which stamps the authenticated
    account — the acl.yaml address this used to require was never the right
    sender for a linked account anyway."""
    _, token_store = wired
    _link_gmail(token_store, "a@gmail.com")
    token_store.set_scopes("alice", "google", "a@gmail.com", "mail", ["proj"])
    created: list = []

    def _request(method, url, **kwargs):
        if url.endswith("/drafts"):
            created.append(kwargs["json"])
            return _FakeResponse({"id": "d1"})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr("requests.request", _request)

    result = server.email_create_draft("proj", "them@example.com", "Hi", "body")

    assert result["status"] == "draft_created"
    raw = created[0]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "To: them@example.com" in decoded
    assert "From:" not in decoded


# ------------------------------------------------------------------
# _ensure_fresh_google_mail_token
# ------------------------------------------------------------------


def test_no_linked_google_account_is_a_noop(wired):
    server._ensure_fresh_google_mail_token("alice")  # must not raise


def test_fresh_token_is_not_refreshed(wired, monkeypatch):
    _, token_store = wired
    token_store.store("alice", "google", "a@gmail.com", {
        "access_token": "still-good", "refresh_token": "r1",
        "expires_at": time.time() + 3600,
    })
    called = []
    monkeypatch.setattr("requests.post", lambda *a, **kw: called.append(1))

    server._ensure_fresh_google_mail_token("alice")

    assert called == []


def test_every_linked_account_is_refreshed_not_just_the_first(wired, monkeypatch):
    """Multi-account is the point of this feature — a stale token on the
    second account would fail the merged fetch while the first looked fine."""
    _, token_store = wired
    for name in ("a@gmail.com", "b@gmail.com"):
        token_store.store("alice", "google", name, {
            "access_token": "stale", "refresh_token": "r1",
            "expires_at": time.time() - 10,
        })

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "fresh", "expires_in": 3600}

    monkeypatch.setattr("requests.post", lambda *a, **kw: _FakeResp())
    monkeypatch.setattr(server.config, "load", lambda: {"memaix": {}})

    server._ensure_fresh_google_mail_token("alice")

    for name in ("a@gmail.com", "b@gmail.com"):
        assert token_store.load_one("alice", "google", name)["access_token"] == "fresh"


def test_failed_refresh_marks_the_account_for_relink(wired, monkeypatch):
    _, token_store = wired
    token_store.store("alice", "google", "a@gmail.com", {
        "access_token": "stale", "refresh_token": "r1",
        "expires_at": time.time() - 10,
    })

    def _boom(*a, **kw):
        raise RuntimeError("refresh refused")

    monkeypatch.setattr("requests.post", _boom)
    monkeypatch.setattr(server.config, "load", lambda: {"memaix": {}})

    server._ensure_fresh_google_mail_token("alice")

    (account,) = [a for a in token_store.list_accounts("alice") if a["provider"] == "google"]
    assert account["status"] == "needs_relink"


# ------------------------------------------------------------------
# Absolute expiry — the root cause behind "every call refreshes"
# ------------------------------------------------------------------


def test_relative_expires_in_is_converted_to_an_absolute_instant():
    """An OAuth response dates itself relatively, which stops being true
    the moment it's written to disk."""
    before = time.time()

    stamped = server._stamp_expiry({"access_token": "t", "expires_in": 3599})

    assert before + 3599 <= stamped["expires_at"] <= time.time() + 3599


def test_a_provider_that_omits_expires_in_gets_no_fabricated_deadline():
    assert "expires_at" not in server._stamp_expiry({"access_token": "t"})


def test_a_just_refreshed_token_is_not_refreshed_again(wired, monkeypatch):
    """The regression this fix exists for. `expires_in` alone made the
    freshness check compute epoch+3599 — a 1970 timestamp — so every single
    request re-refreshed, and a revoked refresh_token turned into a hard
    outage instead of a stale-but-working token."""
    _, token_store = wired
    token_store.store("alice", "google", "a@gmail.com", server._stamp_expiry({
        "access_token": "fresh", "refresh_token": "r1", "expires_in": 3599,
    }))
    calls = []
    monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append(1))

    server._ensure_fresh_google_mail_token("alice")

    assert calls == []
