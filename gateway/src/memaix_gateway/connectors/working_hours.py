# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-user bookable working hours — memaix-src backlog card e21fde31.

A recurring weekly schedule of which local times are open for booking at
all, independent of the calendar's busy/free truth (calendar_free_busy /
EventOverrideStore / OverrideStore). This module only ever *subtracts* from
already-free time — it never marks a busy block free. That single direction
is what keeps this concern from ever colliding with a forced-busy event
override from card c7698ff3: a busy interval is never even offered to this
filter, since it operates on free_slots() output, not on the busy list.

No stored schedule (or an unconfigured store) means every time is bookable
— this feature must not retroactively restrict existing users who never
opted in.

Storage: one JSON file per (project, user) in the project vault, same
convention as calendar_overrides.py.

Explicitly out of scope for v1 (see the card's design discussion):
holidays/date-specific exceptions, per-meeting-type schedules, buffers/
min-notice/slot granularity, and windows that cross local midnight.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .aggregate import to_utc

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEDULE_EXTRA_KEYS = ("weeks", "dates", "blocks", "max_per_day")
PARITIES = ("even", "odd")


def _working_hours_path(acl, project: str, user: str) -> Path:
    vault = acl.resource(project, "vault")
    if not vault:
        raise ValueError(f"project {project!r} has no vault configured")
    directory = Path(vault) / "working_hours"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user}.json"


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    hour_i, minute_i = int(hour), int(minute)
    if hour_i == 24 and minute_i == 0:
        return time(23, 59, 59, 999999)  # end-of-day sentinel, see _day_windows_utc
    return time(hour_i, minute_i)


def validate_week(week: dict) -> None:
    """Raise ValueError if *week* is not a valid schedule shape."""
    if not set(week.keys()) <= set(WEEKDAYS):
        raise ValueError(f"unknown weekday key(s): {sorted(set(week.keys()) - set(WEEKDAYS))}")
    for day, windows in week.items():
        parsed = []
        for w in windows:
            start, end = _parse_hhmm(w["start"]), _parse_hhmm(w["end"])
            if start >= end:
                raise ValueError(f"{day}: start {w['start']!r} must be before end {w['end']!r}")
            parsed.append((start, end, w))
        parsed.sort(key=lambda p: (p[0], p[1]))
        for (start_a, end_a, w_a), (start_b, end_b, w_b) in zip(parsed, parsed[1:]):
            if start_b < end_a:
                raise ValueError(
                    f"{day}: window {w_a['start']!r}-{w_a['end']!r} overlaps "
                    f"{w_b['start']!r}-{w_b['end']!r}"
                )


class WorkingHoursStore:
    def __init__(self, acl, project: str, user: str) -> None:
        self._path = _working_hours_path(acl, project, user)

    def get(self) -> dict:
        """{} (no tz/week) if never configured — callers treat that as
        wide-open, no filtering."""
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def set(self, tz: str, week: dict) -> None:
        ZoneInfo(tz)  # raises ZoneInfoNotFoundError on an unresolvable tz
        validate_week(week)
        extras = {k: v for k, v in self.get().items() if k in SCHEDULE_EXTRA_KEYS}
        self._path.write_text(json.dumps({"tz": tz, "week": week, **extras}))

    def set_extras(self, **extras) -> dict:
        """Replace the schedule extras (weeks/dates/blocks/max_per_day)
        that sit on top of the base weekly schedule. Passing None for a
        key clears it. The base tz/week must already be set (or default
        to Europe/Stockholm with an empty week)."""
        current = self.get() or {"tz": "Europe/Stockholm", "week": {}}
        for key, value in extras.items():
            if key not in SCHEDULE_EXTRA_KEYS:
                raise ValueError(f"unknown schedule key: {key}")
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        validate_schedule(current)
        self._path.write_text(json.dumps(current))
        return current


def _day_windows_utc(week: dict, tz: ZoneInfo, local_day: date) -> list[tuple[datetime, datetime]]:
    """The given local calendar day's bookable windows, each localized to
    *tz* and converted to UTC. [] if that weekday has no windows."""
    windows = week.get(WEEKDAYS[local_day.weekday()], [])
    out = []
    for w in windows:
        start_t, end_t = _parse_hhmm(w["start"]), _parse_hhmm(w["end"])
        start_local = datetime.combine(local_day, start_t, tzinfo=tz)
        if end_t == time(23, 59, 59, 999999):  # "24:00" sentinel = next midnight
            end_local = datetime.combine(local_day + timedelta(days=1), time(0, 0), tzinfo=tz)
        else:
            end_local = datetime.combine(local_day, end_t, tzinfo=tz)
        out.append((start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))))
    return out


def apply_working_hours(free: list[dict], week: dict, tz: str) -> list[dict]:
    """Intersect each free {start, end} gap with the bookable local windows
    it overlaps. An empty *week* (or falsy *tz*) means wide-open — *free* is
    returned unchanged. Pure: UTC ISO-8601 strings in, UTC ISO-8601 strings
    out, same shape as aggregate.free_slots()."""
    if not week or not tz:
        return free

    zone = ZoneInfo(tz)
    out: list[dict] = []
    for gap in free:
        gap_start, gap_end = to_utc(gap["start"]), to_utc(gap["end"])
        day = gap_start.astimezone(zone).date()
        last_day = gap_end.astimezone(zone).date()
        while day <= last_day:
            for w_start, w_end in _day_windows_utc(week, zone, day):
                start = max(gap_start, w_start)
                end = min(gap_end, w_end)
                if start < end:
                    out.append({"start": start.isoformat(), "end": end.isoformat()})
            day += timedelta(days=1)
    out.sort(key=lambda s: s["start"])
    return out


