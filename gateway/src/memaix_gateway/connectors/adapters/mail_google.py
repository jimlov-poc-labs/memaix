# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gmail API mail adapter — the read path for a linked Google account.

Same translation job as mail_microsoft.py, third foreign shape: tools/
email.py speaks imap_tools (`.folder.set(name)`, `.fetch(criteria, ...)`
with the three criteria strings "ALL" / f"UID {id}" / f'BODY "{query}"',
`.append(message, folder=, flag_set=)`), and this adapter translates that to
Gmail's REST API rather than changing the caller. Every other mail path
keeps working unchanged.

Two Gmail specifics worth knowing before reading the code:

1. **List returns ids, not messages.** `users.messages.list` gives only
   {id, threadId}; each message needs its own `get`. That N+1 is inherent
   to the API, which is why `limit` is honoured strictly — an unbounded
   fetch would be one HTTP request per message in the mailbox.
2. **Bodies are base64url inside a MIME part tree.** `_walk_parts` hunts
   the first text/plain and text/html leaf, matching what `_msg_to_dict`
   reads off an imap_tools message.

v1 scope mirrors the Graph adapter: read (list/read/search) + append-to-
Drafts, i.e. everything tools/email.py calls `_imap` for. `email_send`
stays on shared SMTP — sending as the linked account needs the
`gmail.send` scope, which is deliberately not requested today
(`gmail.readonly` + `gmail.compose` are). Creating a draft is within
`gmail.compose`, so append works; actually sending it does not, and that
is a documented gap rather than a silent failure.
"""

from __future__ import annotations

import base64

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# tools/email.py only ever passes "INBOX" or "Drafts". Gmail has labels
# rather than folders; these are the two system label ids that correspond.
# Anything else is upper-cased and passed through — a custom label id works
# as-is, an unknown one simply matches nothing.
_SYSTEM_LABELS = {"inbox": "INBOX", "drafts": "DRAFT"}


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


def _imap_unquote(value: str) -> str:
    """Reverse tools/email.py's `_imap_quote` escaping to recover the raw
    search term before handing it to Gmail's `q`."""
    return value.replace('\\"', '"').replace("\\\\", "\\")


class _FolderProxy:
    """`mb.folder.set(name)` — the one imap_tools call tools/email.py makes
    that isn't part of connectors/base.py's declared MailBackend Protocol."""

    def __init__(self, adapter: "GmailAdapter") -> None:
        self._adapter = adapter

    def set(self, name: str) -> None:
        self._adapter._label = _SYSTEM_LABELS.get(name.lower(), name.upper())


class GmailAdapter:
    """MailBackend over the Gmail API. `access_token` must already be a
    live, unexpired bearer token — refreshing it is the caller's job (see
    server.py's `_ensure_fresh_google_mail_token`), the same division of
    responsibility as the Graph and Google-calendar per-user flows."""

    def __init__(self, access_token: str, *, _http=None) -> None:
        self._token = access_token
        self._http = _http  # injected for tests: object with .request(method, url, **kw)
        self._label = "INBOX"  # a fresh connection defaults to INBOX, like imap_tools

    @property
    def folder(self) -> _FolderProxy:
        return _FolderProxy(self)

    def _request(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {self._token}")
        headers.setdefault("Content-Type", "application/json")
        url = _GMAIL_BASE + path
        if self._http is not None:
            resp = self._http.request(method, url, headers=headers, **kwargs)
        else:
            import requests

            resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        resp.raise_for_status()
        return resp

    def _get_message(self, message_id: str) -> dict:
        return self._request("GET", f"/messages/{message_id}", params={"format": "full"}).json()

    def _list_ids(self, query: str | None, limit: int | None) -> list[str]:
        params: dict[str, object] = {"labelIds": self._label}
        if query:
            params["q"] = query
        # maxResults caps the page, not the total; combined with taking a
        # single page this bounds the follow-up gets to `limit` requests.
        params["maxResults"] = limit if limit else 20
        data = self._request("GET", "/messages", params=params).json()
        return [m["id"] for m in data.get("messages") or []]

    def fetch(self, criteria: str = "ALL", *, mark_seen: bool = False, limit: int | None = None):
        if criteria.startswith("UID "):
            messages = [self._get_message(criteria[len("UID "):])]
        elif criteria.startswith('BODY "') and criteria.endswith('"'):
            query = _imap_unquote(criteria[len('BODY "'):-1])
            messages = [self._get_message(i) for i in self._list_ids(query, limit)]
        else:  # "ALL"
            messages = [self._get_message(i) for i in self._list_ids(None, limit)]

        if mark_seen:
            for m in messages:
                if "UNREAD" in (m.get("labelIds") or []):
                    self._request(
                        "POST", f"/messages/{m['id']}/modify",
                        json={"removeLabelIds": ["UNREAD"]},
                    )
                    m["labelIds"] = [lbl for lbl in m["labelIds"] if lbl != "UNREAD"]
        return [_GmailMessage(m) for m in messages]

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
        pass  # stateless REST — nothing to close
