# SPDX-License-Identifier: AGPL-3.0-or-later
"""email_* tools — IMAP/SMTP with injected client for testability.

The *_imap / *_smtp keyword arguments accept duck-typed objects whose
interface is documented below.  When None, a real imap_tools.MailBox /
smtplib.SMTP connection is created from project config.

_imap duck type (must implement):
  fetch(criteria='ALL', *, mark_seen=False, limit=None) -> Iterable[msg]
    criteria is an IMAP search string: "ALL", "UID <id>", or any mix of
    SINCE/BEFORE/FROM/TEXT. Adapters that are not IMAP parse it with
    connectors/adapters/mail_criteria.py and raise on anything else.
    where msg has: uid, subject, from_, to, cc, date_str, text, html, and
    either flags (imap_tools.MailMessage) or seen (other connectors, e.g.
    the Microsoft Graph adapter) — _msg_to_dict checks for flags first.
    Messages may also expose `headers` (lowercase name -> tuple of values,
    as imap_tools.MailMessage does); email_create_draft reads Message-ID and
    References from it to thread a reply.
  folder.set(name: str)
    — every adapter must accept "ALL" (= the whole mailbox) itself:
    MultiMailBackend passes the same name to every source, so a name only
    some adapters understand fails on the others (2026-09-24: "ALL" on
    Gmail/Graph but not IMAP; see connectors/adapters/mail_imap_all.py).
    Optional attribute `source_errors` ([{source, error}]) after fetch
    reports parts that failed; email_list/email_search append it as a
    warning row.
  folder.list() -> [FolderInfo(name, delim, flags)]   (optional; real IMAP
    only — used to find the SPECIAL-USE \\Drafts folder)
  append(message: bytes, folder='INBOX', dt=None, flag_set=None)
    — exactly imap_tools.MailBox.append's signature. Always call it with
    `folder=` and `flag_set=` as keywords: the second positional slot is
    `folder`, so `append(msg, "\\Draft", folder=...)` raises
    "got multiple values for argument 'folder'" against a real MailBox.
  logout()

_smtp duck type:
  send_message(msg: email.message.EmailMessage)

Feature gate:
  acl.resource(project, "allow_send") must be truthy to use email_send.

Outbox gate:
  email_send is outgoing and hard to undo, so it is also routed through the
  approval outbox (see outbox/policy.py). When action_mode() resolves to
  'review' (project's outbox=review, global default, or an unlisted
  recipient), the send is queued instead of executed and the call returns
  {"pending": True, "action_id": ...}. Passing _confirmed=True (used by
  outbox.execute after an operator approves) always executes immediately.
"""

from __future__ import annotations

import datetime
import smtplib
from email.message import EmailMessage
from typing import Any, NamedTuple

from .. import config
from ..acl import Acl

_IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _to_imap_date(iso_date: str) -> str:
    """Convert ISO date string (YYYY-MM-DD or YYYY-MM) to IMAP date literal."""
    d = datetime.date.fromisoformat(iso_date if len(iso_date) > 7 else iso_date + "-01")
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"

# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _mailbox_cfg(acl: Acl, project: str) -> dict:
    cfg = acl.resource(project, "mailbox")
    if not cfg:
        raise ValueError(f"project {project!r} has no mailbox configured")
    return cfg


def _inbox_address(acl: Acl, project: str) -> str:
    """The project's own mailbox address, or "" if it has none.

    Deliberately tolerant where _mailbox_cfg is strict. The acl.yaml
    `mailbox` resource carries two unrelated things: the credentials
    _make_mailbox needs (host/user/password_ref) and a human-readable
    "which inbox is this?" address. A project whose only mail source is a
    per-user linked account (Google, Microsoft) has the second concern but
    not the first — the connector framework resolved its adapter from the
    token store, never from acl.yaml. Raising here would make such a
    project unable to list its own mail.
    """
    return (acl.resource(project, "mailbox") or {}).get("user", "")


def _inbox_of(m, mb, fallback: str) -> str:
    """Which inbox message `m` came from.

    A message fetched through MultiMailBackend carries its own source's
    address (`m.inbox`); a single linked-account backend carries it on the
    adapter (`mb.inbox_address`, set by server._mail_backend). Only a shared
    acl.yaml mailbox falls back to the acl.yaml address — stamping that on
    every message regardless of source is what made a Gmail message look as
    if it had arrived in the project's IMAP inbox.
    """
    return getattr(m, "inbox", None) or getattr(mb, "inbox_address", None) or fallback


