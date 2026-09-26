# SPDX-License-Identifier: AGPL-3.0-or-later
"""email_attachments / email_attachment_get / email_export_pdf.

The receipt flow to the bookkeeping reads attachments (or the mail itself
as a PDF) through Memaix. Covered here: the Gmail and IMAP paths with faked
responses, the 10 MB limit, that nothing marks a message read, that a
project cannot reach an account it has not been given, and that PDF export
of text and HTML mail works without any network access.
"""

from __future__ import annotations

import base64
import socket
import time
import urllib.request
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet
from imap_tools import MailMessage

from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.connectors import registry as registry_mod
from memaix_gateway.connectors.adapters.mail_google import GmailAdapter
from memaix_gateway.notify.store import NotifyStore
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.rules.store import RulesStore
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.timeline.store import ActionsStore
from memaix_gateway.tools import email as t_email
from memaix_gateway.tools import mail_pdf

PDF_BYTES = b"%PDF-1.4 receipt"


class _AllowAll:
    def enforce(self, *a, **kw):
        return None

    def resource(self, project, key):
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# ------------------------------------------------------------------
# Gmail fakes
# ------------------------------------------------------------------


def _gmail_message(msg_id: str = "g1", *, pdf_size: int | None = None) -> dict:
    """A receipt as Gmail's format=full returns it: text + html alternative,
    a PDF behind an attachmentId, and a small inline logo carried inline."""
    return {
        "id": msg_id,
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Kvitto #42"},
                {"name": "From", "value": "shop@example.com"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Date", "value": "Mon, 6 Jan 2025 10:00:00 +0000"},
            ],
            "parts": [
                {
                    "partId": "0",
                    "mimeType": "multipart/alternative",
                    "filename": "",
                    "parts": [
                        {"partId": "0.0", "mimeType": "text/plain", "filename": "",
                         "body": {"size": 5, "data": _b64url(b"Tack!")}},
                        {"partId": "0.1", "mimeType": "text/html", "filename": "",
                         "body": {"size": 12, "data": _b64url(b"<p>Tack!</p>")}},
                    ],
                },
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "kvitto.pdf",
                    "headers": [{"name": "Content-Disposition", "value": 'attachment; filename="kvitto.pdf"'}],
                    "body": {"attachmentId": "ANGjdJ-volatile", "size": pdf_size or len(PDF_BYTES)},
                },
                {
                    "partId": "2",
                    "mimeType": "image/png",
                    "filename": "logo.png",
                    "headers": [
                        {"name": "Content-Disposition", "value": "inline"},
                        {"name": "Content-ID", "value": "<logo@shop>"},
                    ],
                    "body": {"size": 3, "data": _b64url(b"PNG")},
                },
            ],
        },
    }


