# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public meeting-booking routes — memaix-src card 2bef1062.

Unauthenticated surface (no login, no ACL grant on the visitor) that lets
anyone holding a booking-link slug (links.py) see a host's free slots and
book one. Mirrors the unauthenticated-route pattern established by
rule_webhook (server.py) — rate-limited per client IP, the slug plays the
same "token IS the capability" role rule_webhook's token does.

Confirmation email + .ics (card 14666e8a): sent to the visitor and, if the
link config has a "host_email", to the host too — best-effort, after the
calendar event is already committed and the booking lock released. A
missing mailbox/allow_send config for the project, or a transient SMTP
error, is logged and swallowed, never surfaced as a booking failure — the
booking itself already succeeded by the time email is attempted.

GDPR consent + 1-year retention (card 01cf3b74): booking_create requires
{"consent": true} in the body — a booking is rejected with 400
consent_required otherwise, since a frontend checkbox alone is theatre
without a server-side check. The consent text shown to the visitor is
recorded verbatim in consent_store.py alongside the event id and meeting
end time, which purge.py's background loop uses a year later to delete
the calendar event and scrub the log — see those two modules for why a
separate log is the only reliable way to find "which calendar events are
bookings" at all.

The host's own ACL-provisioned user_id (from the link record) is used for
every calendar_* call, so calendar_find_free/calendar_create's existing
"collaborator" enforcement is satisfied naturally — there is no anonymous
bypass in tools/calendar.py and this route does not attempt to add one.

Privacy: only ever returns {start, end} slots (memaix-src card de858332) —
never calendar_free_busy's richer, source-tagged view.

Reschedule/cancel (card 8056150d): booking_create mints an opaque
manage_token (consent_store.py) and includes a manage link in both
confirmation emails — the token IS the capability, same convention as the
slug, so either the visitor or the host can act on it via
/booking/{token}/reschedule or /booking/{token}/cancel, no login for
either party. Reschedule re-runs the exact TOCTOU check and lock
booking_create uses, since it's racing other bookers for a new window the
same way a fresh booking would. Detecting a conflict introduced later by
the host editing their own calendar directly is explicitly out of scope
for this card — see the card's design notes for why (no inbound calendar
channel exists to observe that).

CORS is scoped to these routes only (not the whole app), since jimlov.se
and memaix.se are static exports with no server runtime and must call
this gateway cross-origin. Both front the same booking link — see
_ALLOWED_ORIGINS.

Reminders (card ecffcb5b): reminders.py's background loop sends a 24h and
1h-before reminder for every pending booking, using the manage_token and
_reminder_body/_send_reminder_email helpers below (same best-effort,
never-fail-the-caller contract as the other _send_*_emails helpers here).
meeting_start is threaded through booking_create/reschedule/cancel into
consent_store.py so reminders.py's reminders_due() can find bookings by
start time without a second lookup against the calendar.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
import uuid as _uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .. import config
from ..tools import calendar as t_cal
from ..tools import email as t_email
from ..tools.calendar import CalendarAuthRequired
from .consent_store import get_consent_store
from .links import get_link
from .meeting_providers import MeetingProviderError, resolve_meeting_detail
from .slotting import DEFAULT_GRANULARITY_MIN, subdivide

logger = logging.getLogger(__name__)

# The two sites that predate per-link origins. Kept hardcoded so a config
# mistake can't take booking off the air on either of them; every other site
# embedding the widget adds itself via the link's "origins" list instead of
# by editing this file and redeploying.
_ALLOWED_ORIGINS = {
    "https://jimlov.se",
    "https://www.jimlov.se",
    # memaix.se/boka reaches the same host, the same calendar and the
    # same booking link — only the chrome differs. A separate booking
    # type would just be two ways to double-book one person.
    "https://memaix.se",
    "https://www.memaix.se",
}
_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_MIN_DURATION_MIN = 15
_MAX_DURATION_MIN = 240
# Five minutes is already finer than anyone books on; below it the grid stops
# being a grid and starts being every instant of the day.
_MIN_GRANULARITY_MIN = 5
_MAX_WINDOW_DAYS = 90
_MAX_PURPOSE_LEN = 500

# Omsluter TOCTOU-omkollen + calendar_create per värd-användare. I dagens
# drift (uvicorn utan workers=, se server.py) gör detta inget praktiskt
# jobb — booking_create är async utan await i den kritiska sektionen, så
# händelseloopen kör redan omkoll->skapa till slut innan den växlar till
# nästa request; det är olikt _USER_LOCKS i llm/agent.py, vars kritiska
# sektion INNEHÅLLER await och där låset alltså gör verkligt arbete.
# Poängen med den här är försäkring mot dagen gatewayen körs multi-worker
# (gunicorn -w N, flera containrar) — då är denna defaultdict processlokal
# och skyddar ingenting, medan CalDAV/Google fortfarande saknar
# compare-and-swap på event-skapande. Känd begränsning, se card d99e2840.
_BOOKING_LOCKS: "defaultdict[tuple[str, str], threading.Lock]" = defaultdict(threading.Lock)


def _get_acl():
    # Lazy indirection to server's cached Acl, same pattern as web/routes.py —
    # function-level import avoids a module-import cycle with server.py.
    from ..server import _get_acl as _srv_get_acl

    return _srv_get_acl()


def _resolve_dav(project: str, user: str, *, write: bool = False):
    from ..server import _resolve_calendar_dav as _srv_resolve_dav

    return _srv_resolve_dav(project, user, write=write)