def _make_mailbox(acl: Acl, project: str):
    from imap_tools import MailBox

    from ..connectors.adapters.mail_imap_all import wrap

    cfg = _mailbox_cfg(acl, project)
    password = config.secret(cfg.get("password_ref"))
    if password is None:
        raise ValueError(f"no password configured for mailbox (project {project!r})")
    mb = MailBox(cfg["host"])
    mb.login(cfg["user"], password)
    # A real IMAP server has no "ALL" folder; the wrapper makes
    # folder="ALL" mean every folder, as it does on Gmail and Graph.
    return wrap(mb)


def _with_source_errors(rows: list[dict], mb) -> list[dict]:
    """Append a warning row if part of the mailbox could not be read.

    A backend that fans out (MultiMailBackend over several sources,
    AllFoldersMailBox over several folders) no longer fails the whole call
    when one part fails; it returns what it could and lists the rest in
    `source_errors`. Returning the hits without saying so would pass a
    partial result off as complete, so the caller gets a final row:
    {"warning": ..., "source_errors": [{"source", "error"}, ...]}.
    """
    errors = getattr(mb, "source_errors", None)
    if not isinstance(errors, list) or not errors:
        return rows
    return rows + [{
        "warning": (
            f"Ofullständigt resultat: {len(errors)} källa/källor kunde inte läsas (se source_errors). "
            "Träffarna kommer bara från de källor som svarade."
        ),
        "source_errors": errors,
    }]


def _imap_quote(value: str) -> str:
    """Escape a string for safe use inside an IMAP quoted-string.

    Within IMAP quoted-strings the backslash and double-quote are the only
    characters that must be escaped; CR/LF are stripped since they can never
    appear in a quoted-string and would otherwise allow command injection.
    """
    value = value.replace("\r", " ").replace("\n", " ")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _msg_to_dict(m, full: bool = False, *, inbox: str = "") -> dict:
    base: dict = {
        "id": str(m.uid),
        "subject": m.subject,
        "from": m.from_,
        "date": m.date_str,
        "seen": "\\Seen" in m.flags if hasattr(m, "flags") else bool(m.seen),
    }
    if inbox:
        base["inbox"] = inbox
    if full:
        base.update(
            {
                "to": list(m.to) if m.to else [],
                "cc": list(m.cc) if m.cc else [],
                "body": m.text or m.html or "",
            }
        )
    return base


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def email_list(
    acl: Acl,
    user_id: str,
    project: str,
    folder: str = "INBOX",
    limit: int = 20,
    *,
    _imap=None,
) -> list[dict]:
    """List recent messages.  Returns [{id, subject, from, date, seen, inbox}],
    plus a final {warning, source_errors} row if a source could not be read."""
    acl.enforce(user_id, project, "collaborator")
    mb = _imap if _imap is not None else _make_mailbox(acl, project)
    mb.folder.set(folder)
    msgs = list(mb.fetch("ALL", mark_seen=False, limit=limit))
    fallback = _inbox_address(acl, project)
    return _with_source_errors([_msg_to_dict(m, inbox=_inbox_of(m, mb, fallback)) for m in msgs], mb)


def email_read(
    acl: Acl,
    user_id: str,
    project: str,
    id: str,
    *,
    mark_seen: bool = False,
    _imap=None,
) -> dict:
    """Fetch a single message by UID.  Returns full message dict.

    Reading has no side effects unless `mark_seen=True` asks for one. Even
    then it is best-effort on the linked-account adapters: a Gmail account
    with only gmail.readonly cannot remove UNREAD, and that 403 used to fail
    the whole read.
    """
    acl.enforce(user_id, project, "collaborator")
    mb = _imap if _imap is not None else _make_mailbox(acl, project)
    msgs = list(mb.fetch(f"UID {id}", mark_seen=mark_seen))
    if not msgs:
        raise FileNotFoundError(f"message not found: {id!r}")
    return _msg_to_dict(msgs[0], full=True, inbox=_inbox_of(msgs[0], mb, _inbox_address(acl, project)))


