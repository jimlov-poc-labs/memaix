# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gmail API mail adapter — the read path for a linked Google account.

Same translation job as mail_microsoft.py, third foreign shape: tools/
email.py speaks imap_tools (`.folder.set(name)`, `.fetch(criteria, ...)`
with the IMAP criteria mail_criteria.py parses, `.append(message, folder=,
flag_set=)`), and this adapter translates that to Gmail's REST API rather
than changing the caller. Every other mail path keeps working unchanged.

Gmail specifics worth knowing before reading the code:

1. **List returns ids, not messages.** `users.messages.list` gives only
   {id, threadId}; each message needs its own `get`. Listing and searching
   therefore fetch `format=metadata` (headers + labels, no body) over one
   keep-alive session, and only `email_read` (a `UID` fetch) asks for
   `format=full`. The list itself is paged with `pageToken` until `limit`
   ids are collected.
2. **Search is Gmail's own `q`.** SINCE/BEFORE/FROM/TEXT become
   `after:`/`before:`/`from:`/free text. Free text is passed as-is, so Gmail
   operators such as `has:attachment` work inside a query.
3. **Rate limits come back as 403 as well as 429.** `rateLimitExceeded` /
   `userRateLimitExceeded` are retried with backoff; any other 403 is a
   real permission problem and is reported as one.
4. **Reading is side-effect free.** A linked account normally has only
   `gmail.readonly` + `gmail.compose`, neither of which may modify labels,
   so `mark_seen` is opt-in and a failed modify never fails the read.
5. **Bodies are base64url inside a MIME part tree.** `_walk_parts` hunts
   the first text/plain and text/html leaf, matching what `_msg_to_dict`
   reads off an imap_tools message.

v1 scope mirrors the Graph adapter: read (list/read/search) + append-to-
Drafts, i.e. everything tools/email.py calls `_imap` for. `email_send`
stays on shared SMTP — sending as the linked account needs the
`gmail.send` scope, which is deliberately not requested today. Creating a
draft is within `gmail.compose`, so append works; actually sending it does
not, and that is a documented gap rather than a silent failure.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from .mail_criteria import Criteria, parse

logger = logging.getLogger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Gmail has labels rather than folders; these map the folder names
# tools/email.py passes to system label ids. "ALL" (or Gmail IMAP's "All
# Mail") means no label filter at all, i.e. archived mail too. Anything else
# is upper-cased and passed through — a custom label id works as-is, an
# unknown one simply matches nothing.
_SYSTEM_LABELS: dict[str, str | None] = {
    "inbox": "INBOX", "drafts": "DRAFT", "sent": "SENT",
    "all": None, "[gmail]/all mail": None, "[google mail]/all mail": None,
}

# Headers a list/search row needs (_msg_to_dict without full=True) plus the
# threading headers email_create_draft reads off a looked-up original.
_METADATA_HEADERS = ["Subject", "From", "Date", "To", "Cc", "Message-ID", "References"]

_MAX_PAGE = 500  # messages.list's maxResults ceiling

# 403 reasons that mean "slow down", not "you may not".
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
_MAX_RETRIES = 4
_MAX_BACKOFF_S = 32.0


class GmailRateLimited(RuntimeError):
    """Gmail kept answering rate-limited after the retries ran out."""


class GmailPermissionDenied(PermissionError):
    """Gmail refused the call for a reason other than rate limiting."""


