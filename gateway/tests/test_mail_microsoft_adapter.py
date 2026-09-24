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


def _params(http) -> dict:
    return [r[2].get("params") or {} for r in http.requests if r[0] == "GET"][0]


def test_fetch_text_search(adapter):
    msgs = adapter.fetch('TEXT "this is the body"')
    assert len(msgs) == 1
    # ConsistencyLevel header required by Graph for $search
    method, url, kwargs = [r for r in adapter._http.requests if "$search" in (r[2].get("params") or {})][0]
    assert kwargs["headers"]["ConsistencyLevel"] == "eventual"


def test_fetch_text_search_unescapes_imap_quoting(adapter, http):
    # tools/email.py's _imap_quote escapes a literal backslash/quote; the
    # criteria parser must undo that before the term reaches Graph.
    from memaix_gateway.tools.email import _imap_quote

    adapter.fetch(f'TEXT "{_imap_quote("a back\\\\slash")}"')
    assert _params(http)["$search"] == '"a back\\\\slash"'


def test_sender_and_dates_become_one_kql_search(adapter, http):
    """Graph refuses $search combined with $filter on messages, so a sender
    or text criterion carries the dates inside the KQL."""
    adapter.fetch('SINCE 01-Sep-2026 BEFORE 01-Oct-2026 FROM "anthropic.com" TEXT "faktura"')
    params = _params(http)
    assert params["$search"] == '"from:anthropic.com received>=2026-09-01 received<2026-10-01 faktura"'
    assert "$filter" not in params


def test_dates_alone_become_a_filter_newest_first(adapter, http):
    adapter.fetch("SINCE 01-Sep-2026 BEFORE 01-Oct-2026")
    params = _params(http)
    assert params["$filter"] == (
        "receivedDateTime ge 2026-09-01T00:00:00Z and receivedDateTime lt 2026-10-01T00:00:00Z"
    )
    assert params["$orderby"] == "receivedDateTime desc"
    assert "$search" not in params


def test_list_selects_no_body(adapter, http):
    adapter.fetch("ALL")
    assert "body" not in _params(http)["$select"].split(",")


def test_untranslatable_criteria_raise(adapter, http):
    from memaix_gateway.connectors.adapters.mail_criteria import UnsupportedCriteria

    with pytest.raises(UnsupportedCriteria):
        adapter.fetch('BODY "x"')
    assert http.requests == []


def test_limit_follows_next_link(http):
    pages = {
        None: {"value": [{"id": "a"}, {"id": "b"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/next?p=2"},
        "2": {"value": [{"id": "c"}, {"id": "d"}]},
    }

    class _Paged:
        requests: list = []

        def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            return _FakeResponse(pages["2" if url.endswith("p=2") else None])

    adapter = GraphMailAdapter("tok", _http=_Paged())
    assert [m.uid for m in adapter.fetch("ALL", limit=3)] == ["a", "b", "c"]


def test_folder_all_uses_every_folder(adapter, http):
    real = http.request

    def _all_messages(method, url, **kwargs):
        if url.endswith("/me/messages"):
            http.requests.append((method, url, kwargs))
            return _FakeResponse({"value": http.inbox})
        return real(method, url, **kwargs)

    http.request = _all_messages
    adapter.folder.set("ALL")
    assert [m.uid for m in adapter.fetch("SINCE 01-Sep-2026")] == ["m1"]
    assert http.requests[0][1].endswith("/me/messages")


def test_forbidden_mark_read_does_not_fail_the_read(adapter, http):
    real = http.request

    def _no_patch(method, url, **kwargs):
        if method == "PATCH":
            return _FakeResponse({"error": "forbidden"}, status_code=403)
        return real(method, url, **kwargs)

    http.request = _no_patch
    (msg,) = adapter.fetch("UID m1", mark_seen=True)
    assert msg.text == "hi there, this is the body"
    assert msg.seen is False


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
