# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Gmail API mail adapter — the read path for a linked Google
account. Mirrors test_mail_microsoft_adapter.py: the adapter's job is to
make Gmail answer the exact imap_tools calls tools/email.py makes."""

from __future__ import annotations

import base64
from email.message import EmailMessage

import pytest

from memaix_gateway.connectors.adapters.mail_criteria import UnsupportedCriteria
from memaix_gateway.connectors.adapters.mail_google import (
    GmailAdapter,
    GmailPermissionDenied,
    GmailRateLimited,
)


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
    def __init__(self, data, status_code: int = 200, headers: dict | None = None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

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


def _list_params(http) -> list[dict]:
    return [kw.get("params") or {} for m, url, kw in http.requests if m == "GET" and url.endswith("/messages")]


# ------------------------------------------------------------------
# fetch — the IMAP criteria tools/email.py sends, translated to Gmail
# ------------------------------------------------------------------


def test_fetch_all_lists_inbox_messages():
    http = _FakeHttp()
    adapter = GmailAdapter("tok", _http=http)
    (msg,) = adapter.fetch("ALL")
    assert msg.uid == "m1"
    assert msg.subject == "Hello there"
    assert msg.from_ == "sender@example.com"
    assert msg.seen is False


def test_fetch_sends_the_bearer_token():
    http = _FakeHttp()
    GmailAdapter("tok-123", _http=http).fetch("ALL")
    _, _, kwargs = http.requests[0]
    assert kwargs["headers"]["Authorization"] == "Bearer tok-123"


def test_fetch_uid_reads_one_full_message_directly():
    """UID fetch (email_read) must not page the list endpoint first, and is
    the one fetch that asks for the body."""
    http = _FakeHttp()
    (msg,) = GmailAdapter("tok", _http=http).fetch("UID m1")
    assert msg.uid == "m1"
    assert msg.text == "hi there, this is the body"
    assert all(not url.endswith("/messages") for _, url, _ in http.requests)
    assert http.requests[0][2]["params"] == {"format": "full"}


def test_list_fetches_metadata_not_full_messages():
    """The N+1 is inherent to Gmail, but each of the N is a metadata get —
    headers and labels only — never the whole message with its body."""
    http = _FakeHttp(messages=[_message(f"m{i}", f"S{i}", "body") for i in range(3)])
    GmailAdapter("tok", _http=http).fetch("ALL", limit=3)
    gets = [kw["params"] for m, url, kw in http.requests if m == "GET" and "/messages/" in url]
    assert len(gets) == 3
    assert all(p["format"] == "metadata" for p in gets)
    assert {"Subject", "From", "Date"} <= set(gets[0]["metadataHeaders"])


@pytest.mark.parametrize(
    "criteria, expected_q",
    [
        ('TEXT "beta"', "beta"),
        ('FROM "anthropic.com"', 'from:"anthropic.com"'),
        ("SINCE 01-Sep-2026", "after:2026/09/01"),
        ("BEFORE 01-Oct-2026", "before:2026/10/01"),
        (
            'SINCE 01-Sep-2026 BEFORE 01-Oct-2026 FROM "noreply@loopia.se" TEXT "faktura has:attachment"',
            'from:"noreply@loopia.se" after:2026/09/01 before:2026/10/01 faktura has:attachment',
        ),
    ],
)
def test_search_criteria_become_a_gmail_query(criteria, expected_q):
    http = _FakeHttp(messages=[])
    GmailAdapter("tok", _http=http).fetch(criteria)
    assert _list_params(http)[0]["q"] == expected_q


def test_all_sends_no_query():
    http = _FakeHttp()
    GmailAdapter("tok", _http=http).fetch("ALL")
    assert "q" not in _list_params(http)[0]


def test_text_search_filters_results():
    http = _FakeHttp(
        messages=[
            _message("m1", "First", "alpha content"),
            _message("m2", "Second", "beta content"),
        ]
    )
    (msg,) = GmailAdapter("tok", _http=http).fetch('TEXT "beta"')
    assert msg.subject == "Second"


def test_search_term_is_unescaped_before_it_reaches_gmail():
    """tools/email.py escapes quotes for IMAP; Gmail must get the raw term."""
    http = _FakeHttp(messages=[_message("m1", "Quoted", 'say "hi" now')])
    GmailAdapter("tok", _http=http).fetch('TEXT "say \\"hi\\" now"')
    assert _list_params(http)[0]["q"] == 'say "hi" now'


@pytest.mark.parametrize("criteria", ['BODY "x"', "UNSEEN", 'SUBJECT "x"', "SINCE yesterday", 'TEXT "open'])
def test_untranslatable_criteria_raise_instead_of_listing_everything(criteria):
    """The old `else:  # "ALL"` turned every unknown criterion into the
    newest N messages, unfiltered — an answer-shaped wrong answer."""
    http = _FakeHttp()
    with pytest.raises(UnsupportedCriteria):
        GmailAdapter("tok", _http=http).fetch(criteria)
    assert http.requests == []


def test_limit_bounds_the_number_of_message_gets():
    """List returns ids only, so each message costs a request — limit has to
    actually bound them, not just the final list."""
    http = _FakeHttp(messages=[_message(f"m{i}", f"S{i}", "body") for i in range(5)])
    result = GmailAdapter("tok", _http=http).fetch("ALL", limit=2)
    assert len(result) == 2
    assert sum(1 for _, url, _ in http.requests if "/messages/" in url) == 2


class _PagedHttp(_FakeHttp):
    """messages.list that pages with nextPageToken, at most `page` ids at a
    time whatever maxResults asks for."""

    def __init__(self, messages, page):
        super().__init__(messages=messages)
        self.page = page

    def request(self, method, url, **kwargs):
        if method == "GET" and url.endswith("/messages"):
            self.requests.append((method, url, kwargs))
            params = kwargs.get("params") or {}
            start = int(params.get("pageToken") or 0)
            size = min(self.page, params["maxResults"])
            chunk = self.messages[start:start + size]
            data: dict = {"messages": [{"id": m["id"]} for m in chunk]}
            if start + size < len(self.messages):
                data["nextPageToken"] = str(start + size)
            return _FakeResponse(data)
        return super().request(method, url, **kwargs)


def test_limit_above_one_page_follows_next_page_token():
    http = _PagedHttp([_message(f"m{i}", f"S{i}", "b") for i in range(250)], page=100)
    result = GmailAdapter("tok", _http=http).fetch("ALL", limit=230)
    assert len(result) == 230
    assert [p.get("pageToken") for p in _list_params(http)] == [None, "100", "200"]
    assert [p["maxResults"] for p in _list_params(http)] == [230, 130, 30]


def test_paging_stops_when_the_mailbox_runs_out():
    http = _PagedHttp([_message(f"m{i}", f"S{i}", "b") for i in range(30)], page=20)
    result = GmailAdapter("tok", _http=http).fetch("ALL", limit=500)
    assert len(result) == 30
    assert len(_list_params(http)) == 2


def test_folder_all_searches_without_a_label():
    """Last month's mail is often archived — no longer in INBOX."""
    http = _FakeHttp()
    adapter = GmailAdapter("tok", _http=http)
    adapter.folder.set("ALL")
    adapter.fetch("SINCE 01-Sep-2026")
    assert "labelIds" not in _list_params(http)[0]