def _b64url_decode(data: str) -> bytes:
    """Gmail omits base64 padding; restore it before decoding."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _walk_parts(part: dict, out: dict) -> None:
    """Collect the first text/plain and text/html body found, depth-first.

    Gmail nests parts arbitrarily (multipart/alternative inside
    multipart/mixed when there are attachments), so this recurses rather
    than looking only one level down.
    """
    mime = part.get("mimeType", "")
    body = part.get("body") or {}
    data = body.get("data")
    if data and mime in ("text/plain", "text/html") and not out.get(mime):
        out[mime] = _b64url_decode(data).decode(errors="replace")
    for child in part.get("parts") or []:
        _walk_parts(child, out)


class _GmailMessage:
    """Wraps one Gmail message resource with the attributes tools/email.py's
    `_msg_to_dict` reads off an imap_tools message."""

    def __init__(self, data: dict) -> None:
        self.uid = data["id"]
        payload = data.get("payload") or {}
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers") or []
        }
        # Same shape as imap_tools' MailMessage.headers, so email_create_draft
        # can read Message-ID/References when threading a reply.
        self.headers = {name: (value,) for name, value in headers.items()}
        self.subject = headers.get("subject", "")
        self.from_ = headers.get("from", "")
        # Prefer the Date header so the value matches what other mail
        # backends return; internalDate (epoch ms) is the fallback.
        self.date_str = headers.get("date") or data.get("internalDate", "")
        self.seen = "UNREAD" not in (data.get("labelIds") or [])
        self.to = _split_addrs(headers.get("to", ""))
        self.cc = _split_addrs(headers.get("cc", ""))
        bodies: dict[str, str] = {}
        _walk_parts(payload, bodies)
        self.text = bodies.get("text/plain", "")
        self.html = bodies.get("text/html", "")


def _split_addrs(raw: str) -> list[str]:
    return [a.strip() for a in raw.split(",") if a.strip()]


def _gmail_query(c: Criteria) -> str | None:
    """Gmail `q` for parsed IMAP criteria. Gmail's `after:`/`before:` with a
    date are inclusive/exclusive exactly like IMAP SINCE/BEFORE."""
    parts: list[str] = []
    if c.from_:
        parts.append('from:"{}"'.format(c.from_.replace('"', "")))
    if c.since:
        parts.append(f"after:{c.since:%Y/%m/%d}")
    if c.before:
        parts.append(f"before:{c.before:%Y/%m/%d}")
    if c.text:
        parts.append(c.text)
    return " ".join(parts) or None


def _error_reason(resp) -> tuple[str, str]:
    """(reason, message) from a Google API error body, or ("", "")."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except Exception:  # noqa: BLE001 - a non-JSON error body simply has no reason
        return "", ""
    if not isinstance(err, dict):
        return "", ""
    errors = err.get("errors") or [{}]
    reason = (errors[0] or {}).get("reason") or ""
    return str(reason), str(err.get("message") or "")


def _backoff_seconds(resp, attempt: int) -> float:
    """Retry-After if the server sent one, else exponential 1, 2, 4, 8 s."""
    retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if retry_after is not None:
        try:
            return min(float(retry_after), _MAX_BACKOFF_S)
        except (TypeError, ValueError):
            pass  # an HTTP-date Retry-After falls back to exponential
    return min(float(2 ** attempt), _MAX_BACKOFF_S)


class _FolderProxy:
    """`mb.folder.set(name)` — the one imap_tools call tools/email.py makes
    that isn't part of connectors/base.py's declared MailBackend Protocol."""

    def __init__(self, adapter: "GmailAdapter") -> None:
        self._adapter = adapter

    def set(self, name: str) -> None:
        key = name.lower()
        self._adapter._label = _SYSTEM_LABELS[key] if key in _SYSTEM_LABELS else name.upper()


