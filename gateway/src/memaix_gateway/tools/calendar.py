# SPDX-License-Identifier: AGPL-3.0-or-later
"""calendar_* tools — CalDAV or Google Calendar REST with injected client for testability.

The _dav keyword argument accepts a duck-typed object.  When None,
a real caldav.DAVClient connection is created from project config.

_dav duck type (must implement):
  list_events(start: datetime, end: datetime) -> list[dict]
    where dict has at minimum: id, title, start, end
  create_event(uid, title, start, end, attendees, location, description) -> dict
  update_event(id, **fields) -> dict
  delete_event(id) -> None
  find_events(start: datetime, end: datetime) -> list[dict]  (same as list_events)

For real CalDAV the adapter is inline below (_RealDavAdapter).

Outbox gate:
  calendar_create/calendar_update are routed through the approval outbox (see
  outbox/policy.py) exactly like email_send — when action_mode() resolves to
  'review', the call is queued and returns {"pending": True, "action_id": ...}
  instead of touching the calendar. _confirmed=True (used by outbox.execute
  after approval) always executes immediately.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import config
from ..acl import Acl

_ICAL_READ_ONLY = "iCal feed is read-only — use calendar_setup mode=oauth for write access"
_FREEBUSY_READ_ONLY = "FreeBusy mode is read-only — use calendar_setup mode=oauth for write access"


class CalendarAuthRequired(Exception):
    """Raised when the user has no linked calendar account for this project."""

    def __init__(self, link_url: str, options: list[dict] | None = None) -> None:
        self.link_url = link_url
        self.options = options or []
        super().__init__("auth_required: configure calendar via calendar_setup")


# ------------------------------------------------------------------
# Per-user Google Calendar REST adapter
# ------------------------------------------------------------------


class _PerUserGoogleAdapter:
    """Google Calendar REST API v3 using a per-user OAuth access token."""

    _BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, access_token: str) -> None:
        self._token = access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _get(self, path: str, **params) -> dict:
        import requests
        r = requests.get(f"{self._BASE}{path}", headers=self._headers(), params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict, **params) -> dict:
        import requests
        r = requests.post(f"{self._BASE}{path}", headers=self._headers(), json=body, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, body: dict) -> dict:
        import requests
        r = requests.patch(f"{self._BASE}{path}", headers=self._headers(), json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> None:
        import requests
        r = requests.delete(f"{self._BASE}{path}", headers=self._headers(), timeout=10)
        r.raise_for_status()

    @staticmethod
    def _to_dict(item: dict) -> dict:
        start = item.get("start", {})
        end = item.get("end", {})
        # originalStartTime is present on an expanded singleEvents=true
        # instance iff Google materialized a distinct event object for it —
        # i.e. it was individually modified (title, attendees, time, ...).
        # A clean occurrence of a series never has this key at all. Do NOT
        # compare against start: an exception whose *time* is unchanged
        # (e.g. only its title changed) would otherwise be misclassified as
        # a normal occurrence and wrongly inherit a series override.
        is_exception = bool(item.get("originalStartTime"))
        return {
            "id": item.get("id", ""),
            "title": item.get("summary", ""),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "location": item.get("location", ""),
            "description": item.get("description", ""),
            # memaix-src card c7698ff3 — series identity for per-event overrides.
            # singleEvents=true (list_events below) expands recurrences and
            # populates recurringEventId on each instance of a series.
            "series_id": item.get("recurringEventId"),
            "is_exception": is_exception,
            # Google: transparency:"transparent" == Free, default "opaque" == Busy.
            "source_busy": item.get("transparency", "opaque") != "transparent",
            # memaix-src card 85854d2c — set only on a create_event(want_conference=True)
            # response; entryPointType "video" is the Meet join link, Google also
            # returns "more" entry points (phone dial-in, sip) we don't surface here.
            "meet_url": next(
                (ep.get("uri") for ep in item.get("conferenceData", {}).get("entryPoints", [])
                 if ep.get("entryPointType") == "video"),
                None,
            ),
        }

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        from urllib.parse import quote as _quote

        time_min = start.isoformat() if start.tzinfo else start.isoformat() + "Z"
        time_max = end.isoformat() if end.tzinfo else end.isoformat() + "Z"

        try:
            cal_list = self._get("/users/me/calendarList", minAccessRole="reader")
            calendar_ids = [c["id"] for c in cal_list.get("items", [])]
        except Exception:
            calendar_ids = []
        if not calendar_ids:
            calendar_ids = ["primary"]

        seen: set[str] = set()
        events: list[dict] = []
        for cal_id in calendar_ids:
            try:
                data = self._get(
                    f"/calendars/{_quote(cal_id, safe='')}/events",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents="true",
                    orderBy="startTime",
                )
            except Exception:
                continue
            for e in data.get("items", []):
                ev = self._to_dict(e)
                if ev["id"] not in seen:
                    seen.add(ev["id"])
                    events.append(ev)

        return sorted(events, key=lambda e: e.get("start", ""))

    find_events = list_events

    def create_event(
        self,
        uid: str,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str] | None = None,
        location: str | None = None,
        description: str | None = None,
        want_conference: bool = False,
    ) -> dict:
        body: dict = {
            "summary": title,
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        params: dict = {}
        if want_conference:
            # requestId reuses the event uid we already generated —
            # idempotent if this create is ever retried.
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": uid,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            params["conferenceDataVersion"] = 1
        return self._to_dict(self._post("/calendars/primary/events", body, **params))

    def update_event(self, id: str, **fields) -> dict:
        body: dict = {}
        if "title" in fields:
            body["summary"] = fields["title"]
        if "location" in fields:
            body["location"] = fields["location"]
        if "description" in fields:
            body["description"] = fields["description"]
        if "start" in fields:
            body["start"] = {"dateTime": fields["start"], "timeZone": "UTC"}
        if "end" in fields:
            body["end"] = {"dateTime": fields["end"], "timeZone": "UTC"}
        # want_conference is intentionally not handled here: Google keeps a
        # PATCH's existing conferenceData untouched unless resent, so a
        # reschedule of a google_meet booking retains its original Meet
        # link without any extra work — verify this against Google's
        # interactive API reference if a future report says otherwise.
        return self._to_dict(self._patch(f"/calendars/primary/events/{id}", body))

    def delete_event(self, id: str) -> None:
        self._delete(f"/calendars/primary/events/{id}")


# ------------------------------------------------------------------
# iCal secret-URL adapter (read-only)
# ------------------------------------------------------------------


class _ICalAdapter:
    """Fetches a secret iCal URL and returns events in the time range."""

    def __init__(self, ical_url: str) -> None:
        self._url = ical_url

    def _fetch(self) -> list[dict]:
        from datetime import date, timezone

        import requests
        import vobject

        from ..safety.net import validate_external_url

        validate_external_url(self._url)  # authoritative SSRF check before fetching the secret iCal URL
        # allow_redirects=False: validate_external_url() prövar bara URL:en vi skickar.
        # requests följer annars 3xx vart som helst, och guarden ser aldrig målet —
        # en angriparkontrollerad iCal-värd kan svara 302 mot 169.254.169.254.
        r = requests.get(self._url, timeout=10, allow_redirects=False)
        r.raise_for_status()
        cal = vobject.readOne(r.text)
        vevents = [c for c in cal.components() if c.name == "VEVENT"]

        # memaix-src card c7698ff3 — series identity. vobject's readOne does
        # not expand RRULE, so what we see is masters (RRULE) + any explicit
        # exception instances (RECURRENCE-ID) sharing the master's UID. A UID
        # is a series iff any component under it carries either marker.
        def _uid_of(c) -> str:
            return str(getattr(c, "uid", "")).strip()

        series_uids = {
            _uid_of(c)
            for c in vevents
            if _uid_of(c) and (hasattr(c, "rrule") or hasattr(c, "recurrence_id"))
        }

        events: list[dict] = []
        for component in vevents:
            dtstart = component.dtstart.value
            dtend = getattr(component, "dtend", None)
            dtend = dtend.value if dtend else dtstart

            # Normalize date → datetime
            if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
                dtstart = datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=timezone.utc)
            if isinstance(dtend, date) and not isinstance(dtend, datetime):
                dtend = datetime(dtend.year, dtend.month, dtend.day, tzinfo=timezone.utc)

            uid = _uid_of(component)
            transp = str(getattr(component, "transp", "")).strip().upper()

            events.append({
                "id": uid or f"ical-{len(events)}",
                "title": str(getattr(component, "summary", "")).strip(),
                "start": dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart),
                "end": dtend.isoformat() if isinstance(dtend, datetime) else str(dtend),
                "location": str(getattr(component, "location", "")).strip(),
                "description": str(getattr(component, "description", "")).strip(),
                "series_id": uid if uid in series_uids else None,
                "is_exception": hasattr(component, "recurrence_id"),
                "source_busy": transp != "TRANSPARENT",
                "_dtstart": dtstart,
                "_dtend": dtend,
            })
        return events

    def _in_range(self, event: dict, start: datetime, end: datetime) -> bool:
        from datetime import timezone
        def _tz(dt: datetime) -> datetime:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        ev_start = _tz(event["_dtstart"]) if isinstance(event["_dtstart"], datetime) else _tz(start)
        ev_end = _tz(event["_dtend"]) if isinstance(event["_dtend"], datetime) else _tz(end)
        s = _tz(start)
        e = _tz(end)
        return ev_start < e and ev_end > s

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        raw = self._fetch()
        filtered = [e for e in raw if self._in_range(e, start, end)]
        # Strip internal keys before returning
        return [{k: v for k, v in e.items() if not k.startswith("_")} for e in filtered]

    find_events = list_events

    def create_event(self, *args, **kwargs):
        raise NotImplementedError(_ICAL_READ_ONLY)

    def update_event(self, *args, **kwargs):
        raise NotImplementedError(_ICAL_READ_ONLY)

    def delete_event(self, *args, **kwargs):
        raise NotImplementedError(_ICAL_READ_ONLY)


# ------------------------------------------------------------------
# Google FreeBusy adapter (read-only, no event titles)
# ------------------------------------------------------------------


class _FreeBusyAdapter:
    """Queries Google FreeBusy API — returns only busy blocks, no event details."""

    _ENDPOINT = "https://www.googleapis.com/calendar/v3/freeBusy"

    def __init__(self, calendar_id: str, api_key: str) -> None:
        self._calendar_id = calendar_id
        self._api_key = api_key

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        from datetime import timezone

        import requests

        def _iso(dt: datetime) -> str:
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()

        r = requests.post(
            self._ENDPOINT,
            params={"key": self._api_key},
            json={
                "timeMin": _iso(start),
                "timeMax": _iso(end),
                "items": [{"id": self._calendar_id}],
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        busy = data.get("calendars", {}).get(self._calendar_id, {}).get("busy", [])
        return [
            {"id": f"busy-{i}", "title": "Busy", "start": b["start"], "end": b["end"], "location": "", "description": ""}
            for i, b in enumerate(busy)
        ]

    find_events = list_events

    def create_event(self, *args, **kwargs):
        raise NotImplementedError(_FREEBUSY_READ_ONLY)

    def update_event(self, *args, **kwargs):
        raise NotImplementedError(_FREEBUSY_READ_ONLY)

    def delete_event(self, *args, **kwargs):
        raise NotImplementedError(_FREEBUSY_READ_ONLY)


# ------------------------------------------------------------------
# Google service account adapter (domain-wide delegation, read-only)
# ------------------------------------------------------------------


class _ServiceAccountGoogleCalendarAdapter:
    """Google Calendar via service account med domain-wide delegation."""

    def __init__(self, sa_info: dict, impersonate_email: str):
        import importlib.util

        if importlib.util.find_spec("googleapiclient") is None:
            raise RuntimeError(
                "google-auth och google-api-python-client måste installeras"
            )
        try:
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError(
                "google-auth och google-api-python-client måste installeras"
            ) from exc

        scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
        self._creds = (
            service_account.Credentials.from_service_account_info(
                sa_info, scopes=scopes
            ).with_subject(impersonate_email)
        )
        self._email = impersonate_email

    def _build(self):
        import googleapiclient.discovery
        return googleapiclient.discovery.build(
            "calendar", "v3", credentials=self._creds, cache_discovery=False
        )

    @staticmethod
    def _to_dict(item: dict) -> dict:
        start = item.get("start") or {}
        end = item.get("end") or {}
        return {
            "id": item.get("id", ""),
            "title": item.get("summary", ""),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "location": item.get("location", ""),
            "description": item.get("description", ""),
        }

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        time_min = start.isoformat() if start.tzinfo else start.isoformat() + "Z"
        time_max = end.isoformat() if end.tzinfo else end.isoformat() + "Z"
        svc = self._build()
        calendars_resp = svc.calendarList().list().execute()
        calendars = calendars_resp.get("items", [])
        events: list[dict] = []
        for cal in calendars:
            cal_id = cal["id"]
            try:
                resp = (
                    svc.events()
                    .list(
                        calendarId=cal_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=250,
                    )
                    .execute()
                )
                events.extend(self._to_dict(ev) for ev in resp.get("items", []))
            except Exception:
                pass
        return events

    find_events = list_events


# ------------------------------------------------------------------
# Multi-calendar merge adapter
# ------------------------------------------------------------------


class _MultiCalendarAdapter:
    """Slår ihop resultat från flera kalenderadapters."""

    def __init__(self, adapters: list):
        self._adapters = adapters

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        seen: set[tuple] = set()
        merged: list[dict] = []
        for adapter in self._adapters:
            try:
                events = adapter.list_events(start, end)
            except Exception:
                events = []
            for ev in events:
                key = (ev.get("title", ""), ev.get("start", ""))
                if key not in seen:
                    seen.add(key)
                    merged.append(ev)
        merged.sort(key=lambda e: e.get("start", ""))
        return merged

    find_events = list_events


# ------------------------------------------------------------------
# Real CalDAV adapter (wraps caldav library)
# ------------------------------------------------------------------


class _RealDavAdapter:
    def __init__(self, acl: Acl, project: str) -> None:
        # caldav's package __init__ exposes DAVClient via a lazy PEP 562
        # __getattr__ typed to return `object`, which makes mypy treat
        # `caldav.DAVClient(...)` as calling a non-callable. Importing the
        # real class from its submodule keeps the actual type.
        from caldav.davclient import DAVClient

        cfg = acl.resource(project, "calendar")
        if not cfg:
            raise ValueError(f"project {project!r} has no calendar configured")
        password = config.secret(cfg.get("password_ref"))
        client = DAVClient(
            url=cfg["url"],
            username=cfg.get("user", ""),
            password=password,
        )
        principal = client.principal()
        cals = principal.calendars()
        if not cals:
            raise RuntimeError("no calendars found for project")
        self._cal = cals[0]

    def _vevent(self, event):
        return event.vobject_instance.vevent

    def _event_to_dict(self, event) -> dict:
        ve = self._vevent(event)
        transp = str(ve.transp.value).strip().upper() if hasattr(ve, "transp") else ""
        # memaix-src card c7698ff3 — same UID-based series detection as
        # _ICalAdapter (date_search(expand=True) expands recurrences, so an
        # instance carries RECURRENCE-ID + shares its master's UID).
        has_series_marker = hasattr(ve, "rrule") or hasattr(ve, "recurrence_id")
        return {
            "id": str(ve.uid.value),
            "title": str(ve.summary.value) if hasattr(ve, "summary") else "",
            "start": str(ve.dtstart.value),
            "end": str(ve.dtend.value) if hasattr(ve, "dtend") else "",
            "location": str(ve.location.value) if hasattr(ve, "location") else "",
            "description": str(ve.description.value) if hasattr(ve, "description") else "",
            "series_id": str(ve.uid.value) if has_series_marker else None,
            "is_exception": hasattr(ve, "recurrence_id"),
            "source_busy": transp != "TRANSPARENT",
        }

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        events = self._cal.date_search(start=start, end=end, expand=True)
        return [self._event_to_dict(e) for e in events]

    find_events = list_events

    def create_event(
        self,
        uid: str,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str] | None = None,
        location: str | None = None,
        description: str | None = None,
        want_conference: bool = False,
    ) -> dict:
        # CalDAV has no conferenceData concept — accepted for duck-type
        # compatibility with _PerUserGoogleAdapter and ignored.
        del want_conference
        import vobject
        from vobject.icalendar import utc as vobject_utc

        # vobject only recognizes its own utc tzinfo (dateutil's tzutc())
        # when picking a TZID at serialize() time — a stdlib
        # datetime.timezone.utc-aware datetime (what booking_create and
        # calendar_create's _parse_dt/normalization produce) makes it raise
        # VObjectError("Unable to guess TZID..."). Pre-existing bug, found
        # while adding a second, unrelated vobject.iCalendar() caller
        # (booking/routes.py._build_ics, card 14666e8a) that hit the same
        # crash and made it obvious this path was never exercised
        # end-to-end against a real CalDAV target with a UTC time.
        if start.tzinfo is not None:
            start = start.astimezone(vobject_utc)
        if end.tzinfo is not None:
            end = end.astimezone(vobject_utc)

        cal = vobject.iCalendar()
        event = cal.add("vevent")
        event.add("uid").value = uid
        event.add("summary").value = title
        event.add("dtstart").value = start
        event.add("dtend").value = end
        if location:
            event.add("location").value = location
        if description:
            event.add("description").value = description
        if attendees:
            for att in attendees:
                event.add("attendee").value = att
        self._cal.save_event(cal.serialize())
        return {"id": uid, "title": title, "start": str(start), "end": str(end)}

    def update_event(self, id: str, **fields) -> dict:
        events = self._cal.search(uid=id)
        if not events:
            raise FileNotFoundError(f"event not found: {id!r}")
        event = events[0]
        ve = self._vevent(event)
        if "title" in fields:
            ve.summary.value = fields["title"]
        if "location" in fields and hasattr(ve, "location"):
            ve.location.value = fields["location"]
        if "description" in fields and hasattr(ve, "description"):
            ve.description.value = fields["description"]
        if "start" in fields:
            ve.dtstart.value = datetime.fromisoformat(fields["start"])
        if "end" in fields:
            ve.dtend.value = datetime.fromisoformat(fields["end"])
        event.save()
        return self._event_to_dict(event)

    def delete_event(self, id: str) -> None:
        events = self._cal.search(uid=id)
        if not events:
            raise FileNotFoundError(f"event not found: {id!r}")
        events[0].delete()


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _get_dav(acl: Acl, project: str, _dav) -> _RealDavAdapter:
    if _dav is not None:
        return _dav
    return _RealDavAdapter(acl, project)


def _parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid ISO 8601 datetime: {s!r}") from exc


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def calendar_list(
    acl: Acl,
    user_id: str,
    project: str,
    start: str,
    end: str,
    *,
    _dav=None,
) -> list[dict]:
    """List events in [start, end].  Returns [{id, title, start, end, ...}]."""
    acl.enforce(user_id, project, "collaborator")
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    dav = _get_dav(acl, project, _dav)
    return dav.list_events(start_dt, end_dt)


def calendar_find_free(
    acl: Acl,
    user_id: str,
    project: str,
    duration_min: int,
    within_start: str,
    within_end: str,
    *,
    _dav=None,
    _exclude_event_id: str | None = None,
) -> list[dict]:
    """Find free slots of *duration_min* minutes within [within_start, within_end].

    Returns [{start, end}] for each free block (minimum duration). Blocks
    outside the user's configured working hours (card e21fde31) are
    excluded — a schedule only ever narrows this result, it can never
    surface a busy block as free.

    _exclude_event_id (card 8056150d): when re-checking availability for a
    reschedule, the event being moved is itself still on the calendar and
    would otherwise count as busy against its own new window. Pass its id
    to leave it out of the busy set.

    Known limitation (memaix-src d0a1f633): once a user has configured
    working hours, this can never return a slot longer than one day,
    because apply_working_hours() chops every free gap into day-sized
    windows before the duration filter below runs. A caller asking for a
    multi-day duration_min will silently get [] for such users. Not fixed
    here — tracked as a separate risk, not this card's scope.
    """
    acl.enforce(user_id, project, "collaborator")
    # All datetimes are normalised to tz-aware UTC via to_utc before any
    # comparison below. within_* strings may carry an offset (e.g. a "Z"
    # suffix -> aware) while adapter event start/end may be naive; mixing
    # the two raises "can't compare offset-naive and offset-aware
    # datetimes". Mirrors calendar_free_busy, which already routes every
    # datetime through to_utc.
    from ..connectors.aggregate import to_utc

    ws = to_utc(_parse_dt(within_start))
    we = to_utc(_parse_dt(within_end))
    duration = timedelta(minutes=duration_min)
    dav = _get_dav(acl, project, _dav)

    all_events = dav.find_events(ws, we)
    # Respect the adapter's transparency flag: events marked "free" by the
    # calendar owner (Google transparency=transparent, CalDAV TRANSP=TRANSPARENT)
    # carry source_busy=False and must not block booking slots. Week-number and
    # informational calendars use this convention; real busy blocks (ATEA,
    # conferences, OOTO) are opaque and continue to block correctly.
    busy = sorted(
        [e for e in all_events if e.get("source_busy", True)],
        key=lambda e: to_utc(e["start"]),
    )
    if _exclude_event_id is not None:
        busy = [e for e in busy if e.get("id") != _exclude_event_id]

    # Build free slots
    free: list[dict] = []
    cursor = ws
    for ev in busy:
        ev_start = to_utc(ev["start"])
        ev_end = to_utc(ev["end"])
        if ev_start > cursor + duration:
            free.append({"start": cursor.isoformat(), "end": ev_start.isoformat()})
        if ev_end > cursor:
            cursor = ev_end
    if we > cursor + duration:
        free.append({"start": cursor.isoformat(), "end": we.isoformat()})

    if acl.resource(project, "vault"):
        from ..connectors.working_hours import WorkingHoursStore, apply_working_hours

        hours = WorkingHoursStore(acl, project, user_id).get()
        free = apply_working_hours(free, hours.get("week", {}), hours.get("tz", ""))
        free = [slot for slot in free if _parse_dt(slot["end"]) - _parse_dt(slot["start"]) >= duration]
    return free


def calendar_free_busy(
    acl: Acl,
    user_id: str,
    project: str,
    start: str,
    end: str,
    *,
    _cache=None,
) -> dict:
    """Aggregated busy view across every calendar source configured for this
    user/project (memaix-src card 4daa20e2) — served from the periodic sync
    cache (connectors/calendar_cache.py), never a live query. Distinct from
    calendar_find_free above, which still queries a single adapter live;
    this is the multi-source view the booking-page epic builds on.

    Returns {busy: [{start, end, source}], synced_at, stale, source_count,
    errors}. `stale=True` when the cache is older than an hour — well past
    the 5-15 minute sync band, meaning the background sync loop is stuck or
    hasn't run — callers should treat the data as unreliable, not refuse it
    outright (the fail-closed call for the actual booking flow belongs to
    the public-booking-page card, 2bef1062, not this read API).

    PRIVACY: this is a reader-scoped API for the calendar's own
    owner/collaborators, and its busy blocks intentionally include
    `source` (which calendar account the block came from, e.g.
    "google:alice@example.com") — useful to an authenticated owner,
    but identifying. memaix-src card de858332 requires the external
    booking view to reveal only free/busy, never who or what. When
    2bef1062 builds that public surface, it MUST NOT pass this
    function's `busy` list straight through — strip `source` (and any
    other field beyond start/end), the same shape calendar_find_free
    and connectors.aggregate.free_slots already guarantee."""
    acl.enforce(user_id, project, "reader")
    from ..connectors.calendar_cache import read_cache

    cache = _cache if _cache is not None else read_cache(acl, project, user_id)
    if cache is None:
        return {
            "busy": [], "synced_at": None, "stale": True, "source_count": 0, "errors": [],
            "note": "Ingen synk har körts än för denna kalender",
        }

    from ..connectors.aggregate import BusyInterval, CalendarEvent, merge_busy, to_utc
    from ..connectors.calendar_event_overrides import EventOverrideStore
    from ..connectors.calendar_overrides import OverrideStore

    ws, we = to_utc(_parse_dt(start)), to_utc(_parse_dt(end))

    cached_events = cache.get("events")
    if cached_events is not None:
        # memaix-src card c7698ff3 — resolve each source event against its
        # per-event override (if any) before merging, so a forced-busy event
        # (e.g. a Tentative meeting) or forced-free event (e.g. a misflagged
        # all-day "Konferens") is reflected in the aggregate. Falls back to
        # the pre-merged "busy" list below for a cache written before this
        # card (no "events" key yet).
        event_store = EventOverrideStore(acl, project, user_id)
        busy_events = []
        for ev in cached_events:
            event = CalendarEvent(
                uid=ev["uid"], start=to_utc(ev["start"]), end=to_utc(ev["end"]), source=ev.get("source", ""),
                title=ev.get("title", ""), series_id=ev.get("series_id"),
                is_exception=bool(ev.get("is_exception", False)), source_busy=bool(ev.get("source_busy", True)),
                stable_id=bool(ev.get("stable_id", True)),
            )
            decision = event_store.resolve(event)
            if decision == "free":
                continue
            if decision == "busy" or event.source_busy:
                busy_events.append(event)
        intervals = merge_busy([BusyInterval(e.start, e.end, e.source) for e in busy_events])
    else:
        intervals = [
            BusyInterval(to_utc(b["start"]), to_utc(b["end"]), b.get("source", "")) for b in cache["busy"]
        ]

    intervals = OverrideStore(acl, project, user_id).apply(intervals)
    intervals = [b for b in intervals if b.start < we and b.end > ws]

    synced_at = datetime.fromisoformat(cache["synced_at"])
    from datetime import timezone as _tz

    age = datetime.now(_tz.utc) - synced_at
    return {
        "busy": [{"start": b.start.isoformat(), "end": b.end.isoformat(), "source": b.source} for b in intervals],
        "synced_at": cache["synced_at"],
        "stale": age.total_seconds() > 3600,
        "source_count": cache.get("source_count", 0),
        "errors": cache.get("errors", []),
    }


def _source_kind(label: str) -> str:
    if label.startswith("public_ics:"):
        return "public"
    if ":" in label and "@" in label.split(":", 1)[1]:
        return "account"
    return "shared"


def calendar_sources_list(acl: Acl, user_id: str, project: str, token_store, *, _registry=None) -> dict:
    """Every calendar source configured/linked for this user/project, plus
    which are currently included in the aggregate — memaix-src card
    324dd801. The (external) booking-page UI renders this as checkboxes."""
    acl.enforce(user_id, project, "reader")
    from ..connectors.calendar_sources import SourceSelectionStore
    from ..connectors.registry import default_registry

    registry = _registry or default_registry()
    store = SourceSelectionStore(acl, project, user_id)
    data = store.list()
    disabled = set(data["disabled"])

    pairs = registry.get_all(acl, token_store, project, "calendar", user_id)
    sources = [
        {"label": label, "kind": _source_kind(label), "enabled": label not in disabled}
        for label, _adapter in pairs
    ]
    public_links = [
        {
            "id": link["id"],
            "label": link["label"],
            "url": link["url"],
            "enabled": f"public_ics:{link['id']}" not in disabled,
        }
        for link in data["public_links"]
    ]

    effective_count = sum(1 for s in sources if s["enabled"]) + sum(1 for p in public_links if p["enabled"])
    result: dict = {"sources": sources, "public_links": public_links}
    if effective_count == 0:
        result["warning"] = "Inga kalendrar räknas mot din bokningsbarhet — allt visas som ledigt"
    return result


def calendar_source_set_enabled(
    acl: Acl, user_id: str, project: str, label: str, enabled: bool
) -> dict:
    """Include/exclude one calendar source (by its calendar_sources_list
    label) from the aggregate — card 324dd801."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.calendar_sources import SourceSelectionStore

    SourceSelectionStore(acl, project, user_id).set_enabled(label, enabled)
    return {"ok": True, "label": label, "enabled": enabled}


