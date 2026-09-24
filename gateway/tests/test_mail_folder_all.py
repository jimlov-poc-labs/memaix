# SPDX-License-Identifier: AGPL-3.0-or-later
"""email_search(folder="ALL") over a real IMAP source, alone and merged with
a Gmail source (live bug 2026-09-24).

#125 made folder="ALL" mean "the whole mailbox" on Gmail and Graph. A real
IMAP server has no folder called ALL, so `SELECT "ALL"` came back
`NO [b'SELECT failed. No such mailbox.']`, and because MultiMailBackend
fanned the folder out to every source, that one refusal failed the whole
search, the Gmail hits included.

The IMAP fake below behaves like imap_tools.MailBox against such a server:
folder.set() of an unknown name raises, folder.list() returns FolderInfo
rows with LIST flags, fetch() searches the currently selected folder only
and UIDs are per folder.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from memaix_gateway.acl import Acl
from memaix_gateway.connectors.adapters.mail_google import GmailAdapter
from memaix_gateway.connectors.adapters.mail_imap_all import AllFoldersMailBox, search_folders, wrap
from memaix_gateway.connectors.adapters.mail_multi import MultiMailBackend
from memaix_gateway.tools import email as t_email


@dataclass
class _FolderInfo:  # imap_tools.folder.FolderInfo's shape
    name: str
    delim: str
    flags: tuple


class _ImapMsg:
    def __init__(self, uid, subject, date_str, text=""):
        self.uid = uid
        self.subject = subject
        self.from_ = "sender@example.com"
        self.to = ("booking@memaix.se",)
        self.cc = ()
        self.date_str = date_str
        self.flags = ("\\Seen",)
        self.text = text or subject
        self.html = ""


class _SelectError(Exception):
    """imap_tools.errors.MailboxFolderSelectError's message, as seen live."""


class _ImapFolders:
    def __init__(self, server: "_FakeImapServer") -> None:
        self._server = server

    def set(self, name, readonly=False):
        if name not in self._server.mail or name in self._server.broken:
            raise _SelectError(
                'Response status "OK" expected, but "NO" received. Data: [b\'SELECT failed. No such mailbox.\']'
            )
        self._server.selected = name
        self._server.selects.append(name)

    def list(self, folder="", search_args="*", subscribed_only=False):
        return list(self._server.folder_infos)


class _FakeImapServer:
    """Stand-in for a logged-in imap_tools.MailBox (Purelymail-like)."""

    def __init__(self, mail: dict[str, list[_ImapMsg]], flags: dict[str, tuple] | None = None, broken=()):
        self.mail = mail
        self.broken = set(broken)
        flags = flags or {}
        self.folder_infos = [_FolderInfo(n, "/", flags.get(n, ("\\HasNoChildren",))) for n in mail]
        self.folder_infos += [_FolderInfo(n, "/", f) for n, f in flags.items() if n not in mail]
        self.selected = "INBOX"
        self.selects: list[str] = []
        self.folder = _ImapFolders(self)
        self.logged_out = False

    def fetch(self, criteria="ALL", charset="US-ASCII", *, limit=None, mark_seen=True, reverse=False, **kw):
        msgs = self.mail[self.selected]
        if criteria.startswith("UID "):
            msgs = [m for m in msgs if m.uid == criteria[4:]]
        elif criteria.startswith("TEXT "):
            needle = criteria.split('"')[1]
            msgs = [m for m in msgs if needle in m.text]
        msgs = list(reversed(msgs)) if reverse else list(msgs)
        msgs = msgs[:limit] if limit else msgs
        # fresh objects per round trip, like a real server
        return [_ImapMsg(m.uid, m.subject, m.date_str, m.text) for m in msgs]

    def logout(self):
        self.logged_out = True


def _purelymail():
    """booking@memaix.se's layout: INBOX, an Archive, Sent, and trash/junk."""
    return _FakeImapServer(
        {
            "INBOX": [_ImapMsg("1", "Inbox mail", "Tue, 22 Sep 2026 09:00:00 +0200")],
            "Archive": [_ImapMsg("1", "Archived mail", "Mon, 07 Sep 2026 09:00:00 +0200")],
            "Sent": [_ImapMsg("5", "Sent mail", "Wed, 16 Sep 2026 09:00:00 +0200")],
            "Trash": [_ImapMsg("9", "Deleted mail", "Thu, 17 Sep 2026 09:00:00 +0200")],
            "Junk": [_ImapMsg("3", "Spam mail", "Fri, 18 Sep 2026 09:00:00 +0200")],
        },
        flags={"Sent": ("\\Sent",), "Trash": ("\\Trash",), "Junk": ("\\Junk",)},
    )


