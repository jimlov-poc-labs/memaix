# SPDX-License-Identifier: AGPL-3.0-or-later
"""deliver — build a brief, then send it through configured channels once.

See docs/FEATURE-PROACTIVE-BRIEF.md §5/§9. Idempotent (one send per
user+date), respects quiet hours unless force=True, and never lets one
broken channel stop the others.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo

from .brief import build
from .channels import build_channels

logger = logging.getLogger(__name__)


def _in_quiet_hours(now: datetime, prefs: dict) -> bool:
    qs, qe = prefs.get("quiet_start"), prefs.get("quiet_end")
    if not qs or not qe:
        return False
    from zoneinfo import ZoneInfo
    tz: tzinfo
    try:
        tz = ZoneInfo(prefs.get("timezone", "UTC"))
    except Exception:
        tz = timezone.utc
    cur = now.astimezone(tz).strftime("%H:%M")
    if qs <= qe:
        return qs <= cur < qe
    return cur >= qs or cur < qe  # window wraps midnight


def deliver(
    store, acl, cfg, user: str, prefs: dict, *,
    now: datetime, force: bool = False, tools: dict | None = None,
    _channels: list | None = None,
) -> dict:
    date_str = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    idem_key = f"{user}:daily:{date_str}"

    if not force and store.already_sent(idem_key):
        return {"skipped": "duplicate"}
    if not force and _in_quiet_hours(now, prefs):
        return {"skipped": "quiet_hours"}

    schedule = store.get_schedule(user, "daily")
    last_run_iso = None
    if schedule and schedule.get("last_run"):
        last_run_iso = datetime.fromtimestamp(schedule["last_run"], tz=timezone.utc).isoformat()

    brief_content = build(acl, user, cfg, prefs, now=now, tools=tools, last_run_iso=last_run_iso)
    if brief_content.get("empty") and not brief_content.get("markdown"):
        return {"skipped": "empty"}

    channels = _channels if _channels is not None else build_channels(prefs.get("channels", []), acl=acl)

    delivered = 0
    errors: list[str] = []
    for ch in channels:
        try:
            ch.send(brief_content["subject"], brief_content["markdown"], brief_content["text"])
            delivered += 1
        except Exception as exc:
            # Type only: requests' error text contains the webhook URL / ntfy topic.
            errors.append(f"{type(ch).__name__}: {type(exc).__name__}")
            logger.warning("brief channel delivery failed (%s: %s)", type(ch).__name__, type(exc).__name__)

    store.record_sent(idem_key, now.isoformat())

    return {
        "ok": delivered > 0 or not channels,
        "delivered": delivered,
        "channels": len(channels),
        "errors": errors,
    }