def calendar_public_link_add(acl: Acl, user_id: str, project: str, url: str, label: str = "") -> dict:
    """Add a public .ics/webcal URL as an extra calendar source — card
    324dd801, väg 2 (no OAuth). The URL is only shape/SSRF-validated here;
    unreachable feeds surface later via the sync loop's per-source errors,
    same as any other source."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.calendar_sources import SourceSelectionStore
    from ..safety.net import BlockedURLError

    try:
        entry = SourceSelectionStore(acl, project, user_id).add_public_link(url, label)
    except BlockedURLError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **entry}


def calendar_public_link_remove(acl: Acl, user_id: str, project: str, id: str) -> dict:
    """Remove a previously-added public calendar link — card 324dd801."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.calendar_sources import SourceSelectionStore

    removed = SourceSelectionStore(acl, project, user_id).remove_public_link(id)
    return {"ok": True, "removed": removed}


def calendar_events_list(acl: Acl, user_id: str, project: str, start: str, end: str, *, _cache=None) -> dict:
    """Every cached source event in [start, end] with its resolved override
    state — memaix-src card c7698ff3. This is what a click-to-override UI
    renders: `in_series` tells the client whether to ask "just this
    instance, or the whole series?" before calling
    calendar_event_override_set; `overridable=False` marks events whose uid
    was synthesized at sync time (the source gave no real id) — setting an
    override against one could silently drift onto a different event on the
    next sync if the source reorders its results."""
    acl.enforce(user_id, project, "reader")
    from ..connectors.aggregate import CalendarEvent, to_utc
    from ..connectors.calendar_cache import read_cache
    from ..connectors.calendar_event_overrides import EventOverrideStore

    cache = _cache if _cache is not None else read_cache(acl, project, user_id)
    if cache is None:
        return {"events": [], "synced_at": None, "stale": True}

    ws, we = to_utc(_parse_dt(start)), to_utc(_parse_dt(end))
    store = EventOverrideStore(acl, project, user_id)

    out = []
    for ev in cache.get("events", []):
        event = CalendarEvent(
            uid=ev["uid"], start=to_utc(ev["start"]), end=to_utc(ev["end"]), source=ev.get("source", ""),
            title=ev.get("title", ""), series_id=ev.get("series_id"),
            is_exception=bool(ev.get("is_exception", False)), source_busy=bool(ev.get("source_busy", True)),
            stable_id=bool(ev.get("stable_id", True)),
        )
        if not (event.start < we and event.end > ws):
            continue
        override = store.resolve(event)
        effective_busy = (override == "busy") or (override is None and event.source_busy)
        out.append({
            "uid": event.uid, "source": event.source, "title": event.title,
            "start": event.start.isoformat(), "end": event.end.isoformat(),
            "source_busy": event.source_busy, "override": override, "effective_busy": effective_busy,
            "series_id": event.series_id, "is_exception": event.is_exception,
            "in_series": event.series_id is not None, "overridable": event.stable_id,
        })

    synced_at = cache.get("synced_at")
    stale = True
    if synced_at:
        from datetime import timezone as _tz
        stale = (datetime.now(_tz.utc) - datetime.fromisoformat(synced_at)).total_seconds() > 3600
    return {"events": out, "synced_at": synced_at, "stale": stale}


