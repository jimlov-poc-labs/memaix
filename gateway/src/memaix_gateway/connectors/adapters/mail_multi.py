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

from typing import Iterable, Protocol, cast


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
        self._backend._folder = name
        for _, adapter in self._backend._sources:
            adapter.folder.set(name)


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
    already contain colons, so splitting on the LAST "|" (never appears in
    a label or a real IMAP UID) is unambiguous where splitting on ":" would
    not be.
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

    @property
    def folder(self) -> _MultiMailFolderProxy:
        return _MultiMailFolderProxy(self)

    def _labeled(self, label: str, msgs):
        for m in msgs:
            m.uid = f"{label}{self._SEP}{m.uid}"
            yield m

    def fetch(self, criteria: str = "ALL", *, mark_seen: bool = False, limit: int | None = None):
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
            label, _, real_uid = wanted.rpartition(self._SEP)
            for src_label, adapter in self._sources:
                if src_label == label:
                    result = list(adapter.fetch(f"UID {real_uid}", mark_seen=mark_seen))
                    return list(self._labeled(label, result))
            return []

        # ALL / BODY "..." / date-range criteria — fan out to every source and
        # merge. `limit` is applied per-source AND to the merged result so a
        # single noisy mailbox can't crowd out the others while still
        # respecting the caller's overall cap.
        merged = []
        for label, adapter in self._sources:
            result = list(adapter.fetch(criteria, mark_seen=mark_seen, limit=limit))
            merged.extend(self._labeled(label, result))
        if limit:
            merged = merged[:limit]
        return merged

    def append(self, message: bytes, folder: str = "INBOX", dt=None, flag_set=None):
        """Drafts are appended to the FIRST source only (matches today's
        single-mailbox semantics: email_create_draft has always saved to
        exactly one mailbox's Drafts folder — with multiple sources now
        possible, "the first/primary one" is the least-surprising default
        until a project explicitly needs to choose).

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
