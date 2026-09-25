# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-account mail aggregator (FEATURE-CONNECTOR-FRAMEWORK.md — IMAP
per-user, multiple mail sources per user).

server.py's `_mail_backend` used to resolve a single adapter via
`registry.get(..., "mail", user)`. Now that a user can have more than one
mail source at once (a project's shared IMAP mailbox AND their own linked
per-user IMAP mailbox, or several linked per-user mailboxes), the resolved
backend has to fan `email_list`/`email_read`/`email_search` out over every
source `registry.get_all(..., "mail", user)` returns and merge the results.

Degenerate case (today's overwhelmingly common one — a single shared IMAP
mailbox, or a single linked Microsoft account): server.py's `_mail_backend`
never even constructs this class — it hands back `sources[0][1]` directly,
so `tools/email.py` calls the real adapter with zero indirection, byte-
identical to pre-multi-account behavior. This class only exists, and is
only ever used, when there are 2+ sources.

`email_send` (SMTP) is untouched — it isn't mail-capability-routed at all,
still resolves `_mailbox_cfg`/SMTP config directly in tools/email.py.
"""

from __future__ import annotations

import datetime
from email.utils import parsedate_to_datetime
from typing import Iterable, Protocol, cast

_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def source_address(label: str) -> str:
    """The mailbox address a source label names, or "" if it names none.

    Per-user sources are labelled "{type}:{account}" by registry.get_all
    ("google_mail:jimmy@jimlov.se"); the account IS the inbox address. A
    shared acl.yaml source is "{type}:{project}" and has no address of its
    own here — tools/email.py falls back to the acl.yaml mailbox user.
    """
    _, _, account = label.partition(":")
    return account if "@" in account else ""


def _as_aware(dt: datetime.datetime) -> datetime.datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def message_time(m) -> datetime.datetime:
    """Best-effort timestamp of a fetched message, for ordering a merge.

    imap_tools messages carry a parsed `.date`; the REST adapters only have
    `date_str` — an RFC 2822 Date header (Gmail), ISO 8601 (Graph) or epoch
    milliseconds (Gmail's internalDate fallback). Unparseable sorts last.
    """
    date = getattr(m, "date", None)
    if isinstance(date, datetime.datetime) and date.year > 1900:
        return _as_aware(date)
    raw = str(getattr(m, "date_str", "") or "").strip()
    if not raw:
        return _EPOCH
    if raw.isdigit():
        return datetime.datetime.fromtimestamp(int(raw) / 1000, tz=datetime.timezone.utc)
    try:
        return _as_aware(parsedate_to_datetime(raw))
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return _as_aware(datetime.datetime.fromisoformat(raw))
    except ValueError:
        return _EPOCH


class _MailSource(Protocol):
    """The subset of the `_imap` duck type (base.py MailBackend + the
    `.folder.set` proxy tools/email.py calls) that this aggregator fans
    out over. Every registered mail adapter (imap_user, the shared IMAP
    connector, mail_microsoft) already satisfies it."""

    @property
    def folder(self) -> "_FolderSetter": ...
    def fetch(self, criteria: str = "ALL", *, mark_seen: bool = False, limit: int | None = None) -> Iterable: ...
    def append(self, message: bytes, folder: str = "INBOX", dt=None, flag_set=None): ...
    def logout(self) -> None: ...


class _FolderSetter(Protocol):
    def set(self, name: str) -> None: ...


class _MultiMailFolderProxy:
    def __init__(self, backend: "MultiMailBackend") -> None:
        self._backend = backend

    def set(self, name: str) -> None:
        """Select `name` on every source. A source that refuses it (an IMAP
        server without that folder) is remembered, not raised: the next
        fetch reports it and searches the sources that accepted it."""
        self._backend._folder = name
        self._backend._set_errors = {}
        for label, adapter in self._backend._sources:
            try:
                adapter.folder.set(name)
            except Exception as exc:  # noqa: BLE001 - reported by the next fetch, see MultiMailBackend.fetch
                self._backend._set_errors[label] = exc


class MultiMailBackend:
    """Fan `fetch`/`append`/`logout` out over every (label, adapter) pair
    from `registry.get_all(..., "mail", user)`.

    Message UIDs are ambiguous across sources (two IMAP mailboxes can both
    have a message with uid "17"), so every message this backend returns
    has its `.uid` rewritten to `"{label}|{original_uid}"` — the label
    prefix `tools/email.py`'s `email_read` then uses to route a `UID {id}`
    fetch back to the one source that owns it. "|" (not ":") is the
    separator on purpose: source labels are `"{type}:{account_email}"`
    (registry.get_all's convention, e.g. "imap_user:alice@work.com") and
    already contain colons, so splitting on the FIRST "|" (never appears in
    a label) is unambiguous where splitting on ":" would not be. What
    follows it is the source's own id, which may itself contain "|"
    (AllFoldersMailBox's "{folder}|{uid}" in folder="ALL" mode).

    A source that fails (refuses the folder, errors on fetch) does not fail
    the merged search; see `fetch` and `source_errors`.
    """

    _SEP = "|"

    def __init__(self, sources: list[tuple[str, object]]) -> None:
        if not sources:
            raise ValueError("MultiMailBackend requires at least one source")
        # get_all() types adapters as `object` (it can't know the capability
        # at return time); every adapter registered under the "mail"
        # capability satisfies _MailSource by construction. Narrow once here
        # so the fan-out below is checked against the real duck type.
        self._sources: list[tuple[str, _MailSource]] = [
            (label, cast(_MailSource, adapter)) for label, adapter in sources
        ]
        self._folder = "INBOX"
        # Per-source failures: folder.set's are held until the next fetch,
        # which reports them (with its own) in source_errors.
        self._set_errors: dict[str, Exception] = {}
        self.source_errors: list[dict] = []

    @property
    def folder(self) -> _MultiMailFolderProxy:
        return _MultiMailFolderProxy(self)

    @property
    def sources(self) -> list[tuple[str, _MailSource]]:
        """Every (label, adapter) pair, in registry order — the acl.yaml
        mailbox first when the project has one. email_create_draft uses this
        to put a draft in one chosen source instead of the first."""
        return list(self._sources)

    def _labeled(self, label: str, msgs):
        address = source_address(label)
        for m in msgs:
            m.uid = f"{label}{self._SEP}{m.uid}"
            if address:
                # Which inbox this came from — per source, not the project's
                # acl.yaml mailbox stamped on everything (tools/email.py).
                m.inbox = address
            yield m

    def fetch(self, criteria: str = "ALL", *, mark_seen: bool = False, limit: int | None = None):
        self.source_errors = []
        if criteria.startswith("UID "):
            wanted = criteria[len("UID "):]
            if self._SEP not in wanted:
                # No label prefix — can't tell which source owns it. Ask every
                # source; first match wins (mirrors "search everywhere" rather
                # than silently returning nothing).
                for label, adapter in self._sources:
                    result = list(adapter.fetch(f"UID {wanted}", mark_seen=mark_seen))
                    if result:
                        return list(self._labeled(label, result))
                return []
            # The FIRST "|": a label never contains one, but the source's own
            # id may (AllFoldersMailBox ids are "{folder}|{uid}").
            label, _, real_uid = wanted.partition(self._SEP)
            for src_label, adapter in self._sources:
                if src_label == label:
                    result = list(adapter.fetch(f"UID {real_uid}", mark_seen=mark_seen))
                    return list(self._labeled(label, result))
            return []

        # Search/list criteria — fan out to every source and merge. Each
        # source returns up to `limit`, and the merge is ordered newest first
        # BEFORE it is cut to `limit`: concatenating and slicing would let
        # whichever source came first fill the whole result and crowd every
        # other mailbox's matches out, however much newer they were.
        #
        # One failing source does not fail the others: its error goes into
        # `source_errors` (tools/email.py turns that into a warning next to
        # the hits). Only when EVERY source failed is the first error raised.
        merged = []
        errors: list[dict] = []
        first_exc: Exception | None = None
        failed = 0
        for label, adapter in self._sources:
            try:
                set_exc = self._set_errors.get(label)
                if set_exc is not None:
                    raise set_exc
                result = list(adapter.fetch(criteria, mark_seen=mark_seen, limit=limit))
            except Exception as exc:  # noqa: BLE001 - reported in source_errors, raised if nothing worked
                first_exc = first_exc or exc
                failed += 1
                errors.append({"source": label, "error": str(exc) or type(exc).__name__})
                continue
            # A source may itself be partial (AllFoldersMailBox: one folder failed).
            for err in getattr(adapter, "source_errors", None) or []:
                errors.append({"source": f"{label} {err.get('source', '')}".strip(), "error": err.get("error", "")})
            merged.extend(self._labeled(label, result))
        if first_exc is not None and failed == len(self._sources):
            raise first_exc
        self.source_errors = errors
        merged.sort(key=message_time, reverse=True)
        if limit:
            merged = merged[:limit]
        return merged

    def append(self, message: bytes, folder: str = "INBOX", dt=None, flag_set=None):
        """Drafts are appended to the FIRST source only (matches today's
        single-mailbox semantics: email_create_draft has always saved to
        exactly one mailbox's Drafts folder — with multiple sources now
        possible, "the first/primary one" is the least-surprising default).
        A caller that has chosen a source (email_create_draft's `account`,
        or a reply to a message from a linked source) appends to that
        source's adapter directly via `sources`.

        This backend has no `folder.list()`, so email_create_draft hands it
        the logical "Drafts"; it is resolved against the source that
        actually receives the message (e.g. "[Gmail]/Drafts" on Gmail IMAP).
        """
        from ...tools.email import DRAFTS, resolve_drafts_folder

        label, adapter = self._sources[0]
        if folder == DRAFTS:
            folder = resolve_drafts_folder(adapter)
        return adapter.append(message, folder=folder, dt=dt, flag_set=flag_set)

    def logout(self) -> None:
        for _, adapter in self._sources:
            logout = getattr(adapter, "logout", None)
            if logout is not None:
                logout()