def calendar_event_override_set(
    acl: Acl, user_id: str, project: str, source: str, state: str, scope: str = "instance",
    uid: str | None = None, series_id: str | None = None, note: str = "",
) -> dict:
    """Force one event (scope="instance", needs uid) or a whole recurring
    series (scope="series", needs series_id) to Upptagen/Tillgänglig
    regardless of what the source calendar said — card c7698ff3. An
    exception instance never inherits a series override (see
    EventOverrideStore.resolve); it must be set individually via
    scope="instance"."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.calendar_event_overrides import VALID_STATES, EventOverrideStore

    if state not in VALID_STATES:
        return {"ok": False, "error": f"ogiltigt state {state!r}, välj busy eller free"}
    store = EventOverrideStore(acl, project, user_id)
    if scope == "instance":
        if not uid:
            return {"ok": False, "error": "uid krävs för scope=instance"}
        store.set_instance(source, uid, state, note)
        return {"ok": True, "scope": scope, "source": source, "uid": uid, "state": state}
    if scope == "series":
        if not series_id:
            return {"ok": False, "error": "series_id krävs för scope=series"}
        store.set_series(source, series_id, state, note)
        return {"ok": True, "scope": scope, "source": source, "series_id": series_id, "state": state}
    return {"ok": False, "error": f"okänt scope {scope!r}, välj instance eller series"}


def calendar_event_override_clear(
    acl: Acl, user_id: str, project: str, source: str, scope: str = "instance",
    uid: str | None = None, series_id: str | None = None,
) -> dict:
    """Remove a previously-set event/series override — card c7698ff3."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.calendar_event_overrides import EventOverrideStore

    store = EventOverrideStore(acl, project, user_id)
    if scope == "instance":
        if not uid:
            return {"ok": False, "error": "uid krävs för scope=instance"}
        return {"ok": True, "removed": store.clear_instance(source, uid)}
    if scope == "series":
        if not series_id:
            return {"ok": False, "error": "series_id krävs för scope=series"}
        return {"ok": True, "removed": store.clear_series(source, series_id)}
    return {"ok": False, "error": f"okänt scope {scope!r}, välj instance eller series"}