# --- a minimal Gmail API behind the real GmailAdapter ----------------------


class _GmailResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.headers: dict = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _GmailHttp:
    def __init__(self):
        self.msg = {
            "id": "g1",
            "labelIds": ["CATEGORY_UPDATES"],  # archived: not in INBOX
            "internalDate": "1758700000000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Gmail archived mail"},
                    {"name": "From", "value": "noreply@example.com"},
                    {"name": "Date", "value": "Wed, 23 Sep 2026 10:00:00 +0200"},
                ],
                "body": {"data": ""},
            },
        }
        self.list_params: list[dict] = []

    def request(self, method, url, **kwargs):
        params = kwargs.get("params") or {}
        if url.endswith("/messages"):
            self.list_params.append(params)
            label = params.get("labelIds")
            hits = [self.msg] if not label or label in self.msg["labelIds"] else []
            return _GmailResp({"messages": [{"id": m["id"]} for m in hits]})
        return _GmailResp(self.msg)


def _search(mb, **kw):
    acl = Acl(users={"alice": {"grants": {"jimlov": "owner"}}}, projects={"jimlov": {}})
    return t_email.email_search(acl, "alice", "jimlov", _imap=mb, **kw)


def _rows(result):
    return [r for r in result if "id" in r]


def _warnings(result):
    return [r for r in result if "warning" in r]


# --- the live bug ------------------------------------------------------------


def test_all_over_imap_plus_gmail_no_longer_fails_the_whole_search():
    """The exact live shape: an IMAP source that cannot SELECT "ALL" merged
    with a Gmail source. The Gmail hits come back, and the IMAP failure is
    reported, not swallowed."""
    imap = _purelymail()  # bare, as an adapter that doesn't know "ALL" would be
    mb = MultiMailBackend([("imap:jimlov", imap), ("google_mail:jimmy@jimlov.se", GmailAdapter("t", _http=_GmailHttp()))])

    result = _search(mb, folder="ALL", since="2026-09-01", until="2026-10-01")

    assert [r["subject"] for r in _rows(result)] == ["Gmail archived mail"]
    (warning,) = _warnings(result)
    assert warning is result[-1]
    (err,) = warning["source_errors"]
    assert err["source"] == "imap:jimlov"
    assert "No such mailbox" in err["error"]


def test_all_over_wrapped_imap_plus_gmail_searches_every_imap_folder():
    """With the IMAP source built the way production builds it (wrapped),
    "ALL" means every folder there too: archive and sent included, trash
    and junk left out as on Gmail, merged newest first, no warning."""
    gmail_http = _GmailHttp()
    mb = MultiMailBackend([
        ("imap:jimlov", wrap(_purelymail())),
        ("google_mail:jimmy@jimlov.se", GmailAdapter("t", _http=gmail_http)),
    ])

    result = _search(mb, folder="ALL", since="2026-09-01", until="2026-10-01")

    assert [r["subject"] for r in result] == ["Gmail archived mail", "Inbox mail", "Sent mail", "Archived mail"]
    assert not _warnings(result)
    assert "labelIds" not in gmail_http.list_params[0]


def test_read_after_all_search_routes_to_the_right_imap_folder():
    """UIDs are per folder: INBOX and Archive both have a UID 1. The id
    email_search hands out must bring email_read back to the right one."""
    acl = Acl(users={"alice": {"grants": {"jimlov": "owner"}}}, projects={"jimlov": {}})
    imap = _purelymail()
    mb = MultiMailBackend([("imap:jimlov", wrap(imap)), ("google_mail:jimmy@jimlov.se", GmailAdapter("t", _http=_GmailHttp()))])
    hits = {r["subject"]: r["id"] for r in _rows(_search(mb, folder="ALL"))}
    assert hits["Archived mail"] == "imap:jimlov|Archive|1"

    fresh = MultiMailBackend([("imap:jimlov", wrap(imap)), ("google_mail:jimmy@jimlov.se", GmailAdapter("t", _http=_GmailHttp()))])
    read = t_email.email_read(acl, "alice", "jimlov", hits["Archived mail"], _imap=fresh)

    assert read["subject"] == "Archived mail"
    assert read["id"] == "imap:jimlov|Archive|1"


# --- AllFoldersMailBox on its own (single IMAP source) -----------------------


def test_single_imap_source_all_searches_folders_and_skips_trash_and_junk():
    imap = _purelymail()
    result = _search(wrap(imap), folder="ALL")

    assert [r["subject"] for r in result] == ["Inbox mail", "Sent mail", "Archived mail"]
    assert [r["id"] for r in result] == ["INBOX|1", "Sent|5", "Archive|1"]
    assert "Trash" not in imap.selects and "Junk" not in imap.selects