def _resolve_dav_filtered(project: str, user: str, acl) -> object:
    """Like _resolve_dav but honours SourceSelectionStore's disabled-set.

    SA source is labelled "calendar_sa" in the disabled-set; registry
    sources use their existing label from registry.get_all(). Falls back
    to _resolve_dav when no vault is configured (SourceSelectionStore
    needs a vault path to persist state).
    """
    import json
    import os

    from ..connectors.calendar_sources import SourceSelectionStore, resolve_effective_sources
    from ..server import _get_token_store
    from ..tools.calendar import (
        _MultiCalendarAdapter,
        _ServiceAccountGoogleCalendarAdapter,
    )

    vault = acl.resource(project, "vault")
    if not vault:
        return _resolve_dav(project, user)

    store = SourceSelectionStore(acl, project, user)
    disabled = set(store.list()["disabled"])

    adapters: list = []

    sa_res = acl.resource(project, "calendar_sa")
    if (
        isinstance(sa_res, dict)
        and sa_res.get("auth") == "service_account"
        and "calendar_sa" not in disabled
    ):
        try:
            ref = sa_res["service_account_ref"]
            if ref.startswith("env:"):
                env_var = ref[4:]
                sa_json = os.environ.get(env_var, "")
                if not sa_json:
                    raise CalendarAuthRequired(f"Env-var {env_var} saknas för SA-kalender")
                sa_info = json.loads(sa_json)
            elif ref.startswith("file:"):
                with open(ref[5:]) as f:
                    sa_info = json.load(f)
            else:
                raise CalendarAuthRequired(f"Okänd service_account_ref: {ref}")
        except CalendarAuthRequired:
            raise
        except (ValueError, FileNotFoundError, json.JSONDecodeError, KeyError, OSError) as exc:
            raise CalendarAuthRequired(f"SA-konfigfel för {project}: {exc}") from exc
        adapters.append(_ServiceAccountGoogleCalendarAdapter(sa_info, sa_res["impersonate"]))

    token_store = _get_token_store()
    for _label, adapter in resolve_effective_sources(acl, token_store, project, user):
        adapters.append(adapter)

    if not adapters:
        raise CalendarAuthRequired(f"Inga aktiva kalenderadaptrar för {project}/{user}")

    if len(adapters) == 1:
        return adapters[0]

    return _MultiCalendarAdapter(adapters)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _link_origins(request: Request) -> set[str]:
    """Extra origins the widget for *this* booking link may be embedded on.

    Read back off the link rather than threaded in as an argument, because
    _json() is called from a dozen places — including error paths that never
    resolved a link at all — and the one that forgot to pass it would
    silently lose its CORS headers. That failure only ever shows up in
    somebody else's browser console, on a site we don't own.
    """
    slug = request.path_params.get("slug")
    if not slug:
        return set()
    link = get_link(slug)
    origins = (link or {}).get("origins")
    return {str(o) for o in origins} if isinstance(origins, list) else set()


def _cors_headers(request: Request) -> dict:
    origin = request.headers.get("origin", "")
    # No Origin header means no browser asked, and CORS headers mean nothing to
    # curl or the tunnel's health check. Leaving early keeps _link_origins from
    # reading the link file a second time on every non-browser request.
    if not origin:
        return {}
    if origin not in _ALLOWED_ORIGINS and origin not in _link_origins(request):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }


def _json(request: Request, payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=_cors_headers(request))


def _with_cors_on_error(handler):
    """Guarantee a CORS-header-bearing response even when *handler* raises.

    Every clean/handled response in this module goes through _json(), which
    sets the CORS header — but an unhandled exception (a bug, an adapter
    that doesn't implement the method the write path needs, ...) bypasses
    _json() entirely and falls into Starlette's default error middleware,
    which returns a bare 500 with no CORS header at all. A browser then
    reports that as a generic net::ERR_FAILED with no status the frontend
    can act on, instead of a 500 it could show the visitor. Wrapping every
    route here means a future bug in a handler still surfaces as a real,
    CORS-compliant error instead of a silent failure in someone else's
    browser console.
    """

    @functools.wraps(handler)
    async def _wrapped(request: Request):
        try:
            return await handler(request)
        except Exception:
            logger.exception("unhandled error in booking route %s", handler.__name__)
            return _json(request, {"error": "internal_error"}, status_code=500)

    return _wrapped


def _clamp_duration(duration_min: int) -> int:
    return max(_MIN_DURATION_MIN, min(_MAX_DURATION_MIN, duration_min))


def _clamp_granularity(value: object) -> int:
    """The spacing of the offered start times, from an untrusted query param.

    Unparseable falls back to the default rather than 400ing: the grid is a
    presentation choice, and refusing to show a calendar because someone
    typed `granularity_min=abc` would be a tantrum, not validation. The
    ceiling is the same as duration's — a grid coarser than the longest
    bookable meeting can't offer anything the meeting doesn't already fill.
    """
    try:
        n = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return DEFAULT_GRANULARITY_MIN
    return max(_MIN_GRANULARITY_MIN, min(_MAX_DURATION_MIN, n))


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clamp_window(
    start: datetime, end: datetime, max_days_ahead: int | None = None
) -> tuple[datetime, datetime]:
    cap = start + timedelta(days=_MAX_WINDOW_DAYS)
    if max_days_ahead is not None:
        start_of_today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cap = min(cap, start_of_today + timedelta(days=max_days_ahead))
    return start, min(end, cap)


async def _verify_turnstile(token: str, remote_ip: str) -> bool:
    """Fail-closed: no secret configured -> refuse, never silently allow
    through. Captcha is mandatory for this public surface, not optional."""
    cfg = config.load()
    secret_ref = cfg.get("memaix", {}).get("booking", {}).get("turnstile_secret_ref")
    if not secret_ref:
        return False
    try:
        secret = config.secret(secret_ref)
    except (KeyError, NotImplementedError):
        return False
    if not token:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _TURNSTILE_VERIFY_URL,
                data={"secret": secret, "response": token, "remoteip": remote_ip},
                timeout=10.0,
                follow_redirects=False,
            )
        resp.raise_for_status()
        return bool(resp.json().get("success"))
    except httpx.HTTPError:
        return False


class _Refused(Exception):
    """A lookup that ended in a response instead of a result."""

    def __init__(self, response: JSONResponse) -> None:
        self.response = response