def calendar_working_hours_get(acl: Acl, user_id: str, project: str) -> dict:
    """The user's configured bookable weekly schedule — card e21fde31.
    {} (no tz/week) if never configured, which callers should treat as
    wide-open (every time bookable)."""
    acl.enforce(user_id, project, "reader")
    from ..connectors.working_hours import WorkingHoursStore

    return WorkingHoursStore(acl, project, user_id).get()


def calendar_working_hours_set(acl: Acl, user_id: str, project: str, tz: str, week: dict) -> dict:
    """Set the bookable weekly schedule — card e21fde31. *week* maps
    mon/tue/wed/thu/fri/sat/sun to a list of {start, end} local HH:MM
    windows (24h, half-open); an empty or omitted day has nothing
    bookable. *tz* must be a resolvable IANA zone (e.g. "Europe/Stockholm")
    — that's the frame the weekly schedule is expressed in. This only
    restricts calendar_find_free's bookable output; it never changes what
    the calendar itself reports as busy/free."""
    acl.enforce(user_id, project, "collaborator")
    from zoneinfo import ZoneInfoNotFoundError

    from ..connectors.working_hours import WorkingHoursStore

    try:
        WorkingHoursStore(acl, project, user_id).set(tz, week)
    except ZoneInfoNotFoundError:
        return {"ok": False, "error": f"okänd tidszon {tz!r}"}
    except (ValueError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "tz": tz, "week": week}


