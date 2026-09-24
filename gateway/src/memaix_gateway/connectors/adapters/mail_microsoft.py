# SPDX-License-Identifier: AGPL-3.0-or-later
"""Microsoft Graph mail adapter (FEATURE-CONNECTOR-FRAMEWORK.md §7 step 6 —
first external adapter added purely by registering a ConnectorSpec, proof
that new integrations don't require touching tools/email.py).

Graph's REST API (JSON, folder IDs, $search/$filter) doesn't look anything
like IMAP, but connectors/base.py's MailBackend — and tools/email.py's
actual `_imap` usage — mirror imap_tools' MailBox exactly: `.folder.set(name)`,
`.fetch(criteria, mark_seen=, limit=)` with criteria strings "ALL" /
f"UID {id}" / f'BODY "{query}"', and `.append(message, folder=, flag_set=)`.
Rather than redesigning that (forbidden — every other mail path must keep
working unchanged), this adapter translates: a tiny parser for the exact
three criteria strings tools/email.py ever sends, a `.folder` proxy mapping
folder names to Graph's well-known folder ids, and a message wrapper
exposing the same attributes (`uid`/`subject`/`from_`/`date_str`/`seen`/
`to`/`cc`/`text`/`html`) imap_tools messages have.

v1 scope: read (list/read/search) + append-to-Drafts — everything
tools/email.py calls `_imap` for. `email_send` stays on SMTP; Graph's own
`/me/sendMail` is future work if that path is ever migrated too.
`In-Reply-To` threading is dropped when creating a Graph draft (v1.0 Graph
has no simple way to set arbitrary MIME headers on a new message) — a
documented gap, not a silent one.
"""

from __future__ import annotations

from email import message_from_bytes
from email.message import Message

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# tools/email.py only ever passes "INBOX" or "Drafts" — map to Graph's
# well-known folder names; anything else is passed through as-is (Graph
# accepts a folder's display name in some contexts, but well-known ids are
# the only case actually exercised today).
_WELL_KNOWN_FOLDERS = {"inbox": "inbox", "drafts": "drafts"}


class _GraphMessage:
    """Wraps one Graph message JSON object with the attributes
    tools/email.py's `_msg_to_dict` reads off an imap_tools message."""

    def __init__(self, data: dict) -> None:
        self.uid = data["id"]
        self.subject = data.get("subject") or ""
        self.from_ = (data.get("from") or {}).get("emailAddress", {}).get("address", "")
        self.date_str = data.get("receivedDateTime") or data.get("sentDateTime") or ""
        self.seen = bool(data.get("isRead"))
        # Same shape as imap_tools' MailMessage.headers, so email_create_draft
        # can read the original's Message-ID when threading a reply.
        internet_id = data.get("internetMessageId")
        self.headers = {"message-id": (internet_id,)} if internet_id else {}
        self.to = [r["emailAddress"]["address"] for r in data.get("toRecipients", [])]
        self.cc = [r["emailAddress"]["address"] for r in data.get("ccRecipients", [])]
        body = data.get("body") or {}
        content = body.get("content", "")
        if body.get("contentType") == "html":
            self.html, self.text = content, ""
        else:
            self.html, self.text = "", content


def _imap_unquote(value: str) -> str:
    """Reverse tools/email.py's `_imap_quote` escaping to recover the raw
    search term before handing it to Graph's $search."""
    return value.replace('\\"', '"').replace("\\\\", "\\")


class _FolderProxy:
    """`mb.folder.set(name)` — the one imap_tools call tools/email.py makes
    that isn't part of connectors/base.py's declared MailBackend Protocol."""

    def __init__(self, adapter: "GraphMailAdapter") -> None:
        self._adapter = adapter

    def set(self, name: str) -> None:
        self._adapter._folder = _WELL_KNOWN_FOLDERS.get(name.lower(), name.lower())


class GraphMailAdapter:
    """MailBackend over Microsoft Graph. `access_token` must already be a
    live, unexpired bearer token — refreshing it is the caller's job (see
    server.py's `_ensure_fresh_microsoft_mail_token`), same division of
    responsibility as the existing Google calendar per-user flow."""

    def __init__(self, access_token: str, *, _http=None) -> None:
        self._token = access_token
        self._http = _http  # injected for tests: object with .request(method, url, **kw)
        self._folder = "inbox"  # a fresh connection defaults to INBOX, like imap_tools

    @property
    def folder(self) -> _FolderProxy:
        return _FolderProxy(self)

    def _request(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {self._token}")
        headers.setdefault("Content-Type", "application/json")
        url = _GRAPH_BASE + path
        if self._http is not None:
            resp = self._http.request(method, url, headers=headers, **kwargs)
        else:
            import requests

            resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        resp.raise_for_status()
        return resp

    def fetch(self, criteria: str = "ALL", *, mark_seen: bool = False, limit: int | None = None):
        if criteria.startswith("UID "):
            data = self._request("GET", f"/me/messages/{criteria[len('UID '):]}").json()
            messages = [data]
        elif criteria.startswith('BODY "') and criteria.endswith('"'):
            query = _imap_unquote(criteria[len('BODY "'):-1])
            search_params: dict[str, str | int] = {"$search": f'"{query}"'}
            if limit:
                search_params["$top"] = limit
            data = self._request(
                "GET", f"/me/mailFolders/{self._folder}/messages",
                params=search_params, headers={"ConsistencyLevel": "eventual"},
            ).json()
            messages = data.get("value", [])
        else:  # "ALL"
            list_params: dict[str, int] = {"$top": limit} if limit else {}
            data = self._request("GET", f"/me/mailFolders/{self._folder}/messages", params=list_params).json()
            messages = data.get("value", [])

        if mark_seen:
            for m in messages:
                if not m.get("isRead"):
                    self._request("PATCH", f"/me/messages/{m['id']}", json={"isRead": True})
                    m["isRead"] = True
        return [_GraphMessage(m) for m in messages]

    def append(self, message: bytes, folder: str = "INBOX", dt=None, flag_set=None) -> None:
        """Graph has no raw-MIME append; parse the message tools/email.py
        built and re-create it as a Graph draft — a faithful translation of
        the subject/to/cc/body fields email_create_draft actually sets.
        Signature mirrors imap_tools.MailBox.append; `folder`, `dt` and
        `flag_set` are ignored (this only ever creates drafts)."""
        parsed: Message = message_from_bytes(message)

        def _addrs(header: str) -> list[dict]:
            raw = parsed.get(header, "")
            return [{"emailAddress": {"address": a.strip()}} for a in raw.split(",") if a.strip()]

        if parsed.is_multipart():
            body_text = ""
            for part in parsed.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    body_text = payload.decode(errors="replace") if isinstance(payload, bytes) else ""
                    break
        else:
            payload = parsed.get_payload(decode=True)
            body_text = payload.decode(errors="replace") if isinstance(payload, bytes) else str(parsed.get_payload())

        draft = {
            "subject": parsed.get("Subject", ""),
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": _addrs("To"),
            "ccRecipients": _addrs("Cc"),
        }
        self._request("POST", "/me/mailFolders/drafts/messages", json=draft)

    def logout(self) -> None:
        pass  # stateless REST — nothing to close
