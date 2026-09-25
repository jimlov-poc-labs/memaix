# SPDX-License-Identifier: AGPL-3.0-or-later
"""email_create_draft's `account`: which mailbox gets the draft (backlog 24e8a91f).

A project with the acl.yaml mailbox (booking@memaix.se) AND a linked Gmail
account (jimmy@jimlov.se) used to get every draft in the acl.yaml mailbox,
stamped From: booking@memaix.se — so a reply to a Gmail thread could never be
drafted in Gmail. With email_send disabled by policy, a draft in the right
mailbox is the only way to answer at all.
"""

from __future__ import annotations

from email import message_from_bytes

import pytest

from memaix_gateway.acl import AccessDenied, Acl
from memaix_gateway.connectors.adapters.mail_multi import MultiMailBackend
from memaix_gateway.tools.email import DRAFTS, email_create_draft

_SHARED_LABEL = "imap:jimlov"
_GMAIL_LABEL = "google_mail:jimmy@jimlov.se"


class _Msg:
    def __init__(self, uid: str, message_id: str) -> None:
        self.uid = uid
        self.subject = "Fråga"
        self.from_ = "kund@example.com"
        self.to = ("jimmy@jimlov.se",)
        self.cc = ()
        self.date_str = "Mon, 01 Jan 2024 12:00:00 +0000"
        self.seen = False
        self.text = "hej"
        self.html = ""
        self.headers = {"message-id": (message_id,)}


class _Folder:
    def set(self, name: str) -> None:
        pass


class _FakeSource:
    """One mail source. No folder.list(), like the Gmail/Graph adapters, so
    email_create_draft hands it the logical DRAFTS folder."""

    def __init__(self, msgs=()) -> None:
        self.folder = _Folder()
        self._msgs = list(msgs)
        self.appended: list[tuple[bytes, str, object]] = []

    def fetch(self, criteria="ALL", *, mark_seen=False, limit=None):
        if criteria.startswith("UID "):
            uid = criteria.split(" ", 1)[1]
            return [_Msg(m.uid, m.headers["message-id"][0]) for m in self._msgs if m.uid == uid]
        return list(self._msgs)

    def append(self, message, folder="INBOX", dt=None, flag_set=None):
        self.appended.append((message, folder, flag_set))

    def logout(self) -> None:
        pass


@pytest.fixture()
def acl():
    return Acl(
        users={
            "jimmy": {"grants": {"jimlov": "owner"}},
            "carol": {"grants": {"jimlov": "collaborator"}},
            "bob": {"grants": {"jimlov": "reader"}},
        },
        projects={"jimlov": {"mailbox": {"host": "imap.example.com", "user": "booking@memaix.se"}}},
    )


@pytest.fixture()
def sources():
    shared = _FakeSource()
    gmail = _FakeSource([_Msg("18f2c", "<orig@mail.gmail.com>")])
    return shared, gmail, MultiMailBackend([(_SHARED_LABEL, shared), (_GMAIL_LABEL, gmail)])


def _headers(appended) -> dict:
    message, _folder, _flags = appended[0]
    return dict(message_from_bytes(message).items())


def test_linked_gmail_account_gets_the_draft_without_a_from(acl, sources):
    shared, gmail, mb = sources

    result = email_create_draft(
        acl, "jimmy", "jimlov", "kund@example.com", "Offert", "Hej", account="jimmy@jimlov.se", _imap=mb,
    )

    assert result == {"status": "draft_created", "subject": "Offert", "account": "jimmy@jimlov.se"}
    assert shared.appended == []
    assert len(gmail.appended) == 1
    _message, folder, flags = gmail.appended[0]
    assert folder == DRAFTS
    assert flags == ("\\Draft",)
    # Gmail stamps the account's own address; booking@memaix.se must not leak in.
    assert "From" not in _headers(gmail.appended)