def calendar_booking_enabled_get(acl: Acl, user_id: str, project: str) -> dict:
    """Whether the meeting booker is switched on for this user — card
    9e035c73. {"enabled": False} if never configured; off by default."""
    acl.enforce(user_id, project, "reader")
    from ..connectors.booking_settings import BookingSettingsStore

    return BookingSettingsStore(acl, project, user_id).get()


def calendar_booking_enabled_set(acl: Acl, user_id: str, project: str, enabled: bool) -> dict:
    """Turn the meeting booker on or off for this user — card 9e035c73."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.booking_settings import BookingSettingsStore

    BookingSettingsStore(acl, project, user_id).set(enabled)
    return {"ok": True, "enabled": bool(enabled)}


def calendar_meeting_type_list(acl: Acl, user_id: str, project: str) -> list[dict]:
    """The user's named meeting-type presets (duration/interval shortcuts)
    — card d0a1f633. [] if never configured. Purely advisory: this does
    not change what calendar_find_free returns, a caller still passes
    duration_min itself."""
    acl.enforce(user_id, project, "reader")
    from ..connectors.meeting_types import MeetingTypesStore

    return MeetingTypesStore(acl, project, user_id).get()


def calendar_meeting_type_set(acl: Acl, user_id: str, project: str, types: list[dict]) -> dict:
    """Replace the full list of meeting-type presets — card d0a1f633.
    Each type needs slug (lowercase alphanumeric/hyphen), name, and
    duration_min (1..43200 minutes); interval_min defaults to
    duration_min. At most one type may be marked default; if none is,
    the first is auto-promoted."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.meeting_types import MeetingTypesStore

    try:
        normalized = MeetingTypesStore(acl, project, user_id).set(types)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "types": normalized}