def test_other_folders_pass_straight_through():
    imap = _purelymail()
    result = _search(wrap(imap), folder="Archive")
    assert [(r["id"], r["subject"]) for r in result] == [("1", "Archived mail")]


def test_limit_keeps_the_newest_across_folders():
    result = _search(wrap(_purelymail()), folder="ALL", limit=2)
    assert [r["subject"] for r in result] == ["Inbox mail", "Sent mail"]


def test_a_special_use_all_folder_is_searched_alone():
    """A server with a \\All folder (Gmail over IMAP) already has every
    message there; searching the rest too would list each one twice."""
    folders = [
        _FolderInfo("INBOX", "/", ()),
        _FolderInfo("[Gmail]", "/", ("\\Noselect", "\\HasChildren")),
        _FolderInfo("[Gmail]/All Mail", "/", ("\\All", "\\HasNoChildren")),
        _FolderInfo("[Gmail]/Spam", "/", ("\\Junk",)),
    ]
    assert search_folders(folders) == ["[Gmail]/All Mail"]


def test_folders_without_special_use_flags_are_skipped_by_name():
    folders = [
        _FolderInfo("INBOX", ".", ()),
        _FolderInfo("INBOX.Archive", ".", ()),
        _FolderInfo("INBOX.Trash", ".", ()),
        _FolderInfo("Spam", ".", ()),
        _FolderInfo("Papperskorgen", ".", ()),
        _FolderInfo("Shared", ".", ("\\Noselect",)),
    ]
    assert search_folders(folders) == ["INBOX", "INBOX.Archive"]


def test_one_failing_folder_is_reported_not_fatal():
    imap = _purelymail()
    imap.broken.add("Archive")
    result = _search(wrap(imap), folder="ALL")

    assert [r["subject"] for r in _rows(result)] == ["Inbox mail", "Sent mail"]
    (warning,) = _warnings(result)
    assert [e["source"] for e in warning["source_errors"]] == ["folder:Archive"]


def test_folder_errors_inside_a_merged_source_carry_the_source_label():
    imap = _purelymail()
    imap.broken.add("Archive")
    mb = MultiMailBackend([("imap:jimlov", wrap(imap)), ("google_mail:jimmy@jimlov.se", GmailAdapter("t", _http=_GmailHttp()))])

    (warning,) = _warnings(_search(mb, folder="ALL"))

    assert [e["source"] for e in warning["source_errors"]] == ["imap:jimlov folder:Archive"]


def test_every_folder_failing_raises():
    imap = _purelymail()
    imap.broken.update(imap.mail)
    with pytest.raises(_SelectError):
        _search(wrap(imap), folder="ALL")


def test_every_source_failing_raises():
    """Nothing searched is a failure, not an empty partial result."""
    mb = MultiMailBackend([("imap:a", _purelymail()), ("imap:b", _purelymail())])
    with pytest.raises(_SelectError):
        _search(mb, folder="ALL")


def test_folder_id_must_name_a_listed_folder_and_a_numeric_uid():
    mb = wrap(_purelymail())
    with pytest.raises(FileNotFoundError):
        mb.fetch("UID Nowhere|1")
    with pytest.raises(ValueError):
        mb.fetch("UID Archive|1:*")


def test_plain_uid_read_is_unchanged():
    imap = _purelymail()
    (msg,) = wrap(imap).fetch("UID 1", mark_seen=False)
    assert msg.subject == "Inbox mail"
    assert msg.uid == "1"


# --- production wiring --------------------------------------------------------


def test_shared_and_per_user_imap_mailboxes_are_built_wrapped(monkeypatch):
    built = []

    class _MailBox(_FakeImapServer):
        def __init__(self, host, port=993):
            super().__init__({"INBOX": []})
            built.append(self)

        def login(self, user, password):
            return self

    monkeypatch.setitem(sys.modules, "imap_tools", types.SimpleNamespace(MailBox=_MailBox))
    monkeypatch.setattr(t_email.config, "secret", lambda ref: "pw")
    acl = Acl(users={}, projects={"jimlov": {"mailbox": {"host": "imap.purelymail.com", "user": "booking@memaix.se"}}})

    from memaix_gateway.connectors.adapters.mail_imap_user import build_mailbox

    shared = t_email._make_mailbox(acl, "jimlov")
    personal = build_mailbox({"host": "imap.example.com", "user": "a", "password": "pw"})

    assert isinstance(shared, AllFoldersMailBox) and isinstance(personal, AllFoldersMailBox)
    shared.logout()
    assert built[0].logged_out  # everything else passes through
    assert wrap(shared) is shared