def email_search(
    acl: Acl,
    user_id: str,
    project: str,
    query: str | None = None,
    limit: int = 50,
    *,
    since: str | None = None,
    until: str | None = None,
    from_addr: str | None = None,
    folder: str = "INBOX",
    _imap=None,
) -> list[dict]:
    """Mail search with optional date range and sender filter.

    Builds IMAP criteria (SINCE/BEFORE/FROM/TEXT). A real IMAP server gets
    them verbatim; the Gmail and Graph adapters translate them to their own
    query languages (connectors/adapters/mail_criteria.py) and refuse
    anything they can't translate rather than returning unfiltered mail.

    Returns [{id, subject, from, date, seen, inbox}]. When several sources
    (or, with folder="ALL", several IMAP folders) are searched and some
    fail, the others' hits are still returned, followed by one
    {warning, source_errors} row naming what failed.

    Args:
        query:     Text to match (omit for header-only searches). IMAP matches
                   it as a substring; Gmail as a Gmail search, so operators
                   like has:attachment work there.
        limit:     Max messages to return (default 50).
        since:     ISO date string YYYY-MM-DD — only messages on or after this date.
        until:     ISO date string YYYY-MM-DD — only messages before this date.
        from_addr: Sender address or domain to filter on (e.g. "anthropic.com").
        folder:    Mailbox folder to search (default "INBOX"; "ALL" searches
                   all mail, archived included: every folder except trash
                   and junk on an IMAP source, no label/folder filter on
                   Gmail and Graph).
    """
    acl.enforce(user_id, project, "collaborator")
    mb = _imap if _imap is not None else _make_mailbox(acl, project)
    mb.folder.set(folder)

    parts: list[str] = []
    if since:
        parts.append(f"SINCE {_to_imap_date(since)}")
    if until:
        parts.append(f"BEFORE {_to_imap_date(until)}")
    if from_addr:
        parts.append(f'FROM "{_imap_quote(from_addr)}"')
    if query:
        parts.append(f'TEXT "{_imap_quote(query)}"')
    criteria = " ".join(parts) if parts else "ALL"

    msgs = list(mb.fetch(criteria, mark_seen=False, limit=limit))
    fallback = _inbox_address(acl, project)
    return _with_source_errors([_msg_to_dict(m, inbox=_inbox_of(m, mb, fallback)) for m in msgs], mb)


# Logical name for the drafts folder. The REST adapters (Gmail API, Graph)
# map it to their own drafts label/folder; a real IMAP mailbox gets it
# replaced by resolve_drafts_folder() with the server's actual folder name.
DRAFTS = "Drafts"
_DRAFT_FLAG = "\\Draft"

# Used only when the server does not advertise SPECIAL-USE (RFC 6154).
# Gmail/Workspace IMAP localises the name ("[Gmail]/Utkast" for a Swedish
# account) but always flags it \Drafts, so the flag lookup is what normally
# wins; this list is the fallback for older servers.
_DRAFTS_FALLBACK_NAMES = (
    "[Gmail]/Drafts", "[Google Mail]/Drafts", "Drafts", "INBOX.Drafts", "INBOX/Drafts",
    "Draft", "Utkast", "[Gmail]/Utkast", "[Google Mail]/Utkast",
)


def resolve_drafts_folder(mb) -> str:
    """The name of `mb`'s drafts folder.

    Prefers the folder flagged with SPECIAL-USE `\\Drafts`, then a known
    name, then the logical DRAFTS. Adapters without `folder.list()` (the
    Gmail API / Graph adapters, MultiMailBackend) get DRAFTS and translate it
    themselves.
    """
    lister = getattr(getattr(mb, "folder", None), "list", None)
    if not callable(lister):
        return DRAFTS
    folders = list(lister())
    for f in folders:
        if any(str(flag).lower() == "\\drafts" for flag in (getattr(f, "flags", None) or ())):
            return f.name
    by_lower = {f.name.lower(): f.name for f in folders}
    for candidate in _DRAFTS_FALLBACK_NAMES:
        if candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    return DRAFTS


def _header(m, name: str) -> str:
    """First value of header `name` on a fetched message, whitespace-unfolded."""
    values = (getattr(m, "headers", None) or {}).get(name.lower()) or ()
    if isinstance(values, str):
        values = (values,)
    return " ".join(values[0].split()) if values else ""


def _resolve_reply_headers(mb, in_reply_to: str) -> tuple[str, str]:
    """Return (In-Reply-To, References) for a reply to `in_reply_to`.

    `in_reply_to` is either an RFC 5322 Message-ID ("<abc@host>", brackets
    optional) or a Memaix message id as returned by email_list/email_search
    (an IMAP UID, a Gmail/Graph message id, or "source-label|id" when the
    project has several mail sources). A Memaix id is looked up so the draft
    carries the original's real Message-ID — writing the Memaix id itself
    into the header would thread nothing.
    """
    value = in_reply_to.strip()
    if "|" not in value and "@" in value:
        msg_id = value if value.startswith("<") else f"<{value.strip('<>')}>"
        return msg_id, msg_id

    msgs = list(mb.fetch(f"UID {value}", mark_seen=False))
    if not msgs:
        raise FileNotFoundError(f"in_reply_to: message not found: {value!r}")
    msg_id = _header(msgs[0], "Message-ID")
    if not msg_id:
        raise ValueError(f"in_reply_to: message {value!r} has no Message-ID header, cannot thread a reply")
    references = _header(msgs[0], "References")
    return msg_id, f"{references} {msg_id}".strip()