def calendar_meeting_type_delete(acl: Acl, user_id: str, project: str, slug: str) -> dict:
    """Remove one meeting-type preset by slug — card d0a1f633. A no-op if
    the slug isn't present."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.meeting_types import MeetingTypesStore

    remaining = MeetingTypesStore(acl, project, user_id).delete(slug)
    return {"ok": True, "types": remaining}


def calendar_meeting_form_list(acl: Acl, user_id: str, project: str) -> list[dict]:
    """The host's enabled meeting forms (video/phone options offered at
    booking time) — card 85854d2c. [] if never configured, which
    booking_create treats as "feature off, old behaviour"."""
    acl.enforce(user_id, project, "reader")
    from ..connectors.meeting_forms import MeetingFormsStore

    return MeetingFormsStore(acl, project, user_id).get()


def calendar_meeting_form_set(acl: Acl, user_id: str, project: str, forms: list[dict]) -> dict:
    """Replace the full list of enabled meeting forms — card 85854d2c.
    Each form needs slug, provider (google_meet/zoom/phone) and label; a
    phone form also needs config.phone_number. At most one form may be
    marked default; if none is, the first is auto-promoted."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.meeting_forms import MeetingFormsStore

    try:
        normalized = MeetingFormsStore(acl, project, user_id).set(forms)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "forms": normalized}