def _find_free(request: Request) -> tuple[dict, list, int]:
    """The lookup both /slots and /times need: rate limit, resolve the link,
    check booking is on, parse the window, ask the calendar.

    Returns (link, windows, duration_min), or raises _Refused carrying the
    response to send. Shared rather than copied because the two endpoints
    must agree on what counts as free — /times is only /slots with the
    subdivision done here instead of in the browser, and a rate limit or a
    404 rule that applied to one but not the other would be a hole, not a
    feature.
    """
    client_ip = _client_ip(request)
    # Separate bucket from booking_create's. They used to share `booking:{ip}`
    # while checking it against different limits, which meant looking at the
    # calendar spent the budget for booking in it: eleven clicks through the
    # weeks and the visitor got a 429 on submit, having done nothing wrong.
    # Reading and writing are different privileges and get different counters.
    if not _rate_limiter().check(f"booking:read:{client_ip}", limit=60, window_s=60):
        raise _Refused(_json(request, {"error": "rate_limited"}, status_code=429))

    link = get_link(request.path_params["slug"])
    if link is None:
        raise _Refused(_json(request, {"error": "not_found"}, status_code=404))

    acl = _get_acl()
    project, host_user = link["project"], link["user"]
    enabled = t_cal.calendar_booking_enabled_get(acl, host_user, project)
    if not enabled.get("enabled"):
        raise _Refused(_json(request, {"error": "not_found"}, status_code=404))

    q = request.query_params
    duration_min = _clamp_duration(int(q.get("duration_min") or link.get("duration_min", 30)))
    within_start = _parse_dt(q.get("within_start", ""))
    within_end = _parse_dt(q.get("within_end", ""))
    if within_start is None or within_end is None or within_end <= within_start:
        raise _Refused(_json(request, {"error": "invalid_window"}, status_code=400))
    within_start, within_end = _clamp_window(
        within_start, within_end, link.get("max_days_ahead")
    )

    try:
        dav = _resolve_dav_filtered(project, host_user, acl)
        windows = t_cal.calendar_find_free(
            acl, host_user, project, duration_min,
            within_start.isoformat(), within_end.isoformat(), _dav=dav,
        )
    except CalendarAuthRequired:
        raise _Refused(_json(request, {"error": "not_found"}, status_code=404))

    return link, windows, duration_min


def _host_timezone(link: dict) -> str:
    """The zone the host's day is lived in, for aligning slots to their clock.

    Working hours win because that's the frame the weekly schedule is already
    expressed in (calendar_working_hours_set) — aligning to a different zone
    than the one that decided "mon 16:00-18:00" would put slots on the half
    hour of somebody else's afternoon. `host_timezone` on the link is the
    fallback for hosts who never configured working hours, and UTC is the
    last resort: wrong for most people, but never unresolvable.
    """
    try:
        hours = t_cal.calendar_working_hours_get(_get_acl(), link["user"], link["project"])
        tz = str(hours.get("tz") or "")
    except Exception:
        logger.warning("kunde inte läsa arbetstider för %s", link.get("user"), exc_info=True)
        tz = ""
    tz = tz or str(link.get("host_timezone") or "") or "UTC"
    try:
        ZoneInfo(tz)
    except Exception:
        # Loudly, because the visible symptom is a grid of times that are
        # merely an hour or two wrong — which looks like a calendar bug, not
        # a config typo, and gets debugged in the wrong place for a day.
        logger.warning("okänd tidszon %r för bokningslänk, faller tillbaka på UTC", tz)
        return "UTC"
    return tz


@_with_cors_on_error
async def booking_slots(request: Request) -> JSONResponse:
    """GET /book/{slug}/slots?within_start=&within_end=&duration_min= —
    free {start, end} windows only, never event details or which calendar
    source they came from (card de858332).

    Superseded by /times, which returns bookable start times instead of raw
    windows. Kept because both live sites still divide the windows
    themselves; remove once neither does.
    """
    try:
        _link, windows, duration_min = _find_free(request)
    except _Refused as refusal:
        return refusal.response
    return _json(request, {"slots": windows, "duration_min": duration_min})


@_with_cors_on_error
async def booking_times(request: Request) -> JSONResponse:
    """GET /book/{slug}/times?within_start=&within_end=&duration_min=&granularity_min= —
    bookable start times, already cut from the free windows and aligned to
    the host's clock.

    /slots hands back windows and leaves the dividing to the caller, which
    meant every embedding site reimplemented it and got it wrong the same
    way: dividing "free 16:15-18:00" from its left edge offers 16:15, 16:45,
    17:15 — the leftovers of whatever meeting overran. Doing it here means
    one implementation, under test, and a browser that only has to render.
    """
    try:
        link, windows, duration_min = _find_free(request)
    except _Refused as refusal:
        return refusal.response

    q = request.query_params
    granularity_min = _clamp_granularity(
        q.get("granularity_min") or link.get("granularity_min")
    )
    times = subdivide(windows, duration_min, _host_timezone(link), granularity_min)
    return _json(
        request,
        {"times": times, "duration_min": duration_min, "granularity_min": granularity_min},
    )


def _refuse_unless_bookable(request: Request) -> dict:
    """Rate limit, resolve the link, check booking is on. Returns the link.

    The half of _find_free that doesn't touch the calendar. /config needs
    exactly this and nothing more: asking a CalDAV server for free windows to
    answer "what is your Turnstile key" would put a network round trip in
    front of the widget's very first paint.
    """
    if not _rate_limiter().check(f"booking:read:{_client_ip(request)}", limit=60, window_s=60):
        raise _Refused(_json(request, {"error": "rate_limited"}, status_code=429))

    link = get_link(request.path_params["slug"])
    if link is None:
        raise _Refused(_json(request, {"error": "not_found"}, status_code=404))

    enabled = t_cal.calendar_booking_enabled_get(_get_acl(), link["user"], link["project"])
    if not enabled.get("enabled"):
        raise _Refused(_json(request, {"error": "not_found"}, status_code=404))
    return link