class _DraftSource(NamedTuple):
    label: str  # registry label ("google_mail:jimmy@jimlov.se"), "" if unknown
    account: str  # the address email_list/email_search show as `inbox`
    adapter: Any
    linked: bool  # a per-user linked account, not the acl.yaml mailbox


def _draft_sources(acl: Acl, project: str, mb) -> list[_DraftSource]:
    """Every source behind `mb` that a draft can be saved in.

    `account` is the same address email_list/email_search put in a message's
    `inbox` field: a linked account's own address, or the acl.yaml mailbox
    user for the project's shared mailbox. A backend that is not a
    MultiMailBackend is a single source whose label is unknown.
    """
    from ..connectors.adapters.mail_multi import source_address

    fallback = _inbox_address(acl, project)
    sources = getattr(mb, "sources", None)
    if isinstance(sources, list) and sources:
        out = []
        for label, adapter in sources:
            address = source_address(label)
            out.append(_DraftSource(label, address or fallback, adapter, bool(address)))
        return out
    linked_address = getattr(mb, "inbox_address", None)
    if isinstance(linked_address, str) and linked_address:
        return [_DraftSource("", linked_address, mb, True)]
    return [_DraftSource("", fallback, mb, False)]


def _matches_account(src: _DraftSource, wanted: str) -> bool:
    """True if `wanted` is `src`'s address (case-insensitive) or its full label."""
    if src.account and src.account.lower() == wanted.lower():
        return True
    return bool(src.label) and src.label == wanted


def _source_for_account(project: str, sources: list[_DraftSource], wanted: str) -> _DraftSource:
    """The source matching an explicit `account`, or ValueError naming the valid ones."""
    for src in sources:
        if _matches_account(src, wanted):
            return src
    valid = sorted({src.account for src in sources if src.account})
    listed = ", ".join(valid) if valid else "(none)"
    raise ValueError(f"unknown account {wanted!r} for project {project!r}; valid accounts: {listed}")


def _source_for_reply(sources: list[_DraftSource], in_reply_to: str | None) -> _DraftSource | None:
    """The source named by a "label|id" `in_reply_to`, or None."""
    if not in_reply_to or "|" not in in_reply_to:
        return None
    label = in_reply_to.strip().partition("|")[0]
    return next((src for src in sources if src.label and src.label == label), None)


def _pick_draft_source(
    project: str, sources: list[_DraftSource], account: str | None, in_reply_to: str | None,
) -> _DraftSource | None:
    """The source a draft goes to, or None for the default path (unchanged:
    the backend's own append, i.e. the project's acl.yaml mailbox first).

    An explicit `account` must match a source's address (case-insensitive)
    or its full label; anything else is an error naming the valid accounts.
    Without one, a reply to a message id carrying a source label
    ("google_mail:jimmy@jimlov.se|18f2c...") goes to that source, so the
    draft lands in the same mailbox as the thread.
    """
    wanted = (account or "").strip()
    if wanted:
        return _source_for_account(project, sources, wanted)
    return _source_for_reply(sources, in_reply_to)


