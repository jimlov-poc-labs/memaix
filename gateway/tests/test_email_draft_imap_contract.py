# SPDX-License-Identifier: AGPL-3.0-or-later
"""email_create_draft against the real imap_tools MailBox code.

Every other email test injects a hand-written fake whose `append` signature
was copied from our own duck-type docstring — which was wrong. The fakes
agreed with the caller, the real `imap_tools.MailBox.append(message,
folder='INBOX', dt=None, flag_set=None)` did not, and every draft in
production failed with "got multiple values for argument 'folder'".

Here the mailbox is a genuine `imap_tools.BaseMailBox` subclass; only the
imaplib client underneath it is fake. `append`, `folder.list()` and `fetch`
therefore run imap_tools' own code, so a signature drift fails these tests
instead of production.
"""

from __future__ import annotations

import inspect
from email import message_from_bytes
from unittest import mock

import pytest
from imap_tools import BaseMailBox, MailBox

from memaix_gateway.acl import Acl
from memaix_gateway.connectors.adapters.mail_multi import MultiMailBackend
from memaix_gateway.tools.email import email_create_draft, resolve_drafts_folder

# Gmail/Workspace LIST response for a Swedish-language account: the drafts
# folder is localised, only the SPECIAL-USE flag says which one it is.
_GMAIL_SV_LIST = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/Alla mail"',
    b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Utkast"',
    b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Skickat"',
]

_ORIGINAL = (
    b"Message-ID: <orig-123@mail.example.com>\r\n"
    b"References: <root-1@mail.example.com>\r\n"
    b" <mid-2@mail.example.com>\r\n"
    b"From: kund@example.com\r\n"
    b"To: jimmy@jimlov.se\r\n"
    b"Subject: Offert\r\n"
    b"\r\n"
    b"Hej!\r\n"
)


class _FakeImaplib:
    """Just enough of imaplib.IMAP4 for imap_tools' append/list/fetch."""

    def __init__(self, list_lines, messages=None):
        self._list_lines = list_lines
        self._messages = messages or {}
        self.appended: list[tuple] = []

    # BaseMailBox.append -> client.append(mailbox, flags, date_time, message)
    def append(self, mailbox, flags, date_time, message):
        self.appended.append((mailbox, flags, date_time, message))
        return "OK", [b"[APPENDUID 1 1] APPEND completed"]

    # MailBoxFolderManager.list -> _simple_command + _untagged_response
    def _simple_command(self, command, *args):
        return "OK", None

    def _untagged_response(self, typ, data, command):
        return "OK", list(self._list_lines)

    # BaseMailBox.fetch -> uid('SEARCH', ...) then uid('fetch', uid, parts)
    def uid(self, command, *args):
        if command.upper() == "SEARCH":
            wanted = args[-1].decode().split()[-1]
            return "OK", [wanted.encode() if wanted in self._messages else b""]
        uid = args[0]
        raw = self._messages[uid]
        head = f"1 (UID {uid} FLAGS (\\Seen) RFC822.SIZE {len(raw)} BODY[] {{{len(raw)}}}".encode()
        return "OK", [(head, raw), b")"]


class _WireMailBox(BaseMailBox):
    """A real imap_tools mailbox over a fake imaplib client."""

    def __init__(self, client):
        self._fake = client
        super().__init__()

    def _get_mailbox_client(self):
        return self._fake


@pytest.fixture()
def acl():
    return Acl(
        users={"jimmy": {"grants": {"jimlov": "owner"}}},
        projects={"jimlov": {"mailbox": {"host": "imap.gmail.com", "user": "jimmy@jimlov.se"}}},
    )


def test_wire_mailbox_has_the_real_append_signature():
    """Guard the guard: if imap_tools changes append, this is the test that
    should tell us, not a failed draft in production."""
    assert inspect.signature(_WireMailBox.append) == inspect.signature(MailBox.append)
    assert list(inspect.signature(MailBox.append).parameters)[:3] == ["self", "message", "folder"]