@_with_cors_on_error
async def booking_config(request: Request) -> JSONResponse:
    """GET /book/{slug}/config — everything the embedded widget needs to draw
    itself before it knows a single free time.

    The widget is served from this gateway to any origin, so it can't carry
    per-host settings in its own source. It asks here instead. Nothing
    returned is a secret: the Turnstile *site* key is printed into every page
    that renders a captcha, and the meeting forms are the choices the visitor
    is about to be offered anyway.

    What is deliberately withheld is each form's `config` — that's where a
    phone form keeps the host's number, and a booking page has no business
    publishing it to anyone who curls the endpoint.
    """
    try:
        link = _refuse_unless_bookable(request)
    except _Refused as refusal:
        return refusal.response

    cfg = config.load().get("memaix", {}).get("booking", {})
    forms = t_cal.calendar_meeting_form_list(_get_acl(), link["user"], link["project"])
    return _json(request, {
        "duration_min": _clamp_duration(int(link.get("duration_min", 30))),
        "granularity_min": _clamp_granularity(link.get("granularity_min")),
        "timezone": _host_timezone(link),
        "max_days_ahead": link.get("max_days_ahead"),
        "turnstile_site_key": link.get("turnstile_site_key") or cfg.get("turnstile_site_key") or "",
        # None, not a default string: the widget carries its own wording per
        # language, and inventing one here would mean recording a consent the
        # visitor never read.
        "consent_text": link.get("consent_text") or None,
        "meeting_forms": [
            {"slug": f["slug"], "label": f.get("label") or f["slug"],
             "provider": f["provider"], "default": bool(f.get("default"))}
            for f in forms
        ],
    })