# ------------------------------------------------------------------
# Rate limits vs permission errors
# ------------------------------------------------------------------


def _google_error(status: int, reason: str, message: str = "nope", headers=None):
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}
    return _FakeResponse(body, status, headers)


class _FlakyHttp(_FakeHttp):
    """Answers the first `failures` requests with `error`, then behaves."""

    def __init__(self, error, failures):
        super().__init__()
        self.error = error
        self.failures = failures

    def request(self, method, url, **kwargs):
        if self.failures:
            self.failures -= 1
            self.requests.append((method, url, kwargs))
            return self.error
        return super().request(method, url, **kwargs)


@pytest.mark.parametrize(
    "error",
    [
        _google_error(403, "rateLimitExceeded"),
        _google_error(403, "userRateLimitExceeded"),
        _google_error(429, "rateLimitExceeded"),
    ],
)
def test_rate_limit_is_retried_with_backoff(error):
    sleeps: list[float] = []
    http = _FlakyHttp(error, failures=2)
    (msg,) = GmailAdapter("tok", _http=http, _sleep=sleeps.append).fetch("ALL")
    assert msg.uid == "m1"
    assert sleeps == [1.0, 2.0]


def test_retry_after_header_is_honoured():
    sleeps: list[float] = []
    http = _FlakyHttp(_google_error(429, "rateLimitExceeded", headers={"Retry-After": "7"}), failures=1)
    GmailAdapter("tok", _http=http, _sleep=sleeps.append).fetch("ALL")
    assert sleeps == [7.0]


def test_persistent_rate_limit_raises_a_rate_limit_error():
    sleeps: list[float] = []
    http = _FlakyHttp(_google_error(403, "rateLimitExceeded"), failures=100)
    with pytest.raises(GmailRateLimited, match="rate limit"):
        GmailAdapter("tok", _http=http, _sleep=sleeps.append).fetch("ALL")
    assert len(sleeps) == 4


def test_real_403_is_a_permission_error_and_not_retried():
    sleeps: list[float] = []
    error = _google_error(403, "insufficientPermissions", "Request had insufficient authentication scopes.")
    http = _FlakyHttp(error, failures=1)
    with pytest.raises(GmailPermissionDenied, match="insufficientPermissions") as exc:
        GmailAdapter("tok", _http=http, _sleep=sleeps.append).fetch("ALL")
    assert "not a rate limit" in str(exc.value)
    assert sleeps == []


