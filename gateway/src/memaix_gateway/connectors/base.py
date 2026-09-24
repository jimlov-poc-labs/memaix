# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capability protocols for the connector framework (FEATURE-CONNECTOR-FRAMEWORK.md §4).

MailBackend and CalendarBackend intentionally mirror the *_imap*/*_dav* duck
types tools/email.py and tools/calendar.py already accept today (see their
module docstrings) — the point of the registry is to build one of these
objects from project config, not to redesign what the tools call.
Files/Contacts/Tasks are implemented by the Nextcloud adapters
(FEATURE-NEXTCLOUD-BACKEND.md) and wired to the nc_files_*/contacts_*/
nc_tasks_* MCP tools. Chat/Issue have no adapter yet; they exist so a
future one (Nextcloud Talk, Deck) has a documented shape to target.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class MailBackend(Protocol):
    """Mirrors the `_imap` duck type in tools/email.py."""

    def fetch(self, criteria: str = "ALL", *, mark_seen: bool = False, limit: int | None = None): ...
    # Exactly imap_tools.MailBox.append — a real MailBox is a MailBackend.
    def append(self, message: bytes, folder: str = "INBOX", dt=None, flag_set=None): ...
    def logout(self) -> None: ...


@runtime_checkable
class CalendarBackend(Protocol):
    """Mirrors the `_dav` duck type in tools/calendar.py."""

    def list_events(self, start: datetime, end: datetime) -> list[dict]: ...
    def find_events(self, start: datetime, end: datetime) -> list[dict]: ...
    def create_event(
        self, uid: str, title: str, start: datetime, end: datetime,
        attendees: list[str] | None = None, location: str | None = None,
        description: str | None = None,
    ) -> dict: ...
    def update_event(self, id: str, **fields) -> dict: ...
    def delete_event(self, id: str) -> None: ...


@runtime_checkable
class FilesBackend(Protocol):
    """Implemented by connectors/adapters/files_webdav.py; consumed by the
    nc_files_* MCP tools — a source *additional* to the local vault, not a
    replacement for it (see connectors/registry.py's RESOURCE_KEYS note)."""

    def list_files(self, path: str) -> list[dict]: ...
    def read_file(self, path: str) -> str: ...
    def write_file(self, path: str, content: str) -> str: ...
    def search_files(self, query: str, path: str) -> list[dict]: ...


@runtime_checkable
class ContactsBackend(Protocol):
    """Implemented by connectors/adapters/contacts_carddav.py; consumed by
    the contacts_search/contacts_get MCP tools."""

    def search(self, query: str) -> list[dict]: ...
    def get(self, id: str) -> dict: ...


@runtime_checkable
class TasksBackend(Protocol):
    """Implemented by connectors/adapters/tasks_caldav.py (CalDAV VTODO);
    consumed by the nc_tasks_* MCP tools."""

    def list(self) -> list[dict]: ...
    def add(self, title: str, due: str | None = None, notes: str | None = None) -> dict: ...
    def complete(self, id: str) -> dict: ...


@runtime_checkable
class ChatBackend(Protocol):
    """No MCP tool consumes this yet — a future `chat_post`/`chat_read`."""

    def post(self, channel: str, text: str) -> dict: ...
    def list_messages(self, channel: str, since: datetime) -> list[dict]: ...


@runtime_checkable
class IssueBackend(Protocol):
    """No MCP tool consumes this yet — a future `issue_*` synced with the backlog."""

    def list(self, query: dict) -> list[dict]: ...
    def create(self, item: dict) -> dict: ...
    def update(self, id: str, **fields) -> dict: ...
