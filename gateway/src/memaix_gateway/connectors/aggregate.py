# SPDX-License-Identifier: AGPL-3.0-or-later
"""Busy/free aggregation across multiple CalendarBackend adapters.

memaix-src backlog card 4daa20e2 ("Kalenderaggregering per person/projekt"):
source-agnostic union of busy intervals from any number of calendar
adapters resolved via connectors.registry.get_all(). This module is pure —
no I/O, no scheduling — it is consumed by connectors/calendar_cache.py's
periodic sync, never called live per-request (see that module's docstring
for why: no realtime requirement, decided on the card 2026-09-04).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True, order=True)
class BusyInterval:
    """A half-open [start, end) busy block. start/end are always tz-aware
    UTC datetimes after construction via to_utc(); source is the adapter
    label that produced it, purely informational."""

    start: datetime
    end: datetime
    source: str = ""


@dataclass(frozen=True)
class CalendarEvent:
    """One source event, identity preserved — memaix-src backlog card
    c7698ff3. Unlike BusyInterval (post-merge, identity-less), this is what
    lets a per-event override be resolved: uid/series_id are provider-native
    ids, scoped to *source* (two different sources can reuse the same id)."""

    uid: str
    start: datetime
    end: datetime
    source: str = ""
    title: str = ""
    series_id: str | None = None
    is_exception: bool = False
    source_busy: bool = True
    # False when uid is a synthesized per-sync fallback (the adapter gave no
    # real id) — an override set against it could silently drift onto a
    # different event on the next sync if the source reorders its results.
    # calendar_events_list surfaces this as overridable=False.
    stable_id: bool = True


class CalendarSourceError(Exception):
    """One calendar source failed to answer. Carries the source label so
    the sync layer can record which source failed without losing the
    others' results (FEATURE-CONNECTOR-FRAMEWORK.md §8, feltålighet)."""

    def __init__(self, label: str, cause: Exception) -> None:
        self.label = label
        self.cause = cause
        super().__init__(f"calendar source {label!r} failed: {cause}")


def to_utc(value: datetime | str) -> datetime:
    """Normalise a datetime or ISO-8601 string to a tz-aware UTC datetime.
    Naive input is assumed UTC (matches _ICalAdapter._in_range's existing
    convention in tools/calendar.py)."""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def busy_from_backend(backend, label: str, start: datetime, end: datetime) -> list[BusyInterval]:
    """Query one CalendarBackend (the existing list_events duck-type from
    tools/calendar.py) and normalise its events into busy intervals.

    Raises CalendarSourceError on any failure instead of letting the
    adapter's raw exception (requests.HTTPError, etc.) propagate — the
    caller decides whether a single source's failure should stop the sync.
    A malformed individual event (missing/unparseable start or end) is
    skipped rather than failing the whole source."""
    try:
        events = backend.list_events(start, end)
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad, see docstring.
        raise CalendarSourceError(label, exc) from exc

    out: list[BusyInterval] = []
    for ev in events:
        try:
            out.append(BusyInterval(to_utc(ev["start"]), to_utc(ev["end"]), label))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def events_from_backend(backend, label: str, start: datetime, end: datetime) -> list[CalendarEvent]:
    """Like busy_from_backend, but preserves per-event identity instead of
    collapsing straight to a BusyInterval. Adapters emit the new
    series_id/is_exception/source_busy/title/id keys additively. An event
    missing a real id still counts as busy (same as busy_from_backend always
    did) — it gets a synthetic per-sync uid, just not one a click-to-override
    UI can target reliably across syncs. A malformed event (missing/
    unparseable start or end) is skipped, same as busy_from_backend."""
    try:
        events = backend.list_events(start, end)
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad, see busy_from_backend.
        raise CalendarSourceError(label, exc) from exc

    out: list[CalendarEvent] = []
    for i, ev in enumerate(events):
        try:
            real_id = ev.get("id")
            uid = real_id or f"{label}-{i}"
            out.append(
                CalendarEvent(
                    uid=str(uid),
                    start=to_utc(ev["start"]),
                    end=to_utc(ev["end"]),
                    source=label,
                    title=ev.get("title", ""),
                    series_id=ev.get("series_id"),
                    is_exception=bool(ev.get("is_exception", False)),
                    source_busy=bool(ev.get("source_busy", True)),
                    stable_id=bool(real_id),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def merge_busy(intervals: list[BusyInterval]) -> list[BusyInterval]:
    """Union + coalesce busy intervals from any number of sources into the
    minimal set of maximal non-overlapping blocks. any-source-busy == busy."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda b: b.start)
    merged = [ordered[0]]
    for iv in ordered[1:]:
        last = merged[-1]
        if iv.start <= last.end:
            if iv.end > last.end:
                merged[-1] = BusyInterval(last.start, iv.end, last.source)
        else:
            merged.append(iv)
    return merged


def free_slots(busy: list[BusyInterval], start: datetime | str, end: datetime | str, duration: timedelta) -> list[dict]:
    """Complement of *busy* within [start, end), keeping only gaps >=
    duration. Same {start, end} ISO-8601 shape tools/calendar.py's existing
    calendar_find_free already returns."""
    start_dt, end_dt = to_utc(start), to_utc(end)
    free: list[dict] = []
    cursor = start_dt
    for iv in sorted(busy, key=lambda b: b.start):
        if iv.start > cursor + duration:
            free.append({"start": cursor.isoformat(), "end": iv.start.isoformat()})
        if iv.end > cursor:
            cursor = iv.end
    if end_dt > cursor + duration:
        free.append({"start": cursor.isoformat(), "end": end_dt.isoformat()})
    return free