@_with_cors_on_error
async def booking_create(request: Request) -> JSONResponse:
    """POST /book/{slug} — {start, end, name, email, turnstile_token, purpose?}."""
    client_ip = _client_ip(request)
    if not _rate_limiter().check(f"booking:create:{client_ip}", limit=10, window_s=60):
        return _json(request, {"error": "rate_limited"}, status_code=429)

    link = get_link(request.path_params["slug"])
    if link is None:
        return _json(request, {"error": "not_found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}

    if not isinstance(body, dict):
        return _json(request, {"error": "invalid_body"}, status_code=400)

    if not await _verify_turnstile(str(body.get("turnstile_token") or ""), client_ip):
        return _json(request, {"error": "captcha_failed"}, status_code=403)

    # Server-side check, not just a frontend checkbox — otherwise "consent"
    # is theatre. consent_text is stored verbatim below so we can later
    # prove exactly what the visitor agreed to, not just that some box was
    # ticked.
    if body.get("consent") is not True:
        return _json(request, {"error": "consent_required"}, status_code=400)
    consent_text = str(body.get("consent_text") or "").strip()

    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or "").strip()
    purpose = str(body.get("purpose") or "").strip()[:_MAX_PURPOSE_LEN]
    meeting_form_slug = str(body.get("meeting_form_slug") or "").strip() or None
    # Display-only: which IANA zone to render times in inside the
    # confirmation email. Never used for scheduling — start/end below stay
    # the authoritative UTC instants. Missing/invalid -> emails show UTC.
    visitor_tz = str(body.get("timezone") or "").strip() or None
    start = _parse_dt(str(body.get("start") or ""))
    end = _parse_dt(str(body.get("end") or ""))
    if not name or not email or start is None or end is None or end <= start:
        return _json(request, {"error": "invalid_body"}, status_code=400)

    duration = end - start
    if not (timedelta(minutes=_MIN_DURATION_MIN) <= duration <= timedelta(minutes=_MAX_DURATION_MIN)):
        return _json(request, {"error": "invalid_duration"}, status_code=400)

    acl = _get_acl()
    project, host_user = link["project"], link["user"]
    enabled = t_cal.calendar_booking_enabled_get(acl, host_user, project)
    if not enabled.get("enabled"):
        return _json(request, {"error": "not_found"}, status_code=404)

    # Meeting forms (card 85854d2c): an empty list means the host never
    # configured any, which is identical to the pre-feature behaviour —
    # skip all of this and book with no video/phone form attached.
    forms = t_cal.calendar_meeting_form_list(acl, host_user, project)
    meeting_form = None
    if forms:
        if meeting_form_slug:
            meeting_form = next((f for f in forms if f["slug"] == meeting_form_slug), None)
            if meeting_form is None:
                return _json(request, {"error": "invalid_meeting_form"}, status_code=400)
        else:
            meeting_form = next((f for f in forms if f.get("default")), None)
            if meeting_form is None:
                return _json(request, {"error": "invalid_meeting_form"}, status_code=400)

    # _resolve_dav_filtered aggregates every enabled source (SA, registry,
    # public ICS...) into a read-only _MultiCalendarAdapter whenever more
    # than one is configured — exactly what the TOCTOU free-check below
    # wants, since a false "free" from ignoring a source would double-book.
    # calendar_create/_update/_delete need the opposite: a single adapter
    # that actually implements create_event/update_event/delete_event, the
    # same one the authenticated calendar_create MCP tool resolves via
    # _resolve_calendar_dav. Using _resolve_dav_filtered's result for the
    # write below crashes with AttributeError on _MultiCalendarAdapter.
    try:
        dav = _resolve_dav_filtered(project, host_user, acl)
        write_dav = _resolve_dav(project, host_user, write=True)
    except CalendarAuthRequired:
        return _json(request, {"error": "not_found"}, status_code=404)

    with _BOOKING_LOCKS[(project, host_user)]:
        # TOCTOU re-check, wrapped with calendar_create in the per-host-user
        # lock above (see the lock's own docstring for what that lock does
        # and doesn't guarantee today). calendar_find_free only returns a
        # slot whose window is strictly longer than the
        # requested duration (see its "we > cursor + duration" check) — pad
        # the query window by a minute so an exact-fit free block still shows
        # up here.
        still_free = t_cal.calendar_find_free(
            acl, host_user, project, int(duration.total_seconds() // 60),
            start.isoformat(), (end + timedelta(minutes=1)).isoformat(), _dav=dav,
        )
        def _covers(s: dict) -> bool:
            s_start, s_end = _parse_dt(s.get("start", "")), _parse_dt(s.get("end", ""))
            # Unparseable start/end -> not a usable free slot. Never crash the
            # public handler on a malformed calendar row; treat it as no cover.
            if s_start is None or s_end is None:
                return False
            return s_start <= start and s_end >= end

        if not any(_covers(s) for s in still_free):
            return _json(request, {"error": "slot_unavailable"}, status_code=409)

        title = link.get("title_template", "Möte").format(name=name) if "{name}" in link.get("title_template", "") else link.get("title_template", "Möte")

        meeting_detail = None
        if meeting_form is not None and meeting_form["provider"] != "google_meet":
            # Zoom/phone must resolve before calendar_create — the detail
            # gets embedded in the event's location, unlike Google Meet
            # whose link is a side effect of calendar_create itself.
            try:
                meeting_detail = resolve_meeting_detail(
                    meeting_form["provider"], meeting_form.get("config") or {},
                    acl, project, host_user, start=start, end=end, title=title,
                )
            except MeetingProviderError:
                logger.exception(
                    "meeting form resolution failed for project=%s host=%s slug=%s",
                    project, host_user, meeting_form["slug"],
                )
                return _json(request, {"error": "meeting_form_unavailable"}, status_code=502)

        event = t_cal.calendar_create(
            acl, host_user, project, title,
            start.isoformat(), end.isoformat(),
            attendees=[email],
            location=meeting_detail["join_url"] or meeting_detail["phone_number"] if meeting_detail else None,
            description=purpose or None,
            want_conference=bool(meeting_form is not None and meeting_form["provider"] == "google_meet"),
            _dav=write_dav, _confirmed=True,
        )

        if meeting_form is not None and meeting_form["provider"] == "google_meet":
            try:
                meeting_detail = resolve_meeting_detail(
                    "google_meet", {}, acl, project, host_user,
                    start=start, end=end, title=title, calendar_event=event,
                )
            except MeetingProviderError:
                logger.exception(
                    "meeting form resolution failed for project=%s host=%s slug=%s",
                    project, host_user, meeting_form["slug"],
                )
                # Unlike Zoom/phone (which resolve before the event exists),
                # calendar_create has already committed a Google event with
                # the visitor invited by this point — leaving it in place on
                # a 502 would silently book a real meeting with no Meet
                # link and no consent record. Undo it so the failure is
                # actually clean, same fail-closed guarantee as the Zoom
                # path gets for free.
                try:
                    t_cal.calendar_delete(acl, host_user, project, event["id"], _dav=write_dav)
                except Exception:
                    logger.exception(
                        "failed to roll back orphaned google_meet event=%s project=%s host=%s",
                        event.get("id"), project, host_user,
                    )
                return _json(request, {"error": "meeting_form_unavailable"}, status_code=502)

    meeting_form_slug_final = meeting_form["slug"] if meeting_form is not None else None
    meeting_form_provider = meeting_form["provider"] if meeting_form is not None else None
    meeting_detail_line = meeting_detail["display_text"] if meeting_detail is not None else None
    meeting_form_detail = (
        (meeting_detail["join_url"] or meeting_detail["phone_number"]) if meeting_detail is not None else None
    )

    _row_id, manage_token = get_consent_store().record(
        project=project, host_user=host_user, event_id=event.get("id"),
        visitor_email=email, consent_text=consent_text,
        consent_at=int(time.time()), meeting_end=int(end.timestamp()),
        slug=request.path_params["slug"], meeting_start=int(start.timestamp()),
        meeting_form_slug=meeting_form_slug_final, meeting_form_provider=meeting_form_provider,
        meeting_form_detail=meeting_form_detail,
    )
    # Lock released above — the booking is already committed to the
    # calendar, so email delivery is not part of the race-critical section
    # and its latency must never hold up the next booker for this host.
    _send_confirmation_emails(
        acl, project, link, title, event, name, email, purpose, start, end, visitor_tz, manage_token,
        meeting_detail_line,
    )
    return _json(request, {"ok": True, "start": event.get("start"), "end": event.get("end"), "manage_url": _manage_url(manage_token)})


def _format_dt(dt: datetime, tz_name: str | None) -> str:
    tz = None
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    local = dt.astimezone(tz) if tz is not None else dt.astimezone(timezone.utc)
    suffix = tz_name if tz is not None else "UTC"
    return f"{local.strftime('%Y-%m-%d %H:%M')} ({suffix})"


def _build_ics(
    uid: str, title: str, start: datetime, end: datetime,
    description: str | None, attendees: list[str],
    location: str | None = None,
) -> bytes:
    import vobject
    from vobject.icalendar import utc as vobject_utc

    # vobject only recognizes its own utc tzinfo (dateutil's tzutc()) when
    # picking a TZID to serialize — a stdlib datetime.timezone.utc-aware
    # datetime makes it raise VObjectError("Unable to guess TZID...").
    start = start.astimezone(vobject_utc)
    end = end.astimezone(vobject_utc)

    cal = vobject.iCalendar()
    vevent = cal.add("vevent")
    vevent.add("uid").value = uid
    vevent.add("summary").value = title
    vevent.add("dtstart").value = start
    vevent.add("dtend").value = end
    if description:
        vevent.add("description").value = description
    if location:
        vevent.add("location").value = location
    for attendee in attendees:
        vevent.add("attendee").value = attendee
    return cal.serialize().encode("utf-8")


def _confirmation_body(
    title: str, name: str, visitor_email: str, when: str, purpose: str, manage_url: str,
    meeting_detail_line: str | None = None,
) -> str:
    lines = [f"Mötet är bokat: {title}", f"Tid: {when}"]
    if meeting_detail_line:
        lines.append(meeting_detail_line)
    if purpose:
        lines.append(f"Syfte: {purpose}")
    lines.append(f"Bokat av: {name} <{visitor_email}>")
    lines.append("En kalenderfil (.ics) är bifogad.")
    lines.append(f"Vill du boka om eller avboka? {manage_url}")
    return "\n".join(lines)


def _reschedule_body(title: str, when: str, manage_url: str, meeting_detail_line: str | None = None) -> str:
    lines = [
        f"Mötet är ombokat: {title}",
        f"Ny tid: {when}",
    ]
    if meeting_detail_line:
        lines.append(meeting_detail_line)
    lines.append("En uppdaterad kalenderfil (.ics) är bifogad.")
    lines.append(f"Vill du boka om igen eller avboka? {manage_url}")
    return "\n".join(lines)


def _cancellation_body(title: str, when: str) -> str:
    return f"Mötet är avbokat: {title}\nTid som avbokades: {when}"


def _format_meeting_detail_line(provider: str | None, detail: str | None) -> str | None:
    """Rebuilds the same display line meeting_providers.py's MeetingDetail
    produced at booking time, from the two columns consent_store persisted
    — used by reschedule/reminders, which never re-resolve a provider
    (see consent_store.py's migration comment for why)."""
    if not provider or not detail:
        return None
    label = {"google_meet": "Google Meet", "zoom": "Zoom", "phone": "Ring"}.get(provider)
    return f"{label}: {detail}" if label else None


def _manage_url(manage_token: str, link: dict | None = None) -> str:
    frontend = (link or {}).get("manage_frontend_url", "")
    if frontend:
        return f"{frontend.rstrip('/')}?token={manage_token}"
    cfg = config.load()
    public_url = cfg.get("memaix", {}).get("server", {}).get("public_url", "http://localhost:8080")
    return f"{public_url.rstrip('/')}/booking/{manage_token}"


def _send_confirmation_emails(
    acl, project: str, link: dict, title: str, event: dict,
    name: str, visitor_email: str, purpose: str,
    start: datetime, end: datetime, visitor_tz: str | None,
    manage_token: str, meeting_detail_line: str | None = None,
) -> None:
    """Best-effort only. The booking already succeeded by the time this
    runs — a project with no mailbox/allow_send configured yet, or a
    transient SMTP error, must never turn into a booking failure for the
    visitor. See the module docstring for why.

    Both copies get the manage link (card 8056150d) — the manage_token IS
    the capability to reschedule/cancel, so either the visitor or the host
    can act on it, whichever notices a change is needed first."""
    try:
        host_user = link["user"]
        if not acl.resource(project, "allow_send"):
            return
        uid = event.get("id") or _uuid.uuid4().hex
        ics_bytes = _build_ics(uid, title, start, end, purpose or None, [visitor_email], meeting_detail_line)
        manage_url = _manage_url(manage_token, link)

        t_email.email_send(
            acl, host_user, project, visitor_email,
            f"Bekräftelse: {title}",
            _confirmation_body(
                title, name, visitor_email, _format_dt(start, visitor_tz), purpose, manage_url,
                meeting_detail_line,
            ),
            attachment_filename="moete.ics", attachment_content=ics_bytes,
            _confirmed=True,
        )

        host_email = link.get("host_email")
        if host_email:
            t_email.email_send(
                acl, host_user, project, host_email,
                f"Ny bokning: {title}",
                _confirmation_body(
                    title, name, visitor_email, _format_dt(start, link.get("host_timezone")), purpose, manage_url,
                    meeting_detail_line,
                ),
                attachment_filename="moete.ics", attachment_content=ics_bytes,
                _confirmed=True,
            )
    except Exception:
        logger.exception("booking confirmation email failed for project=%s slug-host=%s", project, link.get("user"))


def _send_reschedule_emails(acl, project: str, link: dict, title: str, event: dict,
                             visitor_email: str, start: datetime, end: datetime, manage_token: str,
                             meeting_detail_line: str | None = None,
                             meeting_form_detail: str | None = None) -> None:
    """Best-effort, same contract as _send_confirmation_emails."""
    try:
        host_user = link["user"]
        if not acl.resource(project, "allow_send"):
            return
        uid = event.get("id") or _uuid.uuid4().hex
        ics_bytes = _build_ics(uid, title, start, end, None, [visitor_email], meeting_form_detail)
        manage_url = _manage_url(manage_token, link)

        t_email.email_send(
            acl, host_user, project, visitor_email,
            f"Ombokat: {title}",
            _reschedule_body(title, _format_dt(start, None), manage_url, meeting_detail_line),
            attachment_filename="moete.ics", attachment_content=ics_bytes,
            _confirmed=True,
        )
        host_email = link.get("host_email")
        if host_email:
            t_email.email_send(
                acl, host_user, project, host_email,
                f"Ombokat: {title}",
                _reschedule_body(title, _format_dt(start, link.get("host_timezone")), manage_url, meeting_detail_line),
                attachment_filename="moete.ics", attachment_content=ics_bytes,
                _confirmed=True,
            )
    except Exception:
        logger.exception("booking reschedule email failed for project=%s slug-host=%s", project, link.get("user"))


def _send_cancellation_emails(acl, project: str, link: dict, title: str, visitor_email: str, when: str) -> None:
    """Best-effort, same contract as _send_confirmation_emails."""
    try:
        host_user = link["user"]
        if not acl.resource(project, "allow_send"):
            return
        t_email.email_send(
            acl, host_user, project, visitor_email,
            f"Avbokat: {title}", _cancellation_body(title, when), _confirmed=True,
        )
        host_email = link.get("host_email")
        if host_email:
            t_email.email_send(
                acl, host_user, project, host_email,
                f"Avbokat: {title}", _cancellation_body(title, when), _confirmed=True,
            )
    except Exception:
        logger.exception("booking cancellation email failed for project=%s slug-host=%s", project, link.get("user"))


def _reminder_body(
    title: str, when: str, offset_min: int, manage_url: str, meeting_detail_line: str | None = None,
) -> str:
    lead = "imorgon" if offset_min >= 1440 else "om en timme"
    lines = [
        f"Påminnelse: {title} börjar {lead}.",
        f"Tid: {when}",
    ]
    if meeting_detail_line:
        lines.append(meeting_detail_line)
    lines.append("En kalenderfil (.ics) är bifogad.")
    lines.append(f"Behöver du boka om eller avboka? {manage_url}")
    return "\n".join(lines)


def _send_reminder_email(
    acl, project: str, link: dict, title: str, event_id: str | None,
    visitor_email: str, meeting_start: datetime, meeting_end: datetime,
    offset_min: int, manage_token: str, meeting_detail_line: str | None = None,
    meeting_form_detail: str | None = None,
) -> None:
    """Best-effort, same contract as _send_confirmation_emails. Called from
    reminders.py's send_due_reminders() — see that module for the
    claim-after-send ordering this depends on to avoid losing a reminder to
    a transient failure in here."""
    try:
        host_user = link["user"]
        if not acl.resource(project, "allow_send"):
            return
        uid = event_id or _uuid.uuid4().hex
        ics_bytes = _build_ics(uid, title, meeting_start, meeting_end, None, [visitor_email], meeting_form_detail)
        manage_url = _manage_url(manage_token, link)
        when = _format_dt(meeting_start, None)

        t_email.email_send(
            acl, host_user, project, visitor_email,
            f"Påminnelse: {title}",
            _reminder_body(title, when, offset_min, manage_url, meeting_detail_line),
            attachment_filename="moete.ics", attachment_content=ics_bytes,
            _confirmed=True,
        )
        host_email = link.get("host_email")
        if host_email:
            t_email.email_send(
                acl, host_user, project, host_email,
                f"Påminnelse: {title}",
                _reminder_body(
                    title, _format_dt(meeting_start, link.get("host_timezone")), offset_min, manage_url,
                    meeting_detail_line,
                ),
                attachment_filename="moete.ics", attachment_content=ics_bytes,
                _confirmed=True,
            )
    except Exception:
        logger.exception("booking reminder email failed for project=%s slug-host=%s", project, link.get("user"))


@_with_cors_on_error
async def booking_manage_get(request: Request) -> JSONResponse:
    """GET /booking/{token} — booking state for whoever holds the manage token.

    Returns {status, meeting_end, slug, duration_min, host_timezone}.
    slug + duration_min let the manage page fetch available slots for
    rescheduling without a separate config call. Never exposes anything
    beyond what is already visible to whoever holds the token."""
    row = get_consent_store().get_by_manage_token(request.path_params["token"])
    if row is None:
        return _json(request, {"error": "not_found"}, status_code=404)
    link = get_link(row["slug"]) if row.get("slug") else None
    return _json(request, {
        "status": row["status"],
        "meeting_end": row["meeting_end"],
        "slug": row.get("slug"),
        "duration_min": (link or {}).get("duration_min"),
        "host_timezone": (link or {}).get("host_timezone"),
    })


@_with_cors_on_error
async def booking_reschedule(request: Request) -> JSONResponse:
    """POST /booking/{token}/reschedule — {start, end}. The token is the
    capability (card 8056150d) — no turnstile, but still IP rate-limited."""
    client_ip = _client_ip(request)
    if not _rate_limiter().check(f"booking-manage:{client_ip}", limit=10, window_s=60):
        return _json(request, {"error": "rate_limited"}, status_code=429)

    row = get_consent_store().get_by_manage_token(request.path_params["token"])
    if row is None:
        return _json(request, {"error": "not_found"}, status_code=404)
    if row["status"] == "cancelled":
        return _json(request, {"error": "already_cancelled"}, status_code=409)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return _json(request, {"error": "invalid_body"}, status_code=400)

    start = _parse_dt(str(body.get("start") or ""))
    end = _parse_dt(str(body.get("end") or ""))
    if start is None or end is None or end <= start:
        return _json(request, {"error": "invalid_body"}, status_code=400)
    duration = end - start
    if not (timedelta(minutes=_MIN_DURATION_MIN) <= duration <= timedelta(minutes=_MAX_DURATION_MIN)):
        return _json(request, {"error": "invalid_duration"}, status_code=400)

    link = get_link(row["slug"]) if row["slug"] else None
    if link is None:
        return _json(request, {"error": "not_found"}, status_code=404)

    acl = _get_acl()
    project, host_user, event_id = row["project"], row["host_user"], row["event_id"]
    try:
        # See booking_create for why the free-check and the write need two
        # different adapters: _resolve_dav_filtered's merged read-only view
        # for the former, a real single writable adapter for the latter.
        dav = _resolve_dav_filtered(project, host_user, acl)
        write_dav = _resolve_dav(project, host_user, write=True)
    except CalendarAuthRequired:
        return _json(request, {"error": "not_found"}, status_code=404)

    with _BOOKING_LOCKS[(project, host_user)]:
        # Same TOCTOU re-check booking_create does — a reschedule races
        # other bookers for the new window exactly like a fresh booking.
        still_free = t_cal.calendar_find_free(
            acl, host_user, project, int(duration.total_seconds() // 60),
            start.isoformat(), (end + timedelta(minutes=1)).isoformat(), _dav=dav,
            _exclude_event_id=event_id,
        )

        def _covers(s: dict) -> bool:
            s_start, s_end = _parse_dt(s.get("start", "")), _parse_dt(s.get("end", ""))
            if s_start is None or s_end is None:
                return False
            return s_start <= start and s_end >= end

        if not any(_covers(s) for s in still_free):
            return _json(request, {"error": "slot_unavailable"}, status_code=409)

        event = t_cal.calendar_update(
            acl, host_user, project, event_id,
            start=start.isoformat(), end=end.isoformat(),
            _dav=write_dav, _confirmed=True,
        )

    get_consent_store().update_booking(
        row["id"], event_id=event_id, meeting_start=int(start.timestamp()),
        meeting_end=int(end.timestamp()), status="rescheduled",
    )

    # The Zoom meeting itself isn't moved (no zoom meeting id is stored,
    # only the resolved join_url — card 85854d2c's follow-up if this turns
    # out to matter). Google Meet needs nothing: Google preserves
    # conferenceData on a PATCH that doesn't touch it. Phone needs nothing.
    # The stored link/number is still shown to the visitor either way.
    title = event.get("title") or link.get("title_template", "Möte")
    meeting_detail_line = _format_meeting_detail_line(row.get("meeting_form_provider"), row.get("meeting_form_detail"))
    _send_reschedule_emails(
        acl, project, link, title, event, row["visitor_email"], start, end, request.path_params["token"],
        meeting_detail_line, row.get("meeting_form_detail"),
    )
    return _json(request, {"ok": True, "start": event.get("start"), "end": event.get("end")})


@_with_cors_on_error
async def booking_cancel(request: Request) -> JSONResponse:
    """POST /booking/{token}/cancel — the token is the capability."""
    client_ip = _client_ip(request)
    if not _rate_limiter().check(f"booking-manage:{client_ip}", limit=10, window_s=60):
        return _json(request, {"error": "rate_limited"}, status_code=429)

    row = get_consent_store().get_by_manage_token(request.path_params["token"])
    if row is None:
        return _json(request, {"error": "not_found"}, status_code=404)
    if row["status"] == "cancelled":
        return _json(request, {"error": "already_cancelled"}, status_code=409)

    link = get_link(row["slug"]) if row["slug"] else None
    project, host_user, event_id = row["project"], row["host_user"], row["event_id"]
    acl = _get_acl()

    if event_id:
        try:
            # Deleting is a write — needs the real single adapter, not
            # _resolve_dav_filtered's merged read-only view (see
            # booking_create for the full explanation).
            dav = _resolve_dav(project, host_user, write=True)
            try:
                t_cal.calendar_delete(acl, host_user, project, event_id, _dav=dav)
            except FileNotFoundError:
                # Already gone (host deleted it manually) — nothing left to
                # delete, same reasoning as purge.py.
                pass
        except CalendarAuthRequired:
            # Host revoked calendar access — nothing left to delete via
            # that adapter, still cancel the booking record below.
            pass

    get_consent_store().update_booking(
        row["id"], event_id=event_id, meeting_start=row["meeting_start"],
        meeting_end=row["meeting_end"], status="cancelled",
    )

    if link is not None:
        title = link.get("title_template", "Möte")
        when = _format_dt(datetime.fromtimestamp(row["meeting_end"], tz=timezone.utc), link.get("host_timezone"))
        _send_cancellation_emails(acl, project, link, title, row["visitor_email"], when)
    return _json(request, {"ok": True})


def booking_options(request: Request) -> Response:
    return Response(status_code=204, headers=_cors_headers(request))


_WIDGET_JS = Path(__file__).parent / "static" / "booking.js"


def booking_widget(request: Request) -> Response:
    """GET /embed/booking.js — the booking UI, as one file anybody can
    <script src> from their own page.

    The grid, the form and the Turnstile dance used to be copied by hand into
    every site that wanted booking, which is how two sites shipped the same
    broken slot grid on the same day. Serving it from here makes the embedder's
    job a div and a script tag, and makes a fix reach every site on deploy.

    Cache-Control is `no-cache`, not a long max-age: embedders write a bare
    script tag they will never think about again, so the URL has to stay the
    same forever and the freshness has to come from revalidation. A caller
    that does version it (?v=…) opts into the immutable answer instead. The
    wildcard CORS header is redundant for a plain script tag but makes the
    file usable as a module import too, and it protects nothing — this is a
    public asset by definition.
    """
    if not _WIDGET_JS.is_file():
        # Packaging decides whether this file ships, and package-data is a
        # whitelist — miss the entry and the file is simply absent from the
        # installed wheel. FileResponse raises RuntimeError on a missing path,
        # which Starlette turns into a 500 with no body worth reading; a 404
        # at least tells the embedder their gateway has no widget rather than
        # that it fell over.
        return Response("booking widget not installed", status_code=404, media_type="text/plain")

    cache = "public, max-age=31536000, immutable" if request.query_params.get("v") else "no-cache"
    return FileResponse(
        _WIDGET_JS,
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": cache, "Access-Control-Allow-Origin": "*"},
    )


def _rate_limiter():
    from ..safety.rate_limit import rate_limiter

    return rate_limiter


booking_routes = [
    Route("/embed/booking.js", booking_widget, methods=["GET"]),
    Route("/book/{slug}/config", booking_config, methods=["GET"]),
    Route("/book/{slug}/config", booking_options, methods=["OPTIONS"]),
    Route("/book/{slug}/slots", booking_slots, methods=["GET"]),
    Route("/book/{slug}/slots", booking_options, methods=["OPTIONS"]),
    Route("/book/{slug}/times", booking_times, methods=["GET"]),
    Route("/book/{slug}/times", booking_options, methods=["OPTIONS"]),
    Route("/book/{slug}", booking_create, methods=["POST"]),
    Route("/book/{slug}", booking_options, methods=["OPTIONS"]),
    Route("/booking/{token}", booking_manage_get, methods=["GET"]),
    Route("/booking/{token}", booking_options, methods=["OPTIONS"]),
    Route("/booking/{token}/reschedule", booking_reschedule, methods=["POST"]),
    Route("/booking/{token}/reschedule", booking_options, methods=["OPTIONS"]),
    Route("/booking/{token}/cancel", booking_cancel, methods=["POST"]),
    Route("/booking/{token}/cancel", booking_options, methods=["OPTIONS"]),
]
