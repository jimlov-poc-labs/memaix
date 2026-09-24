# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Microsoft Graph mail adapter —
FEATURE-CONNECTOR-FRAMEWORK.md §7 step 6 (first external connector proof)."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from memaix_gateway.connectors.adapters.mail_microsoft import GraphMailAdapter


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
    def __init__(self):
        self.requests = []
        self.inbox = [
            {
                "id": "m1", "subject": "Hello there", "isRead": False,
                "from": {"emailAddress": {"address": "sender@example.com"}},
                "toRecipients": [{"emailAddress": {"address": "me@example.com"}}],
                "ccRecipients": [],
                "receivedDateTime": "2025-01-06T10:00:00Z",
                "body": {"contentType": "text", "content": "hi there, this is the body"},
            },
        ]
        self.drafts_created = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if method == "GET" and url.endswith("/me/mailFolders/inbox/messages"):
            params = kwargs.get("params") or {}
            data = self.inbox
            if "$search" in params:
                needle = params["$search"].strip('"')
                data = [m for m in self.inbox if needle in m["body"]["content"]]
            return _FakeResponse({"value": data})
        if method == "GET" and url.endswith("/me/mailFolders/drafts/messages"):
            return _FakeResponse({"value": []})
        if method == "GET" and "/me/messages/" in url:
            msg_id = url.rsplit("/", 1)[-1]
            match = next((m for m in self.inbox if m["id"] == msg_id), None)
            if match is None:
                return _FakeResponse({"error": "not found"}, status_code=404)
            return _FakeResponse(match)
        if method == "PATCH":
            msg_id = url.rsplit("/", 1)[-1]
            for m in self.inbox:
                if m["id"] == msg_id:
                    m["isRead"] = True
            return _FakeResponse({})
        if method == "POST" and url.endswith("/me/mailFolders/drafts/messages"):
            self.drafts_created.append(kwargs.get("json"))
            return _FakeResponse({"id": "draft1"})
        return _FakeResponse({}, status_code=404)


@pytest.fixture()
def http():
    return _FakeHttp()


@pytest.fixture()
def adapter(http):
    return GraphMailAdapter("fake-token", _http=http)


def test_fetch_all_defaults_to_inbox(adapter):
    msgs = adapter.fetch("ALL")
    assert len(msgs) == 1
    assert msgs[0].subject == "Hello there"
    assert msgs[0].uid == "m1"
    assert msgs[0].from_ == "sender@example.com"
    assert msgs[0].to == ["me@example.com"]
    assert msgs[0].cc == []
    assert msgs[0].seen is False
    assert msgs[0].text == "hi there, this is the body"
    assert msgs[0].html == ""


def test_fetch_by_uid(adapter):
    msgs = adapter.fetch("UID m1")
    assert len(msgs) == 1
    assert msgs[0].uid == "m1"


def test_fetch_body_search(adapter):
    msgs = adapter.fetch('BODY "this is the body"')
    assert len(msgs) == 1
    # ConsistencyLevel header required by Graph for $search
    method, url, kwargs = [r for r in adapter._http.requests if "$search" in (r[2].get("params") or {})][0]
    assert kwargs["headers"]["ConsistencyLevel"] == "eventual"


def test_fetch_body_search_unescapes_imap_quoting(adapter):
    # tools/email.py's _imap_quote would have escaped a literal backslash/quote;
    # the adapter must undo that before handing the term to Graph.
    from memaix_gateway.tools.email import _imap_quote

    escaped = _imap_quote('this is the body')
    msgs = adapter.fetch(f'BODY "{escaped}"')
    assert len(msgs) == 1


def test_fetch_mark_seen_patches_unread_messages(adapter, http):
    adapter.fetch("ALL", mark_seen=True)
    patches = [r for r in http.requests if r[0] == "PATCH"]
    assert len(patches) == 1
    assert http.inbox[0]["isRead"] is True


def test_fetch_mark_seen_skips_already_read(adapter, http):
    http.inbox[0]["isRead"] = True
    adapter.fetch("ALL", mark_seen=True)
    assert [r for r in http.requests if r[0] == "PATCH"] == []


def test_folder_set_maps_to_drafts(adapter):
    adapter.folder.set("Drafts")
    adapter.fetch("ALL")
    assert any(url.endswith("/me/mailFolders/drafts/messages") for _, url, _ in adapter._http.requests)


def test_append_translates_mime_message_to_graph_draft(adapter, http):
    msg = EmailMessage()
    msg["To"] = "to@example.com"
    msg["Cc"] = "cc@example.com"
    msg["Subject"] = "Draft subject"
    msg.set_content("draft body text")

    adapter.append(msg.as_bytes(), folder="Drafts", flag_set=("\\Draft",))

    assert len(http.drafts_created) == 1
    draft = http.drafts_created[0]
    assert draft["subject"] == "Draft subject"
    assert draft["body"] == {"contentType": "Text", "content": "draft body text\n"}
    assert draft["toRecipients"] == [{"emailAddress": {"address": "to@example.com"}}]
    assert draft["ccRecipients"] == [{"emailAddress": {"address": "cc@example.com"}}]


def test_logout_is_a_noop(adapter):
    assert adapter.logout() is None


def test_fetched_message_exposes_internet_message_id_as_header(adapter, http):
    """email_create_draft's in_reply_to lookup reads Message-ID off the
    fetched original; Graph returns it as internetMessageId."""
    http.inbox[0]["internetMessageId"] = "<m1@example.com>"

    fetched = adapter.fetch("UID m1")[0]

    assert fetched.headers == {"message-id": ("<m1@example.com>",)}
