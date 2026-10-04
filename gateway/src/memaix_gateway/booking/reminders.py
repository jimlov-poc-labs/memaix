# SPDX-License-Identifier: AGPL-3.0-or-later
"""Meeting reminders — memaix-src card ecffcb5b.

Beyond the confirmation email sent at booking time (card 14666e8a), a
visitor and host each get a reminder email 24h and 1h before the meeting
starts. Fixed offsets for every booking in v1 — there is no per-link or
per-meeting-type config surface today (links.py's records are flat JSON
with no reminder-policy field), and the offsets that legitimately vary per
booking are handled by the stale-skip logic below, not by config. A
per-link override slots in later without disruption: read
link.get("reminder_offsets") in send_due_reminders() and fall back to
REMINDER_OFFSETS_MIN.

Same isolation shape as purge.py's consent_purge_loop: its own asyncio
task in server.py, a tick's exception never kills the loop, and a single
row's failure is logged and skipped rather than blocking the rest of the
tick. Unlike purge (hourly, retention isn't time-critical), this polls
every DEFAULT_TICK_SECONDS since a 1h-before reminder needs finer
granularity than an hourly tick could reliably hit.

Dedup/idempotency: ConsentStore.mark_reminder_sent(row_id, offset_min) is
a durable, lock-guarded claim (check reminders_sent, append if absent) —
this is what stops a reminder firing twice across overlapping ticks or a
restart, the same principle notify/scheduler.py's claim() uses for
recurring briefs, adapted to a one-shot-per-(booking, offset) job instead
of a recurring per-user slot.

Stale reminders (gateway was down when a fire time passed) are skipped,
not sent late — a 24h reminder arriving 6 hours late is worse than no
reminder at all. GRACE_MIN bounds how late is still worth sending; a
stale offset is still marked sent so it never fires later either.

Reschedule/cancel interaction lives in consent_store.py's update_booking():
reschedule resets reminders_sent (old-time reminders are dead, new time
gets fresh ones) and writes the new meeting_start; cancel's status flip
removes the row from reminders_due()'s query predicate entirely.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 300  # 5 min: fine enough for a 1h-before reminder
REMINDER_OFFSETS_MIN: tuple[int, ...] = (1440, 60)  # 24h, 1h
GRACE_MIN = 30  # don't fire a reminder whose window we missed by more than this


def due_offsets(
    meeting_start: int, sent: set[str], now_epoch: int,
    offsets_min: tuple[int, ...] = REMINDER_OFFSETS_MIN, grace_min: int = GRACE_MIN,
) -> list[int]:
    """Pure. Offsets (minutes before meeting_start) whose fire time falls in
    (now - grace, now] and aren't already in *sent*, largest offset first
    (24h reminder before the 1h one on the rare tick that's due for both)."""
    due = []
    for offset in sorted(offsets_min, reverse=True):
        if str(offset) in sent:
            continue
        fire_at = meeting_start - offset * 60
        if now_epoch - grace_min * 60 < fire_at <= now_epoch:
            due.append(offset)
    return due


def stale_offsets(
    meeting_start: int, sent: set[str], now_epoch: int,
    offsets_min: tuple[int, ...] = REMINDER_OFFSETS_MIN, grace_min: int = GRACE_MIN,
) -> list[int]:
    """Offsets whose fire time has already passed by more than the grace
    window — too late to send, but still need marking as sent so they never
    fire later (e.g. a booking made only 20 minutes out, or a gateway outage
    spanning a fire time)."""
    stale = []
    for offset in offsets_min:
        if str(offset) in sent:
            continue
        fire_at = meeting_start - offset * 60
        if fire_at <= now_epoch - grace_min * 60:
            stale.append(offset)
    return stale


def _close_stale_offsets(store, row_id: str, meeting_start: int, sent: set[str], now_epoch: int) -> None:
    for offset in stale_offsets(meeting_start, sent, now_epoch):
        store.mark_reminder_sent(row_id, offset)  # never send, just close it out
        sent.add(str(offset))


def _send_and_claim(store, row: dict, acl_fn, link_fn, offset: int) -> bool:
    """Send one reminder and claim it. True when it counts as dispatched; False
    when there is no link yet (retry next tick, unclaimed) or another tick
    claimed it after we sent (don't double count)."""
    from .routes import _format_meeting_detail_line, _send_reminder_email

    meeting_start = row["meeting_start"]
    link = link_fn(row["slug"]) if row["slug"] else None
    if link is None:
        return False  # no link to resolve title/host from — don't claim, retry next tick
    title = link.get("title_template", "Möte")
    start_dt = datetime.fromtimestamp(meeting_start, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(row.get("meeting_end") or meeting_start, tz=timezone.utc)
    meeting_detail_line = _format_meeting_detail_line(
        row.get("meeting_form_provider"), row.get("meeting_form_detail"),
    )
    _send_reminder_email(
        acl_fn(), row["project"], link, title, row["event_id"],
        row["visitor_email"], start_dt, end_dt, offset, row.get("manage_token", ""),
        meeting_detail_line, row.get("meeting_form_detail"),
    )
    # Claim only after a successful send — claiming first would
    # permanently lose the reminder if the send then failed
    # (card ecffcb5b review, Simon).
    return bool(store.mark_reminder_sent(row["id"], offset))


def send_due_reminders(store, acl_fn, link_fn, now: datetime) -> int:
    """Send every due reminder across every pending booking. Returns the
    number of emails dispatched this tick. A single row's failure never
    blocks the rest — same isolation as purge_due()."""
    now_epoch = int(now.timestamp())
    sent_count = 0
    for row in store.reminders_due(now_epoch, REMINDER_OFFSETS_MIN):
        row_id, meeting_start = row["id"], row["meeting_start"]
        sent = {s for s in row["reminders_sent"].split(",") if s}
        _close_stale_offsets(store, row_id, meeting_start, sent, now_epoch)

        for offset in due_offsets(meeting_start, sent, now_epoch):
            try:
                if _send_and_claim(store, row, acl_fn, link_fn, offset):
                    sent_count += 1
            except Exception:
                logger.exception(
                    "booking reminder failed for row=%s project=%s host_user=%s offset=%s",
                    row_id, row["project"], row["host_user"], offset,
                )
    return sent_count


async def reminder_loop(acl_fn, link_fn, *, tick_seconds: int = DEFAULT_TICK_SECONDS, now_fn=None) -> None:
    """Check every *tick_seconds* for due reminders and send them. A tick's
    exception never kills the loop (same contract as
    notify/scheduler.py's scheduler_loop and consent_purge_loop)."""
    import asyncio

    from .consent_store import get_consent_store

    while True:
        now = now_fn() if now_fn else datetime.now(timezone.utc)
        try:
            send_due_reminders(get_consent_store(), acl_fn, link_fn, now)
        except Exception:
            logger.warning("reminder_loop tick failed", exc_info=True)
        await asyncio.sleep(tick_seconds)
