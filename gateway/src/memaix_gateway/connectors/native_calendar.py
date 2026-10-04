# SPDX-License-Identifier: AGPL-3.0-or-later
"""Memaix' own calendar for users who have no external one.

Events live in one JSON file per (project, user) in the project vault, the
same convention as working_hours.py. It implements the same adapter surface
as the Google/CalDAV adapters (list_events/find_events/create_event/
update_event/delete_event) so the booking flow can write confirmed
bookings to it and read them back as busy time. Time blocks are not events:
they belong to the schedule (calendar_schedule_set).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from .aggregate import to_utc

NATIVE_PROVIDER = "native"
_FILE_LOCK = threading.Lock()


def native_path(acl, project: str, user: str) -> Path:
    vault = acl.resource(project, "vault")
    if not vault:
        raise ValueError(f"project {project!r} has no vault configured")
    directory = Path(vault) / "native_calendar"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user}.json"


class NativeCalendarAdapter:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text())

    def _save(self, events: list[dict]) -> None:
        self._path.write_text(json.dumps(events))

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        lo, hi = to_utc(start), to_utc(end)
        with _FILE_LOCK:
            events = self._load()
        hits = [e for e in events if to_utc(e["start"]) < hi and to_utc(e["end"]) > lo]
        return sorted(hits, key=lambda e: e["start"])

    find_events = list_events

    def create_event(
        self, uid: str, title: str, start: datetime, end: datetime,
        attendees: list[str] | None = None, location: str | None = None,
        description: str | None = None, want_conference: bool = False,
    ) -> dict:
        event = {
            "id": uid, "title": title, "start": start.isoformat(), "end": end.isoformat(),
            "location": location or "", "description": description or "",
            "attendees": attendees or [], "series_id": None, "is_exception": False,
            "source_busy": True, "meet_url": None,
        }
        with _FILE_LOCK:
            events = self._load()
            events.append(event)
            self._save(events)
        return event

    def update_event(self, id: str, **fields) -> dict:
        with _FILE_LOCK:
            events = self._load()
            for event in events:
                if event["id"] == id:
                    event.update({k: v for k, v in fields.items() if k in ("title", "start", "end", "location", "description")})
                    self._save(events)
                    return event
        raise KeyError(f"no such event: {id}")

    def delete_event(self, id: str) -> None:
        with _FILE_LOCK:
            events = self._load()
            kept = [e for e in events if e["id"] != id]
            if len(kept) == len(events):
                raise KeyError(f"no such event: {id}")
            self._save(kept)