class _Resp:
    def __init__(self, data, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.headers: dict = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeGmail:
    def __init__(self, message: dict | None = None):
        self.message = message or _gmail_message()
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        if method != "GET":
            raise AssertionError(f"read-only tool made a {method} request: {url}")
        if "/attachments/" in url:
            return _Resp({"size": len(PDF_BYTES), "data": _b64url(PDF_BYTES)})
        if url.endswith("/messages"):
            return _Resp({"messages": [{"id": self.message["id"]}]})
        if url.rsplit("/", 1)[-1] == self.message["id"]:
            return _Resp(self.message)
        return _Resp({}, status_code=404)

    def attachment_downloads(self) -> int:
        return sum(1 for _, url in self.requests if "/attachments/" in url)


# ------------------------------------------------------------------
# IMAP fakes: a real imap_tools.MailMessage parsed from MIME bytes
# ------------------------------------------------------------------


def _receipt_mime(*, html: str | None = "<p>Tack för köpet</p>", text: str = "Tack för köpet") -> bytes:
    msg = EmailMessage()
    msg["From"] = "shop@example.com"
    msg["To"] = "me@example.com"
    msg["Subject"] = "Kvitto #42"
    msg["Date"] = "Mon, 6 Jan 2025 10:00:00 +0000"
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    msg.add_attachment(PDF_BYTES, maintype="application", subtype="pdf", filename="kvitto.pdf")
    return msg.as_bytes()


class _FakeImap:
    """imap_tools.MailBox stand-in that records every fetch's mark_seen."""

    class _Folder:
        def set(self, name):
            pass

    def __init__(self, raw: bytes, uid: str = "7"):
        self.folder = self._Folder()
        self._raw = raw
        self._uid = uid
        self.mark_seen_calls: list[bool] = []

    def fetch(self, criteria="ALL", *, mark_seen=False, limit=None):
        self.mark_seen_calls.append(mark_seen)
        if criteria != f"UID {self._uid}":
            return []
        m = MailMessage.from_bytes(self._raw)
        m.uid = self._uid  # from_bytes has no server UID; the tools only read it back
        return [m]

    def logout(self):
        pass


# ------------------------------------------------------------------
# Gmail
# ------------------------------------------------------------------


def test_gmail_lists_attachments_without_downloading_them():
    http = _FakeGmail()
    rows = t_email.email_attachments(_AllowAll(), "u", "p", "g1", _imap=GmailAdapter("tok", _http=http))

    assert rows == [
        {"attachment_id": "0", "filename": "kvitto.pdf", "mimetype": "application/pdf",
         "size": len(PDF_BYTES), "disposition": "attachment", "content_id": ""},
        {"attachment_id": "1", "filename": "logo.png", "mimetype": "image/png",
         "size": 3, "disposition": "inline", "content_id": "logo@shop"},
    ]
    assert http.attachment_downloads() == 0


def test_gmail_attachment_get_downloads_via_attachments_api():
    http = _FakeGmail()
    result = t_email.email_attachment_get(
        _AllowAll(), "u", "p", "g1", "0", _imap=GmailAdapter("tok", _http=http),
    )

    assert result["filename"] == "kvitto.pdf"
    assert result["mimetype"] == "application/pdf"
    assert base64.b64decode(result["content_base64"]) == PDF_BYTES
    assert result["size"] == len(PDF_BYTES)
    # The id Gmail gave in this fetch — never a stale one from an earlier call.
    assert any(url.endswith("/messages/g1/attachments/ANGjdJ-volatile") for _, url in http.requests)


def test_gmail_inline_part_is_decoded_without_an_extra_request():
    http = _FakeGmail()
    result = t_email.email_attachment_get(
        _AllowAll(), "u", "p", "g1", "1", _imap=GmailAdapter("tok", _http=http),
    )
    assert base64.b64decode(result["content_base64"]) == b"PNG"
    assert http.attachment_downloads() == 0


def test_gmail_oversized_attachment_is_refused_before_download():
    http = _FakeGmail(_gmail_message(pdf_size=t_email.MAX_ATTACHMENT_BYTES + 1))
    with pytest.raises(ValueError, match="too large.*10 MB"):
        t_email.email_attachment_get(_AllowAll(), "u", "p", "g1", "0", _imap=GmailAdapter("tok", _http=http))
    assert http.attachment_downloads() == 0


def test_gmail_tools_never_mark_the_message_read():
    """_FakeGmail raises on anything but GET, so a POST .../modify (what
    marking as read is on Gmail) would fail these calls outright."""
    http = _FakeGmail()
    adapter = GmailAdapter("tok", _http=http)
    t_email.email_attachments(_AllowAll(), "u", "p", "g1", _imap=adapter)
    t_email.email_attachment_get(_AllowAll(), "u", "p", "g1", "0", _imap=adapter)
    t_email.email_export_pdf(_AllowAll(), "u", "p", "g1", _imap=adapter)
    assert {method for method, _ in http.requests} == {"GET"}


# ------------------------------------------------------------------
# IMAP
# ------------------------------------------------------------------


def test_imap_lists_and_gets_attachment_without_marking_seen():
    mb = _FakeImap(_receipt_mime())

    rows = t_email.email_attachments(_AllowAll(), "u", "p", "7", _imap=mb)
    assert [(r["attachment_id"], r["filename"], r["mimetype"], r["size"]) for r in rows] == [
        ("0", "kvitto.pdf", "application/pdf", len(PDF_BYTES)),
    ]
    assert rows[0]["disposition"] == "attachment"

    got = t_email.email_attachment_get(_AllowAll(), "u", "p", "7", "0", _imap=mb)
    assert base64.b64decode(got["content_base64"]) == PDF_BYTES
    assert got["filename"] == "kvitto.pdf"
    assert mb.mark_seen_calls and not any(mb.mark_seen_calls)


def test_imap_oversized_attachment_is_refused(monkeypatch):
    monkeypatch.setattr(t_email, "MAX_ATTACHMENT_BYTES", len(PDF_BYTES) - 1)
    with pytest.raises(ValueError, match="too large"):
        t_email.email_attachment_get(_AllowAll(), "u", "p", "7", "0", _imap=_FakeImap(_receipt_mime()))


@pytest.mark.parametrize("bad", ["1", "-1", "x", ""])
def test_unknown_attachment_id_is_not_found(bad):
    with pytest.raises(FileNotFoundError, match="attachment not found"):
        t_email.email_attachment_get(_AllowAll(), "u", "p", "7", bad, _imap=_FakeImap(_receipt_mime()))


def test_unknown_message_is_not_found():
    with pytest.raises(FileNotFoundError, match="message not found"):
        t_email.email_attachments(_AllowAll(), "u", "p", "999", _imap=_FakeImap(_receipt_mime()))


def test_source_without_attachment_support_says_so():
    class _GraphLike:
        uid = "m1"

    class _Mb:
        def fetch(self, criteria, *, mark_seen=False, limit=None):
            return [_GraphLike()]

    with pytest.raises(ValueError, match="not supported for this mail source"):
        t_email.email_attachments(_AllowAll(), "u", "p", "m1", _imap=_Mb())


# ------------------------------------------------------------------
# PDF export
# ------------------------------------------------------------------


@pytest.fixture()
def no_network(monkeypatch):
    """Any attempt to open a connection fails the test."""

    def _refuse(*a, **kw):
        raise AssertionError("network access during PDF rendering")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(urllib.request, "urlopen", _refuse)


@pytest.fixture()
def rendered_text(monkeypatch):
    """Capture the strings written into the PDF (the output is compressed)."""
    from fpdf import FPDF

    seen: list[str] = []
    original = FPDF.multi_cell

    def spy(self, w, h=None, text="", *a, **kw):
        seen.append(text)
        return original(self, w, h, text, *a, **kw)

    monkeypatch.setattr(FPDF, "multi_cell", spy)
    return seen


_RECEIPT_HTML = """<html><head><style>td{color:red}</style>
<link rel="stylesheet" href="https://tracker.example/s.css"><script>alert(1)</script></head>
<body><img src="https://tracker.example/pixel.gif" alt="Butikens logga">
<table><tr><td><table>
<tr><th>Artikel</th><th>Pris</th></tr>
<tr><td>Kaffe</td><td>35,00&nbsp;kr</td></tr>
<tr><td>Moms 12 %</td><td>3,75 kr</td></tr>
</table></td></tr></table>
<p>Totalt: 35,00 kr &ndash; tack!</p></body></html>"""


def test_export_pdf_of_html_mail(no_network, rendered_text):
    mb = _FakeImap(_receipt_mime(html=_RECEIPT_HTML))
    result = t_email.email_export_pdf(_AllowAll(), "u", "p", "7", _imap=mb)

    pdf = base64.b64decode(result["content_base64"])
    assert pdf.startswith(b"%PDF-")
    assert result["mimetype"] == "application/pdf"
    assert result["rendered_from"] == "html"
    assert result["size"] == len(pdf)
    assert result["filename"] == "Kvitto 42.pdf"
    written = "\n".join(rendered_text)
    for expected in ("From: shop@example.com", "Subject: Kvitto #42", "Attachments: kvitto.pdf",
                     "Kaffe | 35,00 kr", "Moms 12 % | 3,75 kr", "[Butikens logga]"):
        assert expected in written
    for leaked in ("color:red", "alert(1)", "tracker.example"):
        assert leaked not in written
    assert not any(mb.mark_seen_calls)


def test_export_pdf_of_text_only_mail(no_network, rendered_text):
    mb = _FakeImap(_receipt_mime(html=None, text="Kvitto\nSumma: 100 kr"))
    result = t_email.email_export_pdf(_AllowAll(), "u", "p", "7", _imap=mb)

    assert base64.b64decode(result["content_base64"]).startswith(b"%PDF-")
    assert result["rendered_from"] == "text"
    assert "Kvitto\nSumma: 100 kr" in "\n".join(rendered_text)


def test_export_pdf_of_gmail_message(no_network):
    result = t_email.email_export_pdf(
        _AllowAll(), "u", "p", "g1", _imap=GmailAdapter("tok", _http=_FakeGmail()),
    )
    assert base64.b64decode(result["content_base64"]).startswith(b"%PDF-")
    assert result["rendered_from"] == "html"


def test_without_unicode_font_characters_are_folded_not_fatal(monkeypatch, rendered_text):
    monkeypatch.setattr(mail_pdf, "unicode_font_path", lambda: None)
    pdf, rendered_from = mail_pdf.render_message_pdf(
        from_="a@example.com", to=[], cc=[], date="", subject="Kvitto – “Café”",
        html="", text="Summa: 12 € — tack 🙂", attachment_names=[],
    )
    assert pdf.startswith(b"%PDF-") and rendered_from == "text"
    written = "\n".join(rendered_text)
    assert "Summa: 12 EUR - tack ?" in written
    assert 'Subject: Kvitto - "Café"' in written


def test_with_unicode_font_characters_are_kept(rendered_text):
    if mail_pdf.unicode_font_path() is None:
        pytest.skip("no DejaVu Sans on this machine (the Docker image installs fonts-dejavu-core)")
    pdf, _ = mail_pdf.render_message_pdf(
        from_="", to=[], cc=[], date="", subject="", html="", text="Summa: 12 € — tack",
        attachment_names=[],
    )
    assert pdf.startswith(b"%PDF-")
    assert "Summa: 12 € — tack" in rendered_text


def test_empty_message_still_renders():
    pdf, rendered_from = mail_pdf.render_message_pdf(
        from_="", to=[], cc=[], date="", subject="", html="  ", text="", attachment_names=[],
    )
    assert pdf.startswith(b"%PDF-") and rendered_from == "empty"


@pytest.mark.parametrize("subject, expected", [
    ("Kvitto #42", "Kvitto 42.pdf"),
    ("../../etc/passwd", "etcpasswd.pdf"),
    ("", "email.pdf"),
    ("Faktura: Å/Ä/Ö", "Faktura ÅÄÖ.pdf"),
])
def test_pdf_filename_is_safe(subject, expected):
    assert mail_pdf.pdf_filename(subject) == expected


# ------------------------------------------------------------------
# Project scoping, end to end through the MCP tools
# ------------------------------------------------------------------


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """`proj` has only a linked Gmail account; `other` has its own IMAP
    mailbox. The Gmail account is shared with `proj` alone."""
    token_store = TokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key())
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True)
    acl = Acl(
        users={"alice": {"grants": {"proj": "owner", "other": "owner", "bare": "owner"}}},
        projects={
            "proj": {"vault": str(vault)},
            "other": {"vault": str(vault), "mailbox": {"host": "imap.example.com", "user": "other@example.com"}},
            "bare": {"vault": str(vault)},
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

    token_store.store("alice", "google", "a@gmail.com", {
        "access_token": "tok", "refresh_token": "r1", "expires_at": time.time() + 3600,
    })
    token_store.set_scopes("alice", "google", "a@gmail.com", "mail", ["proj"])

    gmail = _FakeGmail()
    monkeypatch.setattr("requests.Session.request", lambda self, method, url, **kw: gmail.request(method, url, **kw))
    monkeypatch.setattr("requests.request", gmail.request)
    imap = _FakeImap(_receipt_mime())
    monkeypatch.setattr(t_email, "_make_mailbox", lambda acl, project: imap)
    return gmail, imap


def test_owning_project_gets_the_gmail_attachment(wired):
    gmail, _ = wired
    rows = server.email_attachments("proj", "g1")
    assert [r["filename"] for r in rows] == ["kvitto.pdf", "logo.png"]
    got = server.email_attachment_get("proj", "g1", "0")
    assert base64.b64decode(got["content_base64"]) == PDF_BYTES
    assert server.email_export_pdf("proj", "g1")["mimetype"] == "application/pdf"


def test_project_without_the_account_cannot_reach_it(wired):
    """`bare` has no mail at all: the unshared Gmail account is not a source."""
    gmail, _ = wired
    with pytest.raises(ValueError, match="no mail configured"):
        server.email_attachment_get("bare", "g1", "0")
    with pytest.raises(ValueError, match="no mail configured"):
        server.email_export_pdf("bare", "g1")
    assert gmail.attachment_downloads() == 0


def test_labelled_id_from_another_projects_account_is_not_found(wired):
    """`other` has mail of its own, just not the Gmail account: an id
    naming that account finds nothing instead of reaching into it."""
    gmail, _ = wired
    with pytest.raises(FileNotFoundError, match="message not found"):
        server.email_attachment_get("other", "google_mail:a@gmail.com|g1", "0")
    with pytest.raises(FileNotFoundError, match="message not found"):
        server.email_attachments("other", "google_mail:a@gmail.com|g1")
    assert gmail.requests == []


def test_other_project_still_reads_its_own_imap_attachment(wired):
    _, imap = wired
    got = server.email_attachment_get("other", "7", "0")
    assert base64.b64decode(got["content_base64"]) == PDF_BYTES
    assert not any(imap.mark_seen_calls)
