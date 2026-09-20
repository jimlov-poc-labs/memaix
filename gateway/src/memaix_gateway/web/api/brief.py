# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for daily-brief settings (MEX-025 Fas D) — same store + validators
as the MCP tools brief_configure/brief_status."""

from __future__ import annotations

from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import routes as w


def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _notify_store():
    from ...server import _get_notify

    return _get_notify()


def api_brief_get(request: Request) -> JSONResponse:
    """GET /app/api/brief → {configured, prefs, next_run, last_run}"""
    user = _require_user(request)
    if not user:
        return w._json_401()
    store = _notify_store()
    prefs = store.get_prefs(user)
    if prefs is None:
        return JSONResponse({"configured": False})
    schedule = store.get_schedule(user, "daily")
    next_run = (
        datetime.fromtimestamp(schedule["next_run"], tz=timezone.utc).isoformat()
        if schedule else None
    )
    last_run = (
        datetime.fromtimestamp(schedule["last_run"], tz=timezone.utc).isoformat()
        if schedule and schedule.get("last_run") else None
    )
    return JSONResponse({"configured": True, "prefs": prefs, "next_run": next_run, "last_run": last_run})


async def api_brief_set(request: Request) -> JSONResponse:
    """POST /app/api/brief {enabled, brief_time, timezone?, channels?, quiet_hours?, projects?}"""
    user = _require_user(request)
    if not user:
        return w._json_401()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)

    from ... import config
    from ...notify.scheduler import next_brief_epoch
    from ...server import _validate_brief_time, _validate_channels, _validate_timezone

    cfg = config.load()
    enabled = bool(body.get("enabled", True))
    brief_time = str(body.get("brief_time", "07:00"))
    tz_name = body.get("timezone") or cfg.get("memaix", {}).get("brief", {}).get(
        "default_timezone", "UTC"
    )
    channels = body.get("channels")
    quiet = body.get("quiet_hours") or {}
    projects = body.get("projects")
    try:
        _validate_brief_time(brief_time)
        _validate_timezone(tz_name)
        _validate_channels(channels)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    store = _notify_store()
    now = datetime.now(timezone.utc)
    prefs = store.set_prefs(
        user, now_iso=now.isoformat(),
        enabled=enabled, brief_time=brief_time, timezone=tz_name,
        channels=channels, projects=projects,
        quiet_start=quiet.get("start"), quiet_end=quiet.get("end"),
    )
    next_epoch = next_brief_epoch(prefs, now)
    store.upsert_schedule(user, "daily", next_epoch)
    return JSONResponse({
        "ok": True, "prefs": prefs,
        "next_run": datetime.fromtimestamp(next_epoch, tz=timezone.utc).isoformat(),
    })
