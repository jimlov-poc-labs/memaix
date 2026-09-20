# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Gmail API mail adapter — the read path for a linked Google
account. Mirrors test_mail_microsoft_adapter.py: the adapter's job is to
make Gmail answer the exact imap_tools calls tools/email.py makes."""

from __future__ import annotations

import base64
from email.message import EmailMessage

import pytest

from memaix_gateway.connectors.adapters.mail_google import GmailAdapter


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(msg_id: str, subject: str, body: str, *, unread: bool = True, html: str | None = None):
    payload: dict = {
        "headers": [
            {"name": "Subject", "value": subject},
            {"name": "From", "value": "sender@example.com"},
            {"name": "To", "value": "me@example.com, other@example.com"},
            {"name": "Cc", "value": ""},
            {"name": "Date", "value": "Mon, 6 Jan 2025 10:00:00 +0000"},
        ],
    }
    if html is None:
        payload["mimeType"] = "text/plain"
        payload["body"] = {"data": _b64(body)}
    else:
        # The nested shape Gmail actually returns for an alternative body.
        payload["mimeType"] = "multipart/mixed"
        payload["parts"] = [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64(body)}},
                    {"mimeType": "text/html", "body": {"data": _b64(html)}},
                ],
            }
        ]
    return {
        "id": msg_id,
        "labelIds": ["INBOX"] + (["UNREAD"] if unread else []),
        "internalDate": "1736157600000",
        "payload": payload,
    }


class _FakeResponse:
    def __init__(self, data, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttp:
    def __init__(self, messages=None):
        self.requests = []
        self.messages = messages if messages is not None else [
            _message("m1", "Hello there", "hi there, this is the body")
        ]
        self.drafts_created = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        params = kwargs.get("params") or {}
        if method == "GET" and url.endswith("/messages"):
            data = self.messages
            if params.get("labelIds"):
                data = [m for m in data if params["labelIds"] in m["labelIds"]]
            if params.get("q"):
                data = [m for m in data if params["q"] in _body_of(m)]
            data = data[: params.get("maxResults", 20)]
            return _FakeResponse({"messages": [{"id": m["id"]} for m in data]})
        if method == "GET" and "/messages/" in url:
            msg_id = url.rsplit("/", 1)[-1]
            match = next((m for m in self.messages if m["id"] == msg_id), None)
            if match is None:
                return _FakeResponse({}, status_code=404)
            return _FakeResponse(match)
        if method == "POST" and url.endswith("/modify"):
            return _FakeResponse({})
        if method == "POST" and url.endswith("/drafts"):
            self.drafts_created.append(kwargs["json"])
            return _FakeResponse({"id": "draft1"})
        raise AssertionError(f"unexpected request: {method} {url}")


def _body_of(msg: dict) -> str:
    payload = msg["payload"]
    if "body" in payload:
        return base64.urlsafe_b64decode(
            payload["body"]["data"] + "=" * (-len(payload["body"]["data"]) % 4)
        ).decode()
    return base64.urlsafe_b64decode(
        payload["parts"][0]["parts"][0]["body"]["data"]
        + "=" * (-len(payload["parts"][0]["parts"][0]["body"]["data"]) % 4)
    ).decode()


# ------------------------------------------------------------------
# fetch — the three criteria strings tools/email.py ever sends
# ------------------------------------------------------------------


def test_fetch_all_lists_inbox_messages():
    http = _FakeHttp()
    adapter = GmailAdapter("tok", _http=http)
    (msg,) = adapter.fetch("ALL")
    assert msg.uid == "m1"
    assert msg.subject == "Hello there"
    assert msg.from_ == "sender@example.com"
    assert msg.text == "hi there, this is the body"
    assert msg.seen is False


def test_fetch_sends_the_bearer_token():
    http = _FakeHttp()
    GmailAdapter("tok-123", _http=http).fetch("ALL")
    _, _, kwargs = http.requests[0]
    assert kwargs["headers"]["Authorization"] == "Bearer tok-123"


def test_fetch_uid_reads_one_message_directly():
    """UID fetch must not page the list endpoint first."""
    http = _FakeHttp()
    (msg,) = GmailAdapter("tok", _http=http).fetch("UID m1")
    assert msg.uid == "m1"
    assert all(not url.endswith("/messages") for _, url, _ in http.requests)


def test_fetch_body_criteria_becomes_a_gmail_query():
    http = _FakeHttp(
        messages=[
            _message("m1", "First", "alpha content"),
            _message("m2", "Second", "beta content"),
        ]
    )
    (msg,) = GmailAdapter("tok", _http=http).fetch('BODY "beta"')
    assert msg.subject == "Second"


def test_search_term_is_unescaped_before_it_reaches_gmail():
    """tools/email.py escapes quotes for IMAP; Gmail must get the raw term."""
    http = _FakeHttp(messages=[_message("m1", "Quoted", 'say "hi" now')])
    GmailAdapter("tok", _http=http).fetch('BODY "say \\"hi\\" now"')
    params = http.requests[0][2]["params"]
    assert params["q"] == 'say "hi" now'


def test_limit_bounds_the_number_of_message_gets():
    """List returns ids only, so each message costs a request — limit has to
    actually bound them, not just the final list."""
    http = _FakeHttp(messages=[_message(f"m{i}", f"S{i}", "body") for i in range(5)])
    result = GmailAdapter("tok", _http=http).fetch("ALL", limit=2)
    assert len(result) == 2
    assert sum(1 for _, url, _ in http.requests if "/messages/" in url) == 2


def test_mark_seen_removes_the_unread_label():
    http = _FakeHttp()
    (msg,) = GmailAdapter("tok", _http=http).fetch("ALL", mark_seen=True)
    assert msg.seen is True
    modify = [r for r in http.requests if r[0] == "POST" and r[1].endswith("/modify")]
    assert modify[0][2]["json"] == {"removeLabelIds": ["UNREAD"]}


def test_mark_seen_skips_already_read_messages():
    http = _FakeHttp(messages=[_message("m1", "Read", "body", unread=False)])
    GmailAdapter("tok", _http=http).fetch("ALL", mark_seen=True)
    assert not [r for r in http.requests if r[1].endswith("/modify")]


# ------------------------------------------------------------------
# Message shape — what _msg_to_dict reads
# ------------------------------------------------------------------


def test_nested_multipart_body_is_found():
    http = _FakeHttp(messages=[_message("m1", "S", "plain text", html="<p>rich</p>")])
    (msg,) = GmailAdapter("tok", _http=http).fetch("ALL")
    assert msg.text == "plain text"
    assert msg.html == "<p>rich</p>"


def test_recipients_are_split_into_lists():
    http = _FakeHttp()
    (msg,) = GmailAdapter("tok", _http=http).fetch("ALL")
    assert msg.to == ["me@example.com", "other@example.com"]
    assert msg.cc == []


def test_date_header_is_preferred_over_internal_date():
    http = _FakeHttp()
    (msg,) = GmailAdapter("tok", _http=http).fetch("ALL")
    assert msg.date_str == "Mon, 6 Jan 2025 10:00:00 +0000"


# ------------------------------------------------------------------
# folder proxy
# ------------------------------------------------------------------


def test_folder_set_maps_names_to_system_labels():
    http = _FakeHttp()
    adapter = GmailAdapter("tok", _http=http)
    adapter.folder.set("Drafts")
    adapter.fetch("ALL")
    assert http.requests[0][2]["params"]["labelIds"] == "DRAFT"


def test_folder_defaults_to_inbox():
    http = _FakeHttp()
    GmailAdapter("tok", _http=http).fetch("ALL")
    assert http.requests[0][2]["params"]["labelIds"] == "INBOX"


# ------------------------------------------------------------------
# append — draft creation
# ------------------------------------------------------------------


def test_append_creates_a_draft_from_raw_mime():
    http = _FakeHttp()
    msg = EmailMessage()
    msg["Subject"] = "Draft subject"
    msg["To"] = "someone@example.com"
    msg.set_content("draft body")

    GmailAdapter("tok", _http=http).append(msg.as_bytes(), "\\Draft", folder="Drafts")

    (created,) = http.drafts_created
    raw = created["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "Subject: Draft subject" in decoded
    assert "draft body" in decoded


def test_append_preserves_in_reply_to_threading():
    """Unlike the Graph adapter, Gmail takes raw RFC-822, so the threading
    header survives — this is the gap that does NOT exist here."""
    http = _FakeHttp()
    msg = EmailMessage()
    msg["Subject"] = "Re: thread"
    msg["In-Reply-To"] = "<parent@example.com>"
    msg.set_content("reply")

    GmailAdapter("tok", _http=http).append(msg.as_bytes(), "\\Draft", folder="Drafts")

    raw = http.drafts_created[0]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "In-Reply-To: <parent@example.com>" in decoded


def test_logout_is_a_noop():
    GmailAdapter("tok", _http=_FakeHttp()).logout()


def test_http_error_propagates():
    http = _FakeHttp()
    with pytest.raises(RuntimeError):
        GmailAdapter("tok", _http=http).fetch("UID nope")
