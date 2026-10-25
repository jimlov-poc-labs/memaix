# SPDX-License-Identifier: AGPL-3.0-or-later
"""Booking-page watch: mail the host when a public link has no bookable time.

A link can answer 200 with an empty slot list for days without anything
failing (an all-day event spanning years, a calendar source that needs
relinking, informational calendars counted as busy). The visitor sees "no
open slots" and nobody is told. This loop asks the same question the public
page asks, and mails the host after two empty checks in a row, with a line
on what is blocking. The link's slug is a capability, so it never appears in
the mail: links are named project/user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 1800
ALERT_AFTER_EMPTY_CHECKS = 2
REALERT_AFTER_SECONDS = 86400
DEFAULT_WINDOW_DAYS = 60
LONG_EVENT = timedelta(days=7)


@dataclass
class LinkState:
    empty_streak: int = 0
    alerted_at: float | None = None


def check_link(link: dict, acl, resolve_dav_fn, now: datetime) -> dict:
    """{"status": "ok"|"empty"|"error"|"skipped", "reason": str}.

    Runs the same lookup as the public /slots endpoint, minus the HTTP layer.
    "skipped" means the host switched booking off on purpose."""
    from ..tools import calendar as t_cal
    from ..tools.calendar import CalendarAuthRequired

    project, user = link["project"], link["user"]
    try:
        if not t_cal.calendar_booking_enabled_get(acl, user, project).get("enabled"):
            return {"status": "skipped", "reason": "bokning avstängd"}
        days = min(int(link.get("max_days_ahead") or DEFAULT_WINDOW_DAYS), DEFAULT_WINDOW_DAYS)
        end = now + timedelta(days=days)
        dav = resolve_dav_fn(project, user, acl)
        windows = t_cal.calendar_find_free(
            acl, user, project, int(link.get("duration_min", 30)),
            now.isoformat(), end.isoformat(), _dav=dav,
        )
    except CalendarAuthRequired:
        return {"status": "error", "reason": "kalenderkällan kräver ny inloggning"}
    except Exception as exc:
        return {"status": "error", "reason": f"kalenderfrågan misslyckades: {type(exc).__name__}"}
    if windows:
        return {"status": "ok", "reason": ""}
    return {"status": "empty", "reason": _diagnose(dav, now, end)}


def _diagnose(dav, start: datetime, end: datetime) -> str:
    """One short line per likely culprit, so the mail says why and not just that."""
    from ..connectors.aggregate import to_utc

    try:
        events = dav.find_events(start, end)
    except Exception as exc:
        return f"kunde inte läsa kalendern ({type(exc).__name__})"
    busy = [e for e in events if e.get("source_busy", True)]
    lines = [f"{len(busy)} upptagna händelser i fönstret."]
    for e in busy:
        try:
            if to_utc(e["end"]) - to_utc(e["start"]) > LONG_EVENT:
                title = (e.get("title") or "(utan titel)")[:60]
                lines.append(f"Lång händelse: {title} ({e['start']} till {e['end']}).")
        except Exception:
            continue
    return " ".join(lines[:6])


def evaluate(state: LinkState, result: dict, now_epoch: float) -> str | None:
    """Update *state*; return "alert", "recovered" or None."""
    if result["status"] == "skipped":
        state.empty_streak = 0
        return None
    if result["status"] == "ok":
        recovered = state.alerted_at is not None
        state.empty_streak = 0
        state.alerted_at = None
        return "recovered" if recovered else None
    state.empty_streak += 1
    if state.empty_streak < ALERT_AFTER_EMPTY_CHECKS:
        return None
    if state.alerted_at is None or now_epoch - state.alerted_at >= REALERT_AFTER_SECONDS:
        state.alerted_at = now_epoch
        return "alert"
    return None


def _message(action: str, label: str, result: dict) -> tuple[str, str]:
    if action == "recovered":
        return (
            f"Bokningen fungerar igen ({label})",
            f"Bokningslänken {label} har lediga tider igen.",
        )
    what = "inga lediga tider" if result["status"] == "empty" else "ett fel"
    return (
        f"Bokningen har {what} ({label})",
        f"Bokningslänken {label} har gett {what} vid minst {ALERT_AFTER_EMPTY_CHECKS} kontroller i rad.\n\n"
        f"{result['reason']}\n\nBesökare ser \"inga lediga tider\" tills det är åtgärdat.",
    )


def run_checks(links, states: dict, acl, resolve_dav_fn, send_fn, now: datetime) -> None:
    """One tick. A failing link or a failing mail never stops the others."""
    for slug, link in links:
        label = f"{link.get('project')}/{link.get('user')}"
        try:
            result = check_link(link, acl, resolve_dav_fn, now)
            action = evaluate(states.setdefault(slug, LinkState()), result, now.timestamp())
            if action:
                subject, body = _message(action, label, result)
                send_fn(subject, body)
        except Exception:
            logger.warning("booking watch failed for %s", label, exc_info=True)


async def booking_watch_loop(
    acl_fn, resolve_dav_fn, links_fn, send_fn_factory, *, tick_seconds: int = DEFAULT_TICK_SECONDS, now_fn=None,
) -> None:
    """Same contract as the other background loops: an exception in a tick
    is logged and the loop carries on. Calendar calls block, so a tick runs
    in a worker thread."""
    import asyncio

    states: dict = {}
    while True:
        now = now_fn() if now_fn else datetime.now(timezone.utc)
        try:
            send_fn = send_fn_factory()
            if send_fn is not None:
                await asyncio.to_thread(run_checks, links_fn(), states, acl_fn(), resolve_dav_fn, send_fn, now)
        except Exception:
            logger.warning("booking_watch_loop tick failed", exc_info=True)
        await asyncio.sleep(tick_seconds)
