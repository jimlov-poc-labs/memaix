# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for email_* tools.

All IMAP/SMTP calls are replaced by in-process mocks — no network required.
"""

from __future__ import annotations

import pytest

from memaix_gateway.acl import Acl, AccessDenied
from memaix_gateway.tools.email import (
    email_create_draft,
    email_list,
    email_read,
    email_search,
    email_send,
)


# ------------------------------------------------------------------
# Mock mailbox
# ------------------------------------------------------------------


class _MockMessage:
    """Minimal duck-type for imap_tools.MailMessage."""

    def __init__(
        self,
        uid: str,
        subject: str,
        from_: str,
        date_str: str = "Mon, 01 Jan 2024 12:00:00 +0000",
        seen: bool = False,
        text: str = "body text",
    ) -> None:
        self.uid = uid
        self.subject = subject
        self.from_ = from_
        self.to = ("to@example.com",)
        self.cc = ()
        self.date_str = date_str
        self.flags = ("\\Seen",) if seen else ()
        self.text = text
        self.html = None


class _MockFolder:
    def set(self, name: str) -> None:
        pass


class _MockMailbox:
    """Duck-type for imap_tools.MailBox used by email_* tools."""

    def __init__(self, messages: list[_MockMessage]) -> None:
        self.folder = _MockFolder()
        self._messages = list(messages)
        self._drafts: list[bytes] = []

    def fetch(self, criteria=None, *, mark_seen: bool = False, limit: int | None = None):
        msgs = list(self._messages)
        # Crude UID filter: "UID 42"
        if criteria and str(criteria).startswith("UID "):
            uid = str(criteria).split(None, 1)[1].strip()
            msgs = [m for m in msgs if str(m.uid) == uid]
        # Crude text search: TEXT "term" (what email_search sends)
        elif criteria and 'TEXT "' in str(criteria):
            term = str(criteria).split('"')[1].lower()
            msgs = [m for m in msgs if term in (m.text or "").lower() or term in m.subject.lower()]
        if limit is not None:
            msgs = msgs[:limit]
        return iter(msgs)

    def append(self, message: bytes, folder: str = "INBOX", dt=None, flag_set=None):
        # Same signature as imap_tools.MailBox.append.
        self._drafts.append(message)

    def logout(self) -> None:
        pass


class _MockSmtp:
    def __init__(self) -> None:
        self.sent: list = []

    def send_message(self, msg) -> None:
        self.sent.append(msg)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

_MESSAGES = [
    _MockMessage("1", "Hello World", "alice@example.com", text="important content"),
    _MockMessage("2", "Re: Hello", "bob@example.com", text="reply here"),
    _MockMessage("3", "Meeting", "carol@example.com", text="let us meet"),
]


@pytest.fixture()
def acl():
    return Acl(
        users={
            "alice": {"grants": {"proj": "owner", "nosend": "owner"}},
            "carol": {"grants": {"proj": "collaborator"}},
            "bob": {"grants": {"proj": "reader"}},
        },
        projects={
            "proj": {
                "mailbox": {
                    "host": "imap.example.com",
                    "user": "jimmy@example.com",
                    "password_ref": "env:FAKE_PASSWORD",
                },
                "allow_send": True,
            },
            "nosend": {
                "mailbox": {
                    "host": "imap.example.com",
                    "user": "jimmy@example.com",
                    "password_ref": "env:FAKE_PASSWORD",
                },
                "allow_send": False,
            },
        },
    )


@pytest.fixture()
def imap():
    return _MockMailbox(_MESSAGES)


# ------------------------------------------------------------------
# ACL enforcement (no mock needed — checks happen before imap touch)
# ------------------------------------------------------------------


def test_email_list_denied_for_reader(acl, imap):
    with pytest.raises(AccessDenied):
        email_list(acl, "bob", "proj", _imap=imap)


def test_email_read_denied_for_unknown_user(acl, imap):
    with pytest.raises(AccessDenied):
        email_read(acl, "ghost", "proj", "1", _imap=imap)


def test_email_send_denied_for_collaborator(acl):
    smtp = _MockSmtp()
    with pytest.raises(AccessDenied):
        email_send(acl, "carol", "proj", "to@x.com", "subj", "body", _smtp=smtp)


# ------------------------------------------------------------------
# email_list
# ------------------------------------------------------------------


def test_email_list_returns_structured_results(acl, imap):
    results = email_list(acl, "carol", "proj", _imap=imap)
    assert isinstance(results, list)
    assert len(results) == 3
    first = results[0]
    assert "id" in first
    assert "subject" in first
    assert "from" in first
    assert "date" in first


def test_email_list_respects_limit(acl):
    imap = _MockMailbox(_MESSAGES)
    results = email_list(acl, "carol", "proj", limit=2, _imap=imap)
    assert len(results) == 2


def test_email_list_tags_messages_with_mailbox_address(acl, imap):
    results = email_list(acl, "carol", "proj", _imap=imap)
    assert all(m["inbox"] == "jimmy@example.com" for m in results)


# ------------------------------------------------------------------
# email_read
# ------------------------------------------------------------------


def test_email_read_returns_full_message(acl, imap):
    result = email_read(acl, "carol", "proj", "1", _imap=imap)
    assert result["id"] == "1"
    assert result["subject"] == "Hello World"
    assert "body" in result
    assert result["body"] == "important content"


def test_email_read_missing_raises_file_not_found(acl, imap):
    with pytest.raises(FileNotFoundError):
        email_read(acl, "carol", "proj", "999", _imap=imap)


# ------------------------------------------------------------------
# email_search
# ------------------------------------------------------------------


def test_email_search_returns_matches(acl, imap):
    results = email_search(acl, "carol", "proj", "important", _imap=imap)
    assert any(r["id"] == "1" for r in results)


# ------------------------------------------------------------------
# email_create_draft
# ------------------------------------------------------------------


def test_email_create_draft_appends_to_mailbox(acl, imap):
    result = email_create_draft(
        acl, "carol", "proj", "to@x.com", "Draft subject", "draft body", _imap=imap
    )
    assert result["status"] == "draft_created"
    assert len(imap._drafts) == 1


# ------------------------------------------------------------------
# email_send feature gate
# ------------------------------------------------------------------


def test_email_send_raises_when_allow_send_false(acl):
    smtp = _MockSmtp()
    with pytest.raises(RuntimeError, match="feature_disabled"):
        email_send(acl, "alice", "nosend", "to@x.com", "subj", "body", _smtp=smtp)


def test_email_send_succeeds_for_owner_with_allow_send(acl):
    smtp = _MockSmtp()
    result = email_send(acl, "alice", "proj", "to@x.com", "Hello", "body", _smtp=smtp)
    assert result["status"] == "sent"
    assert len(smtp.sent) == 1


# ------------------------------------------------------------------
# email_send outbox gate (FEATURE-APPROVAL-OUTBOX.md)
# ------------------------------------------------------------------


class _FakeOutbox:
    def __init__(self) -> None:
        self.enqueued: list = []

    def enqueue(self, user, project, tool, args, preview, ttl_h=72):
        self.enqueued.append((user, project, tool, args, preview))
        return "fake-action-id"


def test_email_send_review_mode_queues_instead_of_sending(acl):
    smtp = _MockSmtp()
    outbox = _FakeOutbox()
    cfg = {"memaix": {"outbox": {"default_mode": "review"}}}
    result = email_send(
        acl, "alice", "proj", "to@x.com", "Hello", "body", _smtp=smtp, _outbox=outbox, _cfg=cfg
    )
    assert result == {
        "pending": True, "action_id": "fake-action-id",
        "note": "Väntar på godkännande i utkorgen",
    }
    assert len(smtp.sent) == 0  # never touched SMTP
    assert len(outbox.enqueued) == 1
    assert outbox.enqueued[0][2] == "email_send"


def test_email_send_confirmed_bypasses_outbox_even_in_review(acl):
    smtp = _MockSmtp()
    outbox = _FakeOutbox()
    cfg = {"memaix": {"outbox": {"default_mode": "review"}}}
    result = email_send(
        acl, "alice", "proj", "to@x.com", "Hello", "body",
        _smtp=smtp, _outbox=outbox, _cfg=cfg, _confirmed=True,
    )
    assert result["status"] == "sent"
    assert len(smtp.sent) == 1
    assert outbox.enqueued == []


def test_email_send_still_enforces_owner_before_queueing(acl):
    """The ACL/feature-flag gate runs before the outbox gate — collaborators
    still can't queue a send they're not allowed to make at all."""
    outbox = _FakeOutbox()
    cfg = {"memaix": {"outbox": {"default_mode": "review"}}}
    with pytest.raises(AccessDenied):
        email_send(acl, "carol", "proj", "to@x.com", "s", "b", _outbox=outbox, _cfg=cfg)
    assert outbox.enqueued == []