class GmailAdapter:
    """MailBackend over the Gmail API. `access_token` must already be a
    live, unexpired bearer token — refreshing it is the caller's job (see
    server.py's `_ensure_fresh_google_mail_token`), the same division of
    responsibility as the Graph and Google-calendar per-user flows."""

    def __init__(self, access_token: str, *, _http=None, _sleep=time.sleep) -> None:
        self._token = access_token
        self._http = _http  # injected for tests: object with .request(method, url, **kw)
        self._sleep = _sleep  # injected for tests so backoff does not actually wait
        self._session: Any = None  # requests.Session, one keep-alive for the per-message gets
        self._label: str | None = "INBOX"  # a fresh connection defaults to INBOX, like imap_tools

    @property
    def folder(self) -> _FolderProxy:
        return _FolderProxy(self)

    def _send(self, method: str, url: str, headers: dict, **kwargs):
        if self._http is not None:
            return self._http.request(method, url, headers=headers, **kwargs)
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session.request(method, url, headers=headers, timeout=15, **kwargs)

    def _request(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {self._token}")
        headers.setdefault("Content-Type", "application/json")
        url = _GMAIL_BASE + path
        status, reason = 429, ""
        for attempt in range(_MAX_RETRIES + 1):
            resp = self._send(method, url, headers, **kwargs)
            status = getattr(resp, "status_code", 200)
            if status not in (403, 429):
                resp.raise_for_status()
                return resp
            reason, message = _error_reason(resp)
            if status == 403 and reason not in _RATE_LIMIT_REASONS:
                raise GmailPermissionDenied(
                    f"Gmail denied {method} {path} (403 {reason or 'forbidden'}): "
                    f"{message or 'the linked account lacks permission for this call'}. "
                    "This is a permission problem, not a rate limit: re-link the Google "
                    "account if it should have access."
                )
            if attempt < _MAX_RETRIES:
                self._sleep(_backoff_seconds(resp, attempt))
        raise GmailRateLimited(
            f"Gmail rate limit exceeded ({status} {reason or 'rateLimitExceeded'}) after "
            f"{_MAX_RETRIES} retries; try again shortly or ask for fewer messages."
        )

    def _get_message(self, message_id: str, *, full: bool) -> dict:
        params: dict[str, object] = {"format": "full"}
        if not full:
            params = {"format": "metadata", "metadataHeaders": _METADATA_HEADERS}
        return self._request("GET", f"/messages/{message_id}", params=params).json()

    def _list_ids(self, query: str | None, limit: int | None) -> list[str]:
        """Up to `limit` message ids, newest first, following nextPageToken."""
        want = limit if limit else 20
        ids: list[str] = []
        page_token = None
        while len(ids) < want:
            params: dict[str, object] = {"maxResults": min(want - len(ids), _MAX_PAGE)}
            if self._label:
                params["labelIds"] = self._label
            if query:
                params["q"] = query
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", "/messages", params=params).json()
            ids += [m["id"] for m in data.get("messages") or []]
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids[:want]

    def fetch(self, criteria: str = "ALL", *, mark_seen: bool = False, limit: int | None = None):
        parsed = parse(criteria)
        if parsed.uid is not None:
            messages = [self._get_message(parsed.uid, full=True)]
        else:
            ids = self._list_ids(_gmail_query(parsed), limit)
            messages = [self._get_message(i, full=False) for i in ids]
        if mark_seen:
            for m in messages:
                self._mark_read(m)
        return [_GmailMessage(m) for m in messages]

    def _mark_read(self, m: dict) -> None:
        """Best-effort removal of UNREAD. It needs gmail.modify, which a
        linked account usually lacks, so a failure is logged and swallowed —
        the message then truthfully keeps reporting itself unread."""
        if "UNREAD" not in (m.get("labelIds") or []):
            return
        try:
            self._request("POST", f"/messages/{m['id']}/modify", json={"removeLabelIds": ["UNREAD"]})
        except Exception as exc:  # noqa: BLE001 - a read must never fail on its side effect
            logger.warning("Gmail: could not mark message read (%s)", type(exc).__name__)
            return
        m["labelIds"] = [lbl for lbl in m["labelIds"] if lbl != "UNREAD"]

    def _thread_id_for(self, message_id: str) -> str | None:
        """Gmail thread id of the message with RFC 822 Message-ID
        `message_id`, or None if this mailbox doesn't have it."""
        params = {"q": f"rfc822msgid:{message_id.strip().strip('<>')}", "maxResults": 1}
        found = self._request("GET", "/messages", params=params).json().get("messages") or []
        return found[0].get("threadId") if found else None

    def append(self, message: bytes, folder: str = "INBOX", dt=None, flag_set=None) -> None:
        """Create a Gmail draft from the raw MIME tools/email.py built.

        Signature mirrors imap_tools.MailBox.append. Unlike Graph, Gmail
        accepts raw RFC-822, so the message survives intact — including the
        In-Reply-To/References headers the Graph adapter has to drop. For a
        reply the draft is also pinned to the original's threadId: the API
        (unlike IMAP APPEND) only threads a draft when told the thread.
        `folder`, `dt` and `flag_set` are ignored: drafts.create always files
        into DRAFT, which is the only folder email_create_draft targets.
        """
        from email.parser import BytesHeaderParser

        body: dict[str, object] = {"raw": base64.urlsafe_b64encode(message).decode().rstrip("=")}
        in_reply_to = BytesHeaderParser().parsebytes(message).get("In-Reply-To")
        if in_reply_to:
            thread_id = self._thread_id_for(str(in_reply_to))
            if thread_id:
                body["threadId"] = thread_id
        self._request("POST", "/drafts", json={"message": body})

    def logout(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