def _build_draft_message(
    mb, to: str, subject: str, body: str, sender: str, cc: str | None, in_reply_to: str | None,
) -> EmailMessage:
    """The draft as an EmailMessage; `sender` "" leaves From out entirely."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if sender:
        msg["From"] = sender
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        # Resolved against the whole backend: it routes a "label|id" to the
        # source that owns it, whichever source the draft goes to.
        reply_to_id, references = _resolve_reply_headers(mb, in_reply_to)
        msg["In-Reply-To"] = reply_to_id
        msg["References"] = references
    msg.set_content(body)
    return msg


def email_create_draft(
    acl: Acl,
    user_id: str,
    project: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    in_reply_to: str | None = None,
    account: str | None = None,
    *,
    _imap=None,
) -> dict:
    """Save a draft: IMAP APPEND to Drafts with the \\Draft flag, or a
    Gmail/Graph draft when the chosen mailbox is a linked account.

    `account` (optional) picks the mailbox when the project has several: a
    linked account's address (as in the `inbox` field of email_list /
    email_search) or the project's own acl.yaml mailbox address. Omitted,
    the draft goes where it always has (the project's own mailbox) — unless
    `in_reply_to` is a message id from a specific source ("label|id"), in
    which case it goes to that source so the reply sits in the thread's
    mailbox. An unknown account raises ValueError listing the valid ones.

    `in_reply_to` (optional) threads the draft as a reply: a Memaix message
    id or a Message-ID, see _resolve_reply_headers.

    Returns {status, subject, account}; `account` is the mailbox the draft
    was saved in.
    """
    acl.enforce(user_id, project, "collaborator")
    mb = _imap if _imap is not None else _make_mailbox(acl, project)
    sources = _draft_sources(acl, project, mb)
    target = _pick_draft_source(project, sources, account, in_reply_to)

    # Omitted, not blanked, when the source is a linked account rather than
    # an acl.yaml mailbox: Gmail and Graph both stamp the authenticated
    # account's own address on a draft that arrives without a From header,
    # but an empty `From:` is a malformed header, not an absent one. A draft
    # for a chosen linked account never carries the acl.yaml address.
    sender = "" if target is not None and target.linked else _inbox_address(acl, project)
    msg = _build_draft_message(mb, to, subject, body, sender, cc, in_reply_to)
    # No explicit target: the backend's own append (acl.yaml mailbox first).
    dest, saved_in = (mb, sources[0].account) if target is None else (target.adapter, target.account)
    dest.append(msg.as_bytes(), folder=resolve_drafts_folder(dest), flag_set=(_DRAFT_FLAG,))
    return {"status": "draft_created", "subject": subject, "account": saved_in}


def email_send(
    acl: Acl,
    user_id: str,
    project: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    *,
    attachment_filename: str | None = None,
    attachment_content: bytes | None = None,
    attachment_mimetype: str = "text/calendar",
    _smtp=None,
    _confirmed: bool = False,
    _outbox=None,
    _cfg: dict | None = None,
) -> dict:
    """Send a message via SMTP.  Requires owner + allow_send feature flag.

    Queued for approval instead of sent when the outbox policy resolves to
    'review' (see module docstring) — unless _confirmed=True. The outbox
    preview only ever shows {to, subject, body, cc}; an attachment is never
    queued for review, so callers passing one should also pass
    _confirmed=True (system-generated attachments, e.g. booking .ics files,
    aren't composed by the LLM and don't need a human approval step).
    """
    acl.enforce(user_id, project, "owner")
    # Feature gate
    if not acl.resource(project, "allow_send"):
        raise RuntimeError("feature_disabled: allow_send is false")

    if not _confirmed:
        from ..outbox.policy import action_mode
        from ..outbox.preview import render_preview
        from ..outbox.queue import default_queue

        args = {"to": to, "subject": subject, "body": body, "cc": cc}
        memaix_cfg = _cfg if _cfg is not None else config.load()
        if action_mode(memaix_cfg, acl, project, "email_send", args) == "review":
            queue = _outbox if _outbox is not None else default_queue()
            action_id = queue.enqueue(
                user_id, project, "email_send", args, render_preview("email_send", args)
            )
            return {
                "pending": True,
                "action_id": action_id,
                "note": "Väntar på godkännande i utkorgen",
            }

    cfg = _mailbox_cfg(acl, project)
    smtp_cfg: dict = acl.resource(project, "smtp") or {}

    from_addr = smtp_cfg.get("from_addr") or cfg.get("user", "")
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg["From"] = from_addr
    if cc:
        msg["Cc"] = cc
    msg.set_content(body)
    if attachment_content is not None and attachment_filename:
        maintype, _, subtype = attachment_mimetype.partition("/")
        msg.add_attachment(
            attachment_content,
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=attachment_filename,
        )

    if _smtp is not None:
        _smtp.send_message(msg)
    else:
        host = smtp_cfg.get("host", cfg.get("host", "localhost"))
        port = int(smtp_cfg.get("port", 587))
        user = smtp_cfg.get("user") or cfg.get("user", "")
        password_ref = smtp_cfg.get("password_ref") or cfg.get("password_ref")
        password = config.secret(password_ref)
        if password is None:
            raise ValueError(f"no password configured for mailbox (project {project!r})")
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)

    return {"status": "sent", "to": to, "subject": subject}
