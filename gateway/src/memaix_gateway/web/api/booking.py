# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for the host's own booking settings (/app/booking) — a thin layer
over tools/calendar.py so ACL/RBAC and validation stay in one place.

Reads need reader, writes collaborator (enforced by the tools). The host's
public booking-link slug is never part of any response here."""

from __future__ import annotations

import re
import unicodedata

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...acl import AccessDenied
from ...tools import calendar as t_cal
from .. import routes as w

_DEFAULT_TZ = "Europe/Stockholm"
_EXTRA_CLEARED = {"weeks": {}, "dates": {}, "blocks": [], "max_per_day": 0}


def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _get_acl():
    return w._get_acl()


def _json_401() -> JSONResponse:
    return w._json_401()


def _token_store():
    from ...server import _get_token_store

    return _get_token_store()


def _bad(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _forbidden() -> JSONResponse:
    return JSONResponse({"error": "forbidden"}, status_code=403)


def _fail(result: dict) -> JSONResponse:
    """The tools report validation problems as {ok: False, error}."""
    return JSONResponse({"error": result.get("error", "bad_request")}, status_code=400)


def _check_windows(value, label: str) -> None:
    if not isinstance(value, list) or not all(
        isinstance(x, dict) and isinstance(x.get("start"), str) and isinstance(x.get("end"), str)
        for x in value
    ):
        raise ValueError(f"{label}: expected a list of {{start, end}} windows")


def _check_week(value, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected an object keyed by weekday")
    for day, windows in value.items():
        _check_windows(windows, f"{label}.{day}")


def _check_extras(body: dict) -> dict:
    """Structural checks only — range/overlap rules live in working_hours.py.
    Returns the kwargs for calendar_schedule_set; JSON null clears a key."""
    extras: dict = {}
    for key in ("weeks", "dates", "blocks", "max_per_day"):
        if key not in body:
            continue
        value = body[key]
        if value is None or value == "":
            extras[key] = _EXTRA_CLEARED[key]
            continue
        if key == "weeks":
            if not isinstance(value, dict):
                raise ValueError("weeks: expected an object with even/odd")
            for parity, week in value.items():
                _check_week(week, f"weeks.{parity}")
        elif key == "dates":
            if not isinstance(value, dict):
                raise ValueError("dates: expected an object keyed by date")
            for day, windows in value.items():
                _check_windows(windows, f"dates.{day}")
        elif key == "blocks":
            if not isinstance(value, list) or not all(
                isinstance(b, dict) and isinstance(b.get("start"), str) and isinstance(b.get("end"), str)
                for b in value
            ):
                raise ValueError("blocks: expected a list of {start, end} objects")
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_per_day must be a whole number")
        extras[key] = value
    return extras


def _slugify(name: str, taken: set[str]) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:40].strip("-") or "session"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _state(acl, user: str, project: str) -> dict:
    hours = t_cal.calendar_working_hours_get(acl, user, project)
    mode = t_cal.get_status(user, project, acl, _token_store())
    return {
        "enabled": bool(t_cal.calendar_booking_enabled_get(acl, user, project).get("enabled")),
        "calendar_mode": mode["active_mode"],
        "tz": hours.get("tz") or _DEFAULT_TZ,
        "week": hours.get("week", {}),
        "weeks": hours.get("weeks", {}),
        "dates": hours.get("dates", {}),
        "blocks": hours.get("blocks", []),
        "max_per_day": hours.get("max_per_day"),
        "meeting_types": t_cal.calendar_meeting_type_list(acl, user, project),
    }


async def _body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def api_booking_get(request: Request) -> JSONResponse:
    """GET /app/api/booking?project=X → {enabled, calendar_mode, tz, week, weeks,
    dates, blocks, max_per_day, meeting_types}"""
    user = _require_user(request)
    if not user:
        return _json_401()
    project = request.query_params.get("project", "")
    try:
        return JSONResponse(_state(_get_acl(), user, project))
    except AccessDenied:
        return _forbidden()
    except ValueError as exc:  # project without a vault
        return _bad(str(exc))


async def api_booking_schedule_set(request: Request) -> JSONResponse:
    """POST /app/api/booking/schedule {project, tz?, week?, weeks?, dates?, blocks?,
    max_per_day?} — only the keys present are changed; null clears one."""
    user = _require_user(request)
    if not user:
        return _json_401()
    body = await _body(request)
    if body is None:
        return _bad("bad_request")
    project = body.get("project", "")
    acl = _get_acl()
    try:
        extras = _check_extras(body)
        if "week" in body and body["week"] is not None:
            _check_week(body["week"], "week")
        if "tz" in body and not isinstance(body["tz"], str):
            raise ValueError("tz must be a text")
        if "tz" in body or "week" in body:
            current = t_cal.calendar_working_hours_get(acl, user, project)
            result = t_cal.calendar_working_hours_set(
                acl, user, project,
                body.get("tz") or current.get("tz") or _DEFAULT_TZ,
                body["week"] if body.get("week") is not None else current.get("week", {}),
            )
            if not result.get("ok"):
                return _fail(result)
        if extras:
            result = t_cal.calendar_schedule_set(acl, user, project, **extras)
            if not result.get("ok"):
                return _fail(result)
        return JSONResponse(_state(acl, user, project))
    except AccessDenied:
        return _forbidden()
    except (ValueError, TypeError, AttributeError) as exc:
        return _bad(str(exc))


async def api_booking_enabled_set(request: Request) -> JSONResponse:
    """POST /app/api/booking/enabled {project, enabled} → {ok, enabled}"""
    user = _require_user(request)
    if not user:
        return _json_401()
    body = await _body(request)
    if body is None or not isinstance(body.get("enabled"), bool):
        return _bad("enabled must be true or false")
    try:
        result = t_cal.calendar_booking_enabled_set(
            _get_acl(), user, body.get("project", ""), body["enabled"],
        )
    except AccessDenied:
        return _forbidden()
    except ValueError as exc:
        return _bad(str(exc))
    return JSONResponse(result)


async def api_booking_meeting_type_set(request: Request) -> JSONResponse:
    """POST /app/api/booking/meeting-types {project, name, duration_min, slug?, default?}
    — adds a session length, or replaces the one with the given slug."""
    user = _require_user(request)
    if not user:
        return _json_401()
    body = await _body(request)
    if body is None:
        return _bad("bad_request")
    project = body.get("project", "")
    name = body.get("name")
    minutes = body.get("duration_min")
    if not isinstance(name, str) or not name.strip():
        return _bad("name is required")
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return _bad("duration_min must be a whole number of minutes")
    acl = _get_acl()
    try:
        types = t_cal.calendar_meeting_type_list(acl, user, project)
        existing = next((t for t in types if t["slug"] == body.get("slug")), None)
        slug = existing["slug"] if existing else _slugify(name, {t["slug"] for t in types})
        entry = {
            "slug": slug, "name": name.strip(), "duration_min": minutes,
            "interval_min": minutes,
            "default": bool(body.get("default", existing["default"] if existing else False)),
        }
        if existing and existing["interval_min"] != existing["duration_min"]:
            entry["interval_min"] = existing["interval_min"]  # keep a custom step set elsewhere
        merged = [
            entry if t["slug"] == slug else (dict(t, default=False) if entry["default"] else t)
            for t in types
        ]
        if not existing:
            merged.append(entry)
        result = t_cal.calendar_meeting_type_set(acl, user, project, merged)
    except AccessDenied:
        return _forbidden()
    except ValueError as exc:
        return _bad(str(exc))
    if not result.get("ok"):
        return _fail(result)
    return JSONResponse(result)


def api_booking_meeting_type_delete(request: Request) -> JSONResponse:
    """DELETE /app/api/booking/meeting-types/{slug}?project=X → {ok, types}"""
    user = _require_user(request)
    if not user:
        return _json_401()
    try:
        result = t_cal.calendar_meeting_type_delete(
            _get_acl(), user, request.query_params.get("project", ""), request.path_params["slug"],
        )
    except AccessDenied:
        return _forbidden()
    except ValueError as exc:
        return _bad(str(exc))
    return JSONResponse(result)