# ------------------------------------------------------------------
# mark_seen — opt-in, and never fatal
# ------------------------------------------------------------------


def test_fetch_does_not_modify_labels_by_default():
    http = _FakeHttp()
    GmailAdapter("tok", _http=http).fetch("UID m1")
    assert [r[0] for r in http.requests] == ["GET"]


def test_mark_seen_removes_the_unread_label():
    http = _FakeHttp()
    (msg,) = GmailAdapter("tok", _http=http).fetch("UID m1", mark_seen=True)
    assert msg.seen is True
    modify = [r for r in http.requests if r[0] == "POST" and r[1].endswith("/modify")]
    assert modify[0][2]["json"] == {"removeLabelIds": ["UNREAD"]}


def test_mark_seen_skips_already_read_messages():
    http = _FakeHttp(messages=[_message("m1", "Read", "body", unread=False)])
    GmailAdapter("tok", _http=http).fetch("UID m1", mark_seen=True)
    assert not [r for r in http.requests if r[1].endswith("/modify")]


def test_forbidden_modify_does_not_fail_the_read():
    """Production, 2026-09-23/24: gmail.readonly can't modify, and the 403
    from POST .../modify took email_read down with it."""

    class _ReadOnlyHttp(_FakeHttp):
        def request(self, method, url, **kwargs):
            if url.endswith("/modify"):
                self.requests.append((method, url, kwargs))
                return _google_error(403, "insufficientPermissions")
            return super().request(method, url, **kwargs)

    http = _ReadOnlyHttp()
    (msg,) = GmailAdapter("tok", _http=http).fetch("UID m1", mark_seen=True)
    assert msg.text == "hi there, this is the body"
    assert msg.seen is False  # truthfully still unread


# ------------------------------------------------------------------
# Message shape — what _msg_to_dict reads
# ------------------------------------------------------------------


def test_nested_multipart_body_is_found():
    http = _FakeHttp(messages=[_message("m1", "S", "plain text", html="<p>rich</p>")])
    (msg,) = GmailAdapter("tok", _http=http).fetch("UID m1")
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

    GmailAdapter("tok", _http=http).append(msg.as_bytes(), folder="Drafts", flag_set=("\\Draft",))

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

    GmailAdapter("tok", _http=http).append(msg.as_bytes(), folder="Drafts", flag_set=("\\Draft",))

    raw = http.drafts_created[0]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "In-Reply-To: <parent@example.com>" in decoded


def test_append_pins_reply_draft_to_the_originals_thread():
    """drafts.create only threads a draft when given threadId — headers
    alone are not enough over the API."""

    class _ThreadHttp(_FakeHttp):
        def request(self, method, url, **kwargs):
            params = kwargs.get("params") or {}
            if method == "GET" and url.endswith("/messages") and str(params.get("q", "")).startswith("rfc822msgid:"):
                self.requests.append((method, url, kwargs))
                assert params["q"] == "rfc822msgid:parent@example.com"
                return _FakeResponse({"messages": [{"id": "m1", "threadId": "t-77"}]})
            return super().request(method, url, **kwargs)

    http = _ThreadHttp()
    msg = EmailMessage()
    msg["Subject"] = "Re: thread"
    msg["In-Reply-To"] = "<parent@example.com>"
    msg.set_content("reply")

    GmailAdapter("tok", _http=http).append(msg.as_bytes(), folder="Drafts", flag_set=("\\Draft",))

    assert http.drafts_created[0]["message"]["threadId"] == "t-77"


def test_append_without_reply_does_not_look_up_a_thread():
    http = _FakeHttp()
    msg = EmailMessage()
    msg["Subject"] = "New"
    msg.set_content("x")

    GmailAdapter("tok", _http=http).append(msg.as_bytes(), folder="Drafts")

    assert [r[0] for r in http.requests] == ["POST"]
    assert "threadId" not in http.drafts_created[0]["message"]


def test_fetched_message_exposes_imap_tools_style_headers():
    m = _message("m9", "Hi", "b")
    m["payload"]["headers"].append({"name": "Message-ID", "value": "<m9@example.com>"})
    http = _FakeHttp(messages=[m])

    fetched = GmailAdapter("tok", _http=http).fetch("UID m9")[0]

    assert fetched.headers["message-id"] == ("<m9@example.com>",)


def test_logout_is_a_noop():
    GmailAdapter("tok", _http=_FakeHttp()).logout()


def test_http_error_propagates():
    http = _FakeHttp()
    with pytest.raises(RuntimeError):
        GmailAdapter("tok", _http=http).fetch("UID nope")