def test_draft_is_appended_to_special_use_drafts_folder_with_draft_flag(acl):
    client = _FakeImaplib(_GMAIL_SV_LIST)

    result = email_create_draft(
        acl, "jimmy", "jimlov", "kund@example.com", "Offert", "Hej", _imap=_WireMailBox(client),
    )

    assert result == {"status": "draft_created", "subject": "Offert"}
    assert len(client.appended) == 1
    mailbox, flags, _dt, message = client.appended[0]
    assert mailbox == b'"[Gmail]/Utkast"'  # imap_tools encodes + quotes the name
    assert flags == "(\\Draft)"
    parsed = message_from_bytes(message)
    assert parsed["To"] == "kund@example.com"
    assert parsed["From"] == "jimmy@jimlov.se"


def test_drafts_folder_falls_back_to_known_name_without_special_use():
    client = _FakeImaplib([
        b'(\\HasNoChildren) "." "INBOX"',
        b'(\\HasNoChildren) "." "INBOX.Drafts"',
    ])
    assert resolve_drafts_folder(_WireMailBox(client)) == "INBOX.Drafts"


def test_reply_by_memaix_id_threads_on_the_originals_message_id(acl):
    client = _FakeImaplib(_GMAIL_SV_LIST, messages={"42": _ORIGINAL})

    email_create_draft(
        acl, "jimmy", "jimlov", "kund@example.com", "Re: Offert", "Svar",
        in_reply_to="42", _imap=_WireMailBox(client),
    )

    parsed = message_from_bytes(client.appended[0][3])
    assert parsed["In-Reply-To"] == "<orig-123@mail.example.com>"
    # Long headers are folded on serialisation; compare the unfolded value.
    assert " ".join(parsed["References"].split()) == (
        "<root-1@mail.example.com> <mid-2@mail.example.com> <orig-123@mail.example.com>"
    )


def test_reply_by_message_id_is_used_verbatim(acl):
    client = _FakeImaplib(_GMAIL_SV_LIST)

    email_create_draft(
        acl, "jimmy", "jimlov", "kund@example.com", "Re: Offert", "Svar",
        in_reply_to="orig-123@mail.example.com", _imap=_WireMailBox(client),
    )

    parsed = message_from_bytes(client.appended[0][3])
    assert parsed["In-Reply-To"] == "<orig-123@mail.example.com>"
    assert parsed["References"] == "<orig-123@mail.example.com>"


def test_reply_to_unknown_memaix_id_fails_without_saving_a_draft(acl):
    client = _FakeImaplib(_GMAIL_SV_LIST)

    with pytest.raises(FileNotFoundError, match="in_reply_to"):
        email_create_draft(
            acl, "jimmy", "jimlov", "kund@example.com", "Re: x", "Svar",
            in_reply_to="999", _imap=_WireMailBox(client),
        )
    assert client.appended == []


def test_multi_source_resolves_labeled_id_and_first_sources_drafts_folder(acl):
    """The production setup that failed: several mail sources, ids shaped
    "label|uid", draft landing on the first (IMAP) source."""
    imap_client = _FakeImaplib(_GMAIL_SV_LIST, messages={"42": _ORIGINAL})
    other = mock.create_autospec(MailBox, instance=True)
    backend = MultiMailBackend([
        ("imap:jimmy@jimlov.se", _WireMailBox(imap_client)),
        ("google_mail:jimmy@jimlov.se", other),
    ])

    email_create_draft(
        acl, "jimmy", "jimlov", "kund@example.com", "Re: Offert", "Svar",
        in_reply_to="imap:jimmy@jimlov.se|42", _imap=backend,
    )

    mailbox, flags, _dt, message = imap_client.appended[0]
    assert mailbox == b'"[Gmail]/Utkast"'
    assert flags == "(\\Draft)"
    assert message_from_bytes(message)["In-Reply-To"] == "<orig-123@mail.example.com>"
    other.append.assert_not_called()


def test_autospecced_mailbox_accepts_the_call(acl):
    """Second line of defence, independent of the wire fake: autospec binds
    the call against MailBox.append's real signature and raises TypeError
    on the positional-flags bug."""
    mb = mock.create_autospec(MailBox, instance=True)
    mb.folder = mock.Mock()
    mb.folder.list.return_value = []

    email_create_draft(acl, "jimmy", "jimlov", "kund@example.com", "Offert", "Hej", _imap=mb)

    mb.append.assert_called_once()
    _, kwargs = mb.append.call_args
    assert kwargs["folder"] == "Drafts"
    assert kwargs["flag_set"] == ("\\Draft",)
