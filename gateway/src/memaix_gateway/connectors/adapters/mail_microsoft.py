# SPDX-License-Identifier: AGPL-3.0-or-later
"""Microsoft Graph mail adapter (FEATURE-CONNECTOR-FRAMEWORK.md §7 step 6 —
first external adapter added purely by registering a ConnectorSpec, proof
that new integrations don't require touching tools/email.py).

Graph's REST API (JSON, folder IDs, $search/$filter) doesn't look anything
like IMAP, but connectors/base.py's MailBackend — and tools/email.py's
actual `_imap` usage — mirror imap_tools' MailBox exactly: `.folder.set(name)`,
`.fetch(criteria, mark_seen=, limit=)` with IMAP search criteria, and
`.append(message, folder=, flag_set=)`. Rather than redesigning that
(forbidden — every other mail path must keep working unchanged), this
adapter translates: mail_criteria.py parses the criteria tools/email.py
sends (ALL/UID/SINCE/BEFORE/FROM/TEXT) and `_graph_query` turns them into
$search/$filter, a `.folder` proxy mapping
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

import logging
from email import message_from_bytes
from email.message import Message

from .mail_criteria import Criteria, parse

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Map folder names to Graph's well-known folder names; "all" means every
# folder (/me/messages). Anything else is passed through as-is (Graph
# accepts a folder's display name in some contexts, but well-known ids are
# the only case actually exercised today).
_WELL_KNOWN_FOLDERS = {"inbox": "inbox", "drafts": "drafts", "sent": "sentitems", "all": "all"}

_MAX_PAGE = 1000  # Graph's $top ceiling for messages

# A list/search row needs no body; skipping it keeps big result sets cheap.
_LIST_SELECT = "id,subject,from,receivedDateTime,sentDateTime,isRead,toRecipients,ccRecipients,internetMessageId"


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


def _kql_term(value: str) -> str:
    # The whole $search value is one double-quoted string; an embedded quote
    # would end it early, so drop them rather than attempt escaping.
    return value.replace('"', "")


def _graph_query(c: Criteria) -> tuple[dict[str, str | int], dict[str, str]]:
    """(params, extra headers) for parsed IMAP criteria.

    Graph can't combine $search with $filter on messages, so: any sender or
    text criterion makes it a KQL $search (which also expresses the dates as
    `received>=`/`received<`); dates alone stay a $filter on
    receivedDateTime, newest first. SINCE is inclusive and BEFORE exclusive
    in both, matching IMAP.
    """
    if c.from_ or c.text:
        terms: list[str] = []
        if c.from_:
            terms.append(f"from:{_kql_term(c.from_)}")
        if c.since:
            terms.append(f"received>={c.since.isoformat()}")
        if c.before:
            terms.append(f"received<{c.before.isoformat()}")
        if c.text:
            terms.append(_kql_term(c.text))
        return {"$search": '"{}"'.format(" ".join(terms))}, {"ConsistencyLevel": "eventual"}
    clauses: list[str] = []
    if c.since:
        clauses.append(f"receivedDateTime ge {c.since.isoformat()}T00:00:00Z")
    if c.before:
        clauses.append(f"receivedDateTime lt {c.before.isoformat()}T00:00:00Z")
    if not clauses:
        return {}, {}
    return {"$filter": " and ".join(clauses), "$orderby": "receivedDateTime desc"}, {}


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

    def _messages_path(self) -> str:
        # "all" searches every folder (Graph's /me/messages), like Gmail's
        # no-label query; anything else is one folder.
        if self._folder == "all":
            return "/me/messages"
        return f"/me/mailFolders/{self._folder}/messages"

    def _list(self, parsed: Criteria, limit: int | None) -> list[dict]:
        """Up to `limit` messages matching `parsed`, following @odata.nextLink."""
        want = limit if limit else 10
        params, headers = _graph_query(parsed)
        params["$top"] = min(want, _MAX_PAGE)
        params["$select"] = _LIST_SELECT
        messages: list[dict] = []
        data = self._request("GET", self._messages_path(), params=params, headers=headers).json()
        while True:
            messages += data.get("value", [])
            next_link = data.get("@odata.nextLink")
            if len(messages) >= want or not next_link or not next_link.startswith(_GRAPH_BASE):
                break
            # nextLink is absolute and already carries every query parameter.
            data = self._request("GET", next_link[len(_GRAPH_BASE):], headers=dict(headers)).json()
        return messages[:want]

    def fetch(self, criteria: str = "ALL", *, mark_seen: bool = False, limit: int | None = None):
        parsed = parse(criteria)
        if parsed.uid is not None:
            messages = [self._request("GET", f"/me/messages/{parsed.uid}").json()]
        else:
            messages = self._list(parsed, limit)
        if mark_seen:
            for m in messages:
                self._mark_read(m)
        return [_GraphMessage(m) for m in messages]

    def _mark_read(self, m: dict) -> None:
        """Best-effort, like the Gmail adapter: a read never fails because
        flagging the message read did (Mail.Read alone may not write)."""
        if m.get("isRead"):
            return
        try:
            self._request("PATCH", f"/me/messages/{m['id']}", json={"isRead": True})
        except Exception as exc:  # noqa: BLE001 - a read must never fail on its side effect
            logger.warning("Graph: could not mark message read (%s)", type(exc).__name__)
            return
        m["isRead"] = True

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