def calendar_meeting_form_delete(acl: Acl, user_id: str, project: str, slug: str) -> dict:
    """Remove one meeting form by slug — card 85854d2c. A no-op if the
    slug isn't present."""
    acl.enforce(user_id, project, "collaborator")
    from ..connectors.meeting_forms import MeetingFormsStore

    remaining = MeetingFormsStore(acl, project, user_id).delete(slug)
    return {"ok": True, "forms": remaining}


def _maybe_queue(acl, user_id: str, project: str, tool: str, args: dict, *, _outbox, _cfg) -> dict | None:
    """Return a {"pending": ...} dict if this action should be queued, else None."""
    from ..outbox.policy import action_mode
    from ..outbox.preview import render_preview
    from ..outbox.queue import default_queue

    memaix_cfg = _cfg if _cfg is not None else config.load()
    if action_mode(memaix_cfg, acl, project, tool, args) != "review":
        return None
    queue = _outbox if _outbox is not None else default_queue()
    action_id = queue.enqueue(user_id, project, tool, args, render_preview(tool, args))
    return {"pending": True, "action_id": action_id, "note": "Väntar på godkännande i utkorgen"}


def calendar_create(
    acl: Acl,
    user_id: str,
    project: str,
    title: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    location: str | None = None,
    description: str | None = None,
    *,
    want_conference: bool = False,
    _dav=None,
    _confirmed: bool = False,
    _outbox=None,
    _cfg: dict | None = None,
) -> dict:
    """Create an event.  Returns {id, title, start, end}.

    want_conference=True (memaix-src card 85854d2c) asks the adapter to
    auto-generate a video-conference link (Google Meet only today — other
    adapters silently ignore it, no conferenceData concept exists for
    CalDAV/iCal/FreeBusy). The returned dict then carries a "meet_url" key.

    Queued for approval instead of created when the outbox policy resolves
    to 'review' (see module docstring) — unless _confirmed=True.
    """
    acl.enforce(user_id, project, "collaborator")
    if not _confirmed:
        args = {
            "title": title, "start": start, "end": end, "attendees": attendees,
            "location": location, "description": description,
        }
        queued = _maybe_queue(acl, user_id, project, "calendar_create", args, _outbox=_outbox, _cfg=_cfg)
        if queued is not None:
            return queued

    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    import uuid as _uuid

    uid = _uuid.uuid4().hex
    dav = _get_dav(acl, project, _dav)
    # Only pass want_conference through when set — keeps every existing
    # caller/test fake (whose create_event doesn't know this kwarg) working
    # unchanged, since the default path never sends it at all.
    extra = {"want_conference": True} if want_conference else {}
    return dav.create_event(uid, title, start_dt, end_dt, attendees, location, description, **extra)