def validate_schedule(schedule: dict) -> None:
    """Raise ValueError if the schedule extras (weeks/dates/blocks/
    max_per_day) are malformed."""
    ZoneInfo(schedule.get("tz") or "UTC")
    validate_week(schedule.get("week", {}))
    weeks = schedule.get("weeks", {})
    if not set(weeks) <= set(PARITIES):
        raise ValueError(f"weeks keys must be within {PARITIES}")
    for week in weeks.values():
        validate_week(week)
    for day, windows in schedule.get("dates", {}).items():
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError(f"dates: {day!r} is not YYYY-MM-DD") from exc
        validate_week({"mon": windows})
    for block in schedule.get("blocks", []):
        if "weekday" in block:
            if block.get("parity") not in (None, *PARITIES):
                raise ValueError(f"block parity must be one of {PARITIES}")
            validate_week({block["weekday"]: [{"start": block["start"], "end": block["end"]}]})
        else:
            try:
                b_start = datetime.fromisoformat(block["start"])
                b_end = datetime.fromisoformat(block["end"])
            except (KeyError, ValueError) as exc:
                raise ValueError("single block needs ISO start and end") from exc
            if b_start.tzinfo is None or b_end.tzinfo is None or b_start >= b_end:
                raise ValueError("single block needs tz-aware start before end")
    cap = schedule.get("max_per_day")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
        raise ValueError("max_per_day must be a positive integer")


def _parity(day: date) -> str:
    return "even" if day.isocalendar()[1] % 2 == 0 else "odd"


def _local_day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    utc = ZoneInfo("UTC")
    start = datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(utc)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone).astimezone(utc)
    return start, end


def _base_windows(schedule: dict, zone: ZoneInfo, day: date) -> list[tuple[datetime, datetime]]:
    """Bookable windows for *day* before blocks: a per-date override wins,
    then the even/odd ISO-week schedule, then the plain weekly schedule.
    No restriction configured at all means the whole local day is open."""
    dates = schedule.get("dates", {})
    key = day.isoformat()
    if key in dates:
        return _day_windows_utc({WEEKDAYS[day.weekday()]: dates[key]}, zone, day)
    weeks, week = schedule.get("weeks", {}), schedule.get("week", {})
    if weeks or week:
        chosen = weeks.get(_parity(day), week)
        return _day_windows_utc(chosen, zone, day)
    return [_local_day_bounds(day, zone)]


def _block_intervals(schedule: dict, zone: ZoneInfo, day: date) -> list[tuple[datetime, datetime]]:
    out = []
    for block in schedule.get("blocks", []):
        if "weekday" in block:
            if block["weekday"] != WEEKDAYS[day.weekday()]:
                continue
            if block.get("parity") not in (None, _parity(day)):
                continue
            out += _day_windows_utc({block["weekday"]: [{"start": block["start"], "end": block["end"]}]}, zone, day)
        else:
            out.append((to_utc(block["start"]), to_utc(block["end"])))
    return out


def _subtract(
    pieces: list[tuple[datetime, datetime]], cuts: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    for cut_start, cut_end in cuts:
        nxt = []
        for p_start, p_end in pieces:
            if cut_end <= p_start or cut_start >= p_end:
                nxt.append((p_start, p_end))
                continue
            if p_start < cut_start:
                nxt.append((p_start, cut_start))
            if cut_end < p_end:
                nxt.append((cut_end, p_end))
        pieces = nxt
    return pieces


def apply_schedule(free: list[dict], schedule: dict) -> list[dict]:
    """Like apply_working_hours but understands the full schedule: even/odd
    week variants, per-date locks and releases (an empty list locks the
    day), and single or recurring blocks. Only ever narrows *free*."""
    tz = schedule.get("tz")
    configured = any(schedule.get(k) for k in ("week", "weeks", "dates", "blocks"))
    if not tz or not configured:
        return free
    zone = ZoneInfo(tz)
    out: list[dict] = []
    for gap in free:
        gap_start, gap_end = to_utc(gap["start"]), to_utc(gap["end"])
        day = gap_start.astimezone(zone).date()
        last_day = gap_end.astimezone(zone).date()
        while day <= last_day:
            pieces = [
                (max(gap_start, w_start), min(gap_end, w_end))
                for w_start, w_end in _base_windows(schedule, zone, day)
            ]
            pieces = [(a, b) for a, b in pieces if a < b]
            for a, b in _subtract(pieces, _block_intervals(schedule, zone, day)):
                out.append({"start": a.isoformat(), "end": b.isoformat()})
            day += timedelta(days=1)
    out.sort(key=lambda s: s["start"])
    return out


def apply_day_cap(free: list[dict], tz: str, booked_per_day: dict[date, int], cap: int | None) -> list[dict]:
    """Drop every free window on a local day that already holds *cap*
    bookings, even if time remains that day."""
    if not cap or not tz:
        return free
    zone = ZoneInfo(tz)
    out: list[dict] = []
    for gap in free:
        gap_start, gap_end = to_utc(gap["start"]), to_utc(gap["end"])
        day = gap_start.astimezone(zone).date()
        last_day = gap_end.astimezone(zone).date()
        while day <= last_day:
            if booked_per_day.get(day, 0) < cap:
                d_start, d_end = _local_day_bounds(day, zone)
                a, b = max(gap_start, d_start), min(gap_end, d_end)
                if a < b:
                    out.append({"start": a.isoformat(), "end": b.isoformat()})
            day += timedelta(days=1)
    return out