def test_account_matches_case_insensitively_and_by_label(acl, sources):
    _shared, gmail, mb = sources

    email_create_draft(acl, "jimmy", "jimlov", "k@example.com", "A", "b", account="Jimmy@JimLov.se", _imap=mb)
    email_create_draft(acl, "jimmy", "jimlov", "k@example.com", "B", "b", account=_GMAIL_LABEL, _imap=mb)

    assert len(gmail.appended) == 2


def test_default_is_unchanged_acl_yaml_mailbox_with_from(acl, sources):
    shared, gmail, mb = sources

    result = email_create_draft(acl, "jimmy", "jimlov", "kund@example.com", "Offert", "Hej", _imap=mb)

    assert result == {"status": "draft_created", "subject": "Offert", "account": "booking@memaix.se"}
    assert gmail.appended == []
    assert len(shared.appended) == 1
    assert _headers(shared.appended)["From"] == "booking@memaix.se"


def test_explicit_acl_yaml_account_keeps_its_from(acl, sources):
    shared, gmail, mb = sources

    result = email_create_draft(
        acl, "jimmy", "jimlov", "k@example.com", "S", "b", account="booking@memaix.se", _imap=mb,
    )

    assert result["account"] == "booking@memaix.se"
    assert gmail.appended == []
    assert _headers(shared.appended)["From"] == "booking@memaix.se"


def test_unknown_account_fails_and_lists_the_valid_ones(acl, sources):
    shared, gmail, mb = sources

    with pytest.raises(ValueError) as exc:
        email_create_draft(acl, "jimmy", "jimlov", "k@example.com", "S", "b", account="nobody@example.com", _imap=mb)

    assert "nobody@example.com" in str(exc.value)
    assert "booking@memaix.se" in str(exc.value)
    assert "jimmy@jimlov.se" in str(exc.value)
    assert shared.appended == [] and gmail.appended == []


def test_unknown_account_on_a_single_mailbox_project_fails_too(acl):
    single = _FakeSource()

    with pytest.raises(ValueError, match="booking@memaix.se"):
        email_create_draft(acl, "jimmy", "jimlov", "k@example.com", "S", "b", account="jimmy@jimlov.se", _imap=single)
    assert single.appended == []


def test_reply_to_a_linked_source_id_goes_to_that_source(acl, sources):
    shared, gmail, mb = sources

    result = email_create_draft(
        acl, "jimmy", "jimlov", "kund@example.com", "Re: Fråga", "Svar",
        in_reply_to=f"{_GMAIL_LABEL}|18f2c", _imap=mb,
    )

    assert result["account"] == "jimmy@jimlov.se"
    assert shared.appended == []
    headers = _headers(gmail.appended)
    assert headers["In-Reply-To"] == "<orig@mail.gmail.com>"
    assert "From" not in headers


def test_reply_by_plain_message_id_keeps_the_default(acl, sources):
    shared, gmail, mb = sources

    result = email_create_draft(
        acl, "jimmy", "jimlov", "k@example.com", "Re: x", "b", in_reply_to="<abc@host>", _imap=mb,
    )

    assert result["account"] == "booking@memaix.se"
    assert gmail.appended == [] and len(shared.appended) == 1


def test_single_linked_account_reports_its_own_address(acl):
    gmail = _FakeSource()
    gmail.inbox_address = "jimmy@jimlov.se"  # what server._mail_backend sets on a lone linked source

    result = email_create_draft(acl, "jimmy", "jimlov", "k@example.com", "S", "b", account="jimmy@jimlov.se", _imap=gmail)

    assert result["account"] == "jimmy@jimlov.se"
    assert "From" not in _headers(gmail.appended)


def test_collaborator_may_pick_an_account_reader_may_not(acl, sources):
    _shared, gmail, mb = sources

    email_create_draft(acl, "carol", "jimlov", "k@example.com", "S", "b", account="jimmy@jimlov.se", _imap=mb)
    with pytest.raises(AccessDenied):
        email_create_draft(acl, "bob", "jimlov", "k@example.com", "S", "b", account="jimmy@jimlov.se", _imap=mb)

    assert len(gmail.appended) == 1