def calendar_update(
    acl: Acl,
    user_id: str,
    project: str,
    id: str,
    *,
    _dav=None,
    _confirmed: bool = False,
    _outbox=None,
    _cfg: dict | None = None,
    **fields,
) -> dict:
    """Update event fields.  Returns updated event dict.

    Queued for approval instead of applied when the outbox policy resolves
    to 'review' (see module docstring) — unless _confirmed=True.
    """
    acl.enforce(user_id, project, "collaborator")
    if not _confirmed:
        args = {"id": id, **fields}
        queued = _maybe_queue(acl, user_id, project, "calendar_update", args, _outbox=_outbox, _cfg=_cfg)
        if queued is not None:
            return queued

    dav = _get_dav(acl, project, _dav)
    return dav.update_event(id, **fields)


def calendar_delete(
    acl: Acl,
    user_id: str,
    project: str,
    id: str,
    *,
    _dav=None,
) -> dict:
    """Delete an event.  Always returns requires_confirmation=True (SAFETY.md §8)."""
    acl.enforce(user_id, project, "collaborator")
    dav = _get_dav(acl, project, _dav)
    dav.delete_event(id)
    return {"deleted": True, "requires_confirmation": True}


# ------------------------------------------------------------------
# Setup / status — extracted from MCP wrappers so web-routes can reuse
# ------------------------------------------------------------------


def setup_mode(
    acl: Acl,
    user_id: str,
    project: str,
    mode: str,
    store,
    public_url: str,
    ical_url: str | None = None,
    calendar_id: str | None = None,
) -> dict:
    """Configure per-user calendar access mode for a project.

    Called by both the MCP tool (calendar_setup) and web-routes
    (POST /app/api/calendar-mode).  Accepts explicit dependencies
    instead of relying on MCP context helpers.
    """
    acl.enforce(user_id, project, "collaborator")
    from .account import account_link

    if mode == "oauth":
        result = account_link(acl, user_id, "google", public_url)
        return {"ok": True, "mode": "oauth", "link_url": result["link_url"],
                "next": f"Öppna {result['link_url']} i din webbläsare"}

    if mode == "ical_secret":
        if not ical_url:
            return {"ok": False, "error": "ical_url krävs för mode=ical_secret"}
        # SSRF guard (safety/net.py): reject a URL pointing at an internal target
        # before we ever store or fetch it. resolve=False here — config must not
        # depend on the name resolving at set-time; the authoritative resolve
        # happens in _ICalAdapter._fetch just before the real request.
        from ..safety.net import BlockedURLError, validate_external_url
        try:
            validate_external_url(ical_url, resolve=False)
        except BlockedURLError as exc:
            return {"ok": False, "error": f"ical_url avvisad: {exc}"}
        store.store(user_id, "ical_secret", "ical_secret", {"ical_url": ical_url})
        return {"ok": True, "mode": "ical_secret", "stored": True}

    if mode == "free_busy":
        if not calendar_id:
            return {"ok": False, "error": "calendar_id krävs för mode=free_busy"}
        store.store(user_id, "free_busy", "free_busy", {"calendar_id": calendar_id})
        return {"ok": True, "mode": "free_busy", "calendar_id": calendar_id,
                "note": "Kräver att google_api_key finns i memaix.yaml och att din kalender är publik"}

    if mode == "none":
        for provider, account in [("ical_secret", "ical_secret"), ("free_busy", "free_busy")]:
            store.delete(user_id, provider, account)
        return {"ok": True, "mode": "none",
                "note": "Kalender-koppling borttagen (OAuth-token behåller du via account_unlink)"}

    return {"ok": False, "error": f"Okänt mode: {mode!r}. Välj oauth, ical_secret, free_busy eller none"}


def get_status(user_id: str, project: str, acl: Acl, store) -> dict:
    """Return active calendar mode for user in project.

    Called by both the MCP tool (calendar_status) and web-routes
    (GET /app/api/calendar-mode).
    """
    acl.enforce(user_id, project, "reader")
    all_accounts = store.list_accounts(user_id)

    google = [a for a in all_accounts if a["provider"] == "google"]
    ical = [a for a in all_accounts if a["provider"] == "ical_secret"]
    fb = [a for a in all_accounts if a["provider"] == "free_busy"]

    active = "none"
    details: dict = {}
    if google:
        active = "oauth"
        details = {"account": google[0]["account"], "status": google[0]["status"]}
    elif ical:
        active = "ical_secret"
        details = {"status": ical[0]["status"]}
    elif fb:
        active = "free_busy"
        token_data = store.load_one(user_id, "free_busy", "free_busy") or {}
        details = {"calendar_id": token_data.get("calendar_id", ""), "status": fb[0]["status"]}

    return {
        "active_mode": active,
        "details": details,
        "available_modes": ["oauth", "ical_secret", "free_busy"],
    }

