# SPDX-License-Identifier: AGPL-3.0-or-later
"""`folder="ALL"` for a real IMAP mailbox.

The Gmail and Graph adapters read the folder name "ALL" as "the whole
mailbox, archived mail included" (#125). A real IMAP server has no such
folder: `imap_tools.MailBox.folder.set("ALL")` issues `SELECT "ALL"` and
the server answers `NO ... No such mailbox`. With a Gmail source and an
IMAP source merged by MultiMailBackend, that one refusal failed the whole
`email_search(folder="ALL")` (live 2026-09-24, booking@memaix.se on
Purelymail).

`AllFoldersMailBox` wraps a logged-in `imap_tools.MailBox` and gives
"ALL" the same meaning here: search every selectable folder the server
LISTs and merge the hits newest first. Everything else is passed straight
through, so for any other folder name it behaves exactly like the MailBox
it wraps.

Which folders "ALL" covers:
  - A folder flagged SPECIAL-USE `\\All` (RFC 6154) already holds every
    message; when the server has one, only that folder is searched, so a
    message is not reported once per folder it is filed in.
  - Otherwise every folder except `\\Noselect`/`\\NonExistent` ones (they
    cannot be SELECTed) and trash/junk (`\\Trash`/`\\Junk`, or the usual
    names when the server sets no SPECIAL-USE flags). That matches what
    "ALL" means on the Gmail source, whose unlabelled search leaves out
    Spam and Trash too.

A message's id in "ALL" mode is `"{folder}|{uid}"`: an IMAP UID is only
unique within its folder, and `email_read` has to know which folder to
SELECT before it can fetch the UID. `fetch("UID {folder}|{uid}")` does
that routing whether or not "ALL" is currently selected.

A folder that fails mid-search (it vanished between LIST and SELECT, the
server refused it) does not fail the others; it is reported in
`source_errors`, which tools/email.py turns into a warning in the result.
"""

from __future__ import annotations

from typing import Any

from .mail_multi import message_time

ALL_FOLDERS = "ALL"
_SEP = "|"

_UNSELECTABLE = {"\\noselect", "\\nonexistent"}
_SKIPPED_SPECIAL_USE = {"\\trash", "\\junk"}
# Trash/junk folder names on servers that don't advertise SPECIAL-USE.
# Compared against the last path segment, lower-cased.
_SKIPPED_NAMES = {
    "trash", "deleted", "deleted items", "deleted messages", "bin",
    "junk", "junk e-mail", "junk email", "spam", "bulk mail",
    "papperskorg", "papperskorgen", "skräppost",
}


def _flags(folder_info) -> set[str]:
    return {str(f).lower() for f in (getattr(folder_info, "flags", None) or ())}


def _leaf(folder_info) -> str:
    name = folder_info.name
    delim = getattr(folder_info, "delim", None) or "/"
    return name.rsplit(delim, 1)[-1].strip().lower()


def search_folders(folders) -> list[str]:
    """The folder names "ALL" searches, given `folder.list()`'s output."""
    selectable = [f for f in folders if not (_flags(f) & _UNSELECTABLE)]
    for f in selectable:
        if "\\all" in _flags(f):
            return [f.name]
    return [
        f.name for f in selectable
        if not (_flags(f) & _SKIPPED_SPECIAL_USE) and _leaf(f) not in _SKIPPED_NAMES
    ]


class _FolderProxy:
    """`mb.folder` — intercepts `set("ALL")`, forwards everything else
    (`list`, `get`, ...) to the wrapped MailBox's folder manager."""

    def __init__(self, owner: "AllFoldersMailBox") -> None:
        self._owner = owner

    def set(self, name: str, *args: Any, **kwargs: Any):
        if str(name).strip().upper() == ALL_FOLDERS:
            self._owner._all = True
            return None
        self._owner._all = False
        return self._owner._mb.folder.set(name, *args, **kwargs)

    def __getattr__(self, attr: str):
        return getattr(self._owner._mb.folder, attr)


class AllFoldersMailBox:
    """A logged-in imap_tools.MailBox that also understands `folder="ALL"`."""

    def __init__(self, mailbox: Any) -> None:
        self._mb = mailbox
        self._all = False
        self.source_errors: list[dict] = []

    @property
    def folder(self) -> _FolderProxy:
        return _FolderProxy(self)

    def __getattr__(self, attr: str):
        # login/logout/append/client/... — only reached for names this class
        # doesn't define itself.
        return getattr(self._mb, attr)

    def fetch(self, criteria: str = "ALL", *args: Any, mark_seen: bool = False, limit: int | None = None, **kwargs: Any):
        self.source_errors = []
        if isinstance(criteria, str) and criteria.startswith("UID ") and _SEP in criteria:
            return self._fetch_in_folder(criteria[len("UID "):], mark_seen=mark_seen)
        if not self._all:
            return self._mb.fetch(criteria, *args, mark_seen=mark_seen, limit=limit, **kwargs)
        return self._search_all(criteria, mark_seen=mark_seen, limit=limit)

    def _fetch_in_folder(self, wanted: str, *, mark_seen: bool) -> list:
        folder, _, uid = wanted.rpartition(_SEP)
        if not uid.isdigit():
            raise ValueError(f"invalid message id: {wanted!r}")
        known = {f.name for f in self._mb.folder.list()}
        if folder not in known:
            raise FileNotFoundError(f"message not found: {wanted!r}")
        self._mb.folder.set(folder)
        msgs = list(self._mb.fetch(f"UID {uid}", mark_seen=mark_seen))
        for m in msgs:
            m.uid = f"{folder}{_SEP}{m.uid}"
        return msgs

    def _search_all(self, criteria: str, *, mark_seen: bool, limit: int | None) -> list:
        folders = search_folders(self._mb.folder.list())
        merged: list = []
        errors: list[dict] = []
        first_exc: Exception | None = None
        for name in folders:
            try:
                self._mb.folder.set(name)
                # Newest first per folder: the merge below keeps the newest
                # `limit` overall, so each folder must offer its newest.
                msgs = list(self._mb.fetch(criteria, mark_seen=mark_seen, limit=limit, reverse=True))
            except Exception as exc:  # noqa: BLE001 - one folder must not fail the rest; reported below
                first_exc = first_exc or exc
                errors.append({"source": f"folder:{name}", "error": str(exc) or type(exc).__name__})
                continue
            for m in msgs:
                m.uid = f"{name}{_SEP}{m.uid}"
            merged.extend(msgs)
        if first_exc is not None and len(errors) == len(folders):
            raise first_exc  # nothing searched at all — that is a failure, not a partial result
        self.source_errors = errors
        merged.sort(key=message_time, reverse=True)
        return merged[:limit] if limit else merged


def wrap(mailbox: Any) -> AllFoldersMailBox:
    """Wrap `mailbox` once; wrapping an already wrapped one is a no-op."""
    return mailbox if isinstance(mailbox, AllFoldersMailBox) else AllFoldersMailBox(mailbox)
