# SPDX-License-Identifier: AGPL-3.0-or-later
"""Karakteriseringstester för booking.routes.booking_create.

Publik bokning med gästers persondata. Alla beroenden på modulnivå i
booking.routes byts mot inspelande fakes, så att varje gren, statuskod,
felmeddelande och varje anrop (argument och ordning) är låsta oberoende av
hur funktionen delas upp internt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.booking import routes as routes_mod
from memaix_gateway.booking.meeting_providers import MeetingProviderError
from memaix_gateway.tools import calendar as t_cal
from memaix_gateway.tools.calendar import CalendarAuthRequired

START = datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)
NOW = 1_700_000_000.9

LINK = {"project": "proj", "user": "alice", "duration_min": 30, "title_template": "Möte med {name}"}


class Rig:
    """Alla inspelade anrop + styrbara svar."""

    def __init__(self):
        self.calls: list = []
        self.link = dict(LINK)
        self.rate_ok = True
        self.captcha = True
        self.enabled = {"enabled": True}
        self.forms: list = []
        self.free = [{"start": START.isoformat(), "end": END.isoformat()}]
        self.dav_filtered_exc: Exception | None = None
        self.dav_write_exc: Exception | None = None
        self.detail: dict | Exception | None = None
        self.google_detail: dict | Exception | None = None
        self.event = {"id": "ev1", "start": START.isoformat(), "end": END.isoformat()}
        self.create_exc: Exception | None = None
        self.delete_exc: Exception | None = None
        self.record_result = ("row1", "tok123")
        self.lock_state: dict = {}

    def names(self):
        return [c[0] for c in self.calls]

    def one(self, name):
        found = [c for c in self.calls if c[0] == name]
        assert len(found) == 1, (name, self.names())
        return found[0]


ACL = object()
DAV = object()
WRITE_DAV = object()


@pytest.fixture()
def rig(monkeypatch):
    r = Rig()

    class Limiter:
        def check(self, key, limit, window_s):
            r.calls.append(("rate", key, limit, window_s))
            return r.rate_ok

    def get_link(slug):
        r.calls.append(("get_link", slug))
        return r.link

    async def verify(token, ip):
        r.calls.append(("turnstile", token, ip))
        return r.captcha

    def enabled_get(acl, user, project):
        r.calls.append(("enabled_get", acl, user, project))
        return r.enabled

    def form_list(acl, user, project):
        r.calls.append(("form_list", acl, user, project))
        return r.forms

    def dav_filtered(project, user, acl):
        r.calls.append(("dav_filtered", project, user, acl))
        if r.dav_filtered_exc:
            raise r.dav_filtered_exc
        return DAV

    def dav_write(project, user, write=False):
        r.calls.append(("dav_write", project, user, write))
        if r.dav_write_exc:
            raise r.dav_write_exc
        return WRITE_DAV

    def find_free(acl, user, project, minutes, start, end, _dav=None):
        lock = routes_mod._BOOKING_LOCKS[(project, user)]
        r.lock_state["find_free_locked"] = lock.locked()
        r.calls.append(("find_free", acl, user, project, minutes, start, end, _dav))
        return r.free

    def create(acl, user, project, title, start, end, **kw):
        r.lock_state["create_locked"] = routes_mod._BOOKING_LOCKS[(project, user)].locked()
        r.calls.append(("create", acl, user, project, title, start, end, kw))
        if r.create_exc:
            raise r.create_exc
        return r.event

    def delete(acl, user, project, event_id, _dav=None):
        r.calls.append(("delete", acl, user, project, event_id, _dav))
        if r.delete_exc:
            raise r.delete_exc

    def resolve_detail(provider, config, acl, project, user, **kw):
        r.calls.append(("resolve_detail", provider, config, acl, project, user, kw))
        res = r.google_detail if provider == "google_meet" else r.detail
        if isinstance(res, Exception):
            raise res
        return res

    class Store:
        def record(self, **kw):
            r.calls.append(("record", kw))
            return r.record_result

    def send_emails(*args):
        r.lock_state["send_locked"] = routes_mod._BOOKING_LOCKS[("proj", "alice")].locked()
        r.calls.append(("send", args))

    def manage_url(token, link=None):
        r.calls.append(("manage_url", token, link))
        return f"https://mx.example/booking/{token}"

    monkeypatch.setattr(routes_mod, "_rate_limiter", lambda: Limiter())
    monkeypatch.setattr(routes_mod, "get_link", get_link)
    monkeypatch.setattr(routes_mod, "_verify_turnstile", verify)
    monkeypatch.setattr(routes_mod, "_get_acl", lambda: ACL)
    monkeypatch.setattr(routes_mod, "_resolve_dav_filtered", dav_filtered)
    monkeypatch.setattr(routes_mod, "_resolve_dav", dav_write)
    monkeypatch.setattr(routes_mod, "resolve_meeting_detail", resolve_detail)
    monkeypatch.setattr(routes_mod, "get_consent_store", lambda: Store())
    monkeypatch.setattr(routes_mod, "_send_confirmation_emails", send_emails)
    monkeypatch.setattr(routes_mod, "_manage_url", manage_url)
    monkeypatch.setattr(routes_mod, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(t_cal, "calendar_booking_enabled_get", enabled_get)
    monkeypatch.setattr(t_cal, "calendar_meeting_form_list", form_list)
    monkeypatch.setattr(t_cal, "calendar_meeting_type_list", lambda *a, **k: [])
    monkeypatch.setattr(t_cal, "calendar_find_free", find_free)
    monkeypatch.setattr(t_cal, "calendar_create", create)
    monkeypatch.setattr(t_cal, "calendar_delete", delete)
    routes_mod._BOOKING_LOCKS.clear()

    r.client = TestClient(Starlette(routes=routes_mod.booking_routes), raise_server_exceptions=False)
    return r


def body(**over):
    b = {
        "start": START.isoformat(),
        "end": END.isoformat(),
        "name": "Eva",
        "email": "eva@example.com",
        "turnstile_token": "tt",
        "consent": True,
        "consent_text": "Jag godkänner",
    }
    b.update(over)
    return {k: v for k, v in b.items() if v is not None or k in over and over[k] is None}


def post(rig, **over):
    return rig.client.post("/book/slug1", json=body(**over))


FORM_PHONE = {"slug": "tel", "provider": "phone", "label": "Ring", "config": {"number": "+46"}, "default": True}
FORM_ZOOM = {"slug": "zoom", "provider": "zoom", "config": None}
FORM_MEET = {"slug": "meet", "provider": "google_meet", "config": {}}
DETAIL = {"join_url": "https://zoom/j/1", "phone_number": "", "display_text": "Zoom: https://zoom/j/1"}


# ------------------------------------------------------------------
# Lyckad bokning: alla anrop, argument och ordning
# ------------------------------------------------------------------


def test_happy_path_response_and_every_dependency_call(rig):
    resp = post(rig, purpose="  Prata  ", timezone=" Europe/Stockholm ", name="  Eva  ", email=" eva@example.com ")

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "start": START.isoformat(),
        "end": END.isoformat(),
        "manage_url": "https://mx.example/booking/tok123",
    }
    assert rig.names() == [
        "rate", "get_link", "turnstile", "enabled_get", "form_list", "dav_filtered", "dav_write",
        "find_free", "create", "record", "send", "manage_url",
    ]
    assert rig.one("rate") == ("rate", "booking:create:testclient", 10, 60)
    assert rig.one("get_link") == ("get_link", "slug1")
    assert rig.one("turnstile") == ("turnstile", "tt", "testclient")
    assert rig.one("enabled_get") == ("enabled_get", ACL, "alice", "proj")
    assert rig.one("form_list") == ("form_list", ACL, "alice", "proj")
    assert rig.one("dav_filtered") == ("dav_filtered", "proj", "alice", ACL)
    assert rig.one("dav_write") == ("dav_write", "proj", "alice", True)
    assert rig.one("find_free") == (
        "find_free", ACL, "alice", "proj", 30,
        START.isoformat(), (END + timedelta(minutes=1)).isoformat(), DAV,
    )
    assert rig.one("create") == (
        "create", ACL, "alice", "proj", "Möte med Eva", START.isoformat(), END.isoformat(),
        {
            "attendees": ["eva@example.com"],
            "location": None,
            "description": "Prata",
            "want_conference": False,
            "_dav": WRITE_DAV,
            "_confirmed": True,
        },
    )
    assert rig.one("record") == ("record", {
        "project": "proj", "host_user": "alice", "event_id": "ev1",
        "visitor_email": "eva@example.com", "consent_text": "Jag godkänner",
        "consent_at": 1_700_000_000, "meeting_end": int(END.timestamp()),
        "slug": "slug1", "meeting_start": int(START.timestamp()),
        "meeting_form_slug": None, "meeting_form_provider": None, "meeting_form_detail": None,
    })
    assert rig.one("send") == ("send", (
        ACL, "proj", rig.link, "Möte med Eva", rig.event, "Eva", "eva@example.com", "Prata",
        START, END, "Europe/Stockholm", "tok123", None,
    ))
    assert rig.one("manage_url") == ("manage_url", "tok123", None)


def test_critical_section_holds_lock_but_email_does_not(rig):
    post(rig)
    assert rig.lock_state == {"find_free_locked": True, "create_locked": True, "send_locked": False}


def test_naive_datetimes_are_read_as_utc(rig):
    post(rig, start="2030-01-03T10:00:00", end="2030-01-03T10:30:00")
    assert rig.one("find_free")[5] == START.isoformat()


def test_response_start_end_none_when_event_lacks_them(rig):
    rig.event = {"id": "ev1"}
    assert post(rig).json() == {"ok": True, "start": None, "end": None, "manage_url": "https://mx.example/booking/tok123"}


def test_event_without_id_records_none(rig):
    rig.event = {}
    post(rig)
    assert rig.one("record")[1]["event_id"] is None


def test_optional_fields_default_when_absent_or_empty(rig):
    post(rig, consent_text=None, purpose="   ", timezone="  ", meeting_form_slug="")
    create_kw = rig.one("create")[7]
    assert create_kw["description"] is None
    assert rig.one("record")[1]["consent_text"] == ""
    send_args = rig.one("send")[1]
    assert send_args[7] == "" and send_args[10] is None


def test_non_string_fields_are_stringified(rig):
    post(rig, purpose=12345, consent_text=7)
    assert rig.one("create")[7]["description"] == "12345"
    assert rig.one("record")[1]["consent_text"] == "7"


def test_purpose_is_truncated_to_limit(rig):
    post(rig, purpose="x" * 900)
    assert rig.one("create")[7]["description"] == "x" * routes_mod._MAX_PURPOSE_LEN


def test_duration_minutes_passed_to_find_free(rig):
    end = START + timedelta(minutes=45)
    rig.free = [{"start": START.isoformat(), "end": end.isoformat()}]
    post(rig, end=end.isoformat())
    assert rig.one("find_free")[4] == 45


# ------------------------------------------------------------------
# Tidiga avslag
# ------------------------------------------------------------------


def test_rate_limited_returns_429_before_anything_else(rig):
    rig.rate_ok = False
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (429, {"error": "rate_limited"})
    assert rig.names() == ["rate"]


def test_unknown_link_is_404_not_found(rig):
    rig.link = None
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert "turnstile" not in rig.names()


def test_unparseable_json_is_treated_as_empty_body(rig):
    resp = rig.client.post("/book/slug1", content=b"{nope", headers={"content-type": "application/json"})
    assert (resp.status_code, resp.json()) == (400, {"error": "consent_required"})
    assert rig.one("turnstile") == ("turnstile", "", "testclient")


@pytest.mark.parametrize("payload", [[1, 2], "text", 5])
def test_non_object_json_is_invalid_body_without_captcha(rig, payload):
    resp = rig.client.post("/book/slug1", json=payload)
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_body"})
    assert "turnstile" not in rig.names()


def test_failed_captcha_is_403_before_consent(rig):
    rig.captcha = False
    resp = post(rig, consent=False)
    assert (resp.status_code, resp.json()) == (403, {"error": "captcha_failed"})
    assert "enabled_get" not in rig.names()


def test_missing_turnstile_token_is_passed_as_empty_string(rig):
    post(rig, turnstile_token=None)
    assert rig.one("turnstile")[1] == ""


@pytest.mark.parametrize("consent", [False, "true", 1, None])
def test_consent_must_be_literal_true(rig, consent):
    resp = post(rig, consent=consent)
    assert (resp.status_code, resp.json()) == (400, {"error": "consent_required"})
    assert "enabled_get" not in rig.names()


def test_missing_consent_key_is_rejected(rig):
    payload = body()
    del payload["consent"]
    resp = rig.client.post("/book/slug1", json=payload)
    assert (resp.status_code, resp.json()) == (400, {"error": "consent_required"})


@pytest.mark.parametrize(
    "over",
    [
        {"name": ""},
        {"name": "   "},
        {"name": None},
        {"email": ""},
        {"email": "  "},
        {"email": None},
        {"start": "not-a-date"},
        {"start": None},
        {"end": "garbage"},
        {"end": None},
        {"end": START.isoformat()},
        {"end": (START - timedelta(minutes=30)).isoformat()},
    ],
)
def test_invalid_body_variants(rig, over):
    resp = post(rig, **over)
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_body"})
    assert "enabled_get" not in rig.names()


@pytest.mark.parametrize("minutes", [14, 241, 600])
def test_duration_outside_bounds_is_rejected(rig, minutes):
    resp = post(rig, end=(START + timedelta(minutes=minutes)).isoformat())
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_duration"})
    assert "enabled_get" not in rig.names()


@pytest.mark.parametrize("minutes", [15, 240])
def test_duration_bounds_are_inclusive(rig, minutes):
    end = START + timedelta(minutes=minutes)
    rig.free = [{"start": START.isoformat(), "end": end.isoformat()}]
    resp = post(rig, end=end.isoformat())
    assert resp.status_code == 200


@pytest.mark.parametrize("enabled", [{"enabled": False}, {}, {"enabled": None}])
def test_booking_disabled_is_404_not_found(rig, enabled):
    rig.enabled = enabled
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert "form_list" not in rig.names()


# ------------------------------------------------------------------
# Mötesformulär
# ------------------------------------------------------------------


def test_no_forms_configured_books_without_form(rig):
    rig.forms = []
    resp = post(rig, meeting_form_slug="whatever")
    assert resp.status_code == 200
    assert "resolve_detail" not in rig.names()
    rec = rig.one("record")[1]
    assert (rec["meeting_form_slug"], rec["meeting_form_provider"], rec["meeting_form_detail"]) == (None, None, None)


def test_explicit_slug_selects_that_form(rig):
    rig.forms = [FORM_PHONE, FORM_ZOOM]
    rig.detail = DETAIL
    post(rig, meeting_form_slug=" zoom ")
    rec = rig.one("record")[1]
    assert (rec["meeting_form_slug"], rec["meeting_form_provider"]) == ("zoom", "zoom")


def test_unknown_slug_is_invalid_meeting_form_even_if_default_exists(rig):
    rig.forms = [FORM_PHONE]
    resp = post(rig, meeting_form_slug="nope")
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_meeting_form"})
    assert "dav_filtered" not in rig.names()


def test_default_form_used_when_no_slug(rig):
    rig.forms = [FORM_ZOOM, FORM_PHONE]
    rig.detail = {"join_url": "", "phone_number": "+4670", "display_text": "Ring: +4670"}
    post(rig)
    assert rig.one("record")[1]["meeting_form_slug"] == "tel"


def test_no_slug_and_no_default_is_invalid_meeting_form(rig):
    rig.forms = [FORM_ZOOM]
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_meeting_form"})
    assert "dav_filtered" not in rig.names()


def test_prebooking_provider_detail_goes_into_location_and_record(rig):
    rig.forms = [FORM_ZOOM]
    rig.detail = DETAIL
    resp = post(rig, meeting_form_slug="zoom")
    assert resp.status_code == 200
    call = rig.one("resolve_detail")
    assert call == ("resolve_detail", "zoom", {}, ACL, "proj", "alice", {
        "start": START, "end": END, "title": "Möte med Eva",
    })
    assert rig.names().index("resolve_detail") < rig.names().index("create")
    create_kw = rig.one("create")[7]
    assert create_kw["location"] == "https://zoom/j/1"
    assert create_kw["want_conference"] is False
    assert rig.one("record")[1]["meeting_form_detail"] == "https://zoom/j/1"
    assert rig.one("send")[1][-1] == "Zoom: https://zoom/j/1"


def test_prebooking_config_is_passed_through(rig):
    rig.forms = [FORM_PHONE]
    rig.detail = {"join_url": "", "phone_number": "+46", "display_text": "Ring: +46"}
    post(rig)
    assert rig.one("resolve_detail")[2] == {"number": "+46"}
    assert rig.one("create")[7]["location"] == "+46"
    assert rig.one("record")[1]["meeting_form_detail"] == "+46"


def test_prebooking_provider_error_is_502_and_nothing_is_created(rig, caplog):
    rig.forms = [FORM_ZOOM]
    rig.detail = MeetingProviderError("down")
    with caplog.at_level(logging.ERROR, logger=routes_mod.logger.name):
        resp = post(rig, meeting_form_slug="zoom")
    assert (resp.status_code, resp.json()) == (502, {"error": "meeting_form_unavailable"})
    assert "create" not in rig.names()
    assert "record" not in rig.names()
    assert "send" not in rig.names()
    assert "meeting form resolution failed for project=proj host=alice slug=zoom" in caplog.text


def test_google_meet_resolves_after_create_with_calendar_event(rig):
    rig.forms = [FORM_MEET]
    rig.google_detail = {"join_url": "https://meet/x", "phone_number": "", "display_text": "Google Meet: https://meet/x"}
    resp = post(rig, meeting_form_slug="meet")
    assert resp.status_code == 200
    create_kw = rig.one("create")[7]
    assert create_kw["want_conference"] is True
    assert create_kw["location"] is None
    assert rig.names().index("create") < rig.names().index("resolve_detail")
    assert rig.one("resolve_detail") == ("resolve_detail", "google_meet", {}, ACL, "proj", "alice", {
        "start": START, "end": END, "title": "Möte med Eva", "calendar_event": rig.event,
    })
    assert rig.one("record")[1]["meeting_form_detail"] == "https://meet/x"
    assert rig.one("record")[1]["meeting_form_provider"] == "google_meet"
    assert rig.one("send")[1][-1] == "Google Meet: https://meet/x"


def test_google_meet_error_rolls_back_event_and_returns_502(rig, caplog):
    rig.forms = [FORM_MEET]
    rig.google_detail = MeetingProviderError("no meet")
    with caplog.at_level(logging.ERROR, logger=routes_mod.logger.name):
        resp = post(rig, meeting_form_slug="meet")
    assert (resp.status_code, resp.json()) == (502, {"error": "meeting_form_unavailable"})
    assert rig.one("delete") == ("delete", ACL, "alice", "proj", "ev1", WRITE_DAV)
    assert "record" not in rig.names() and "send" not in rig.names()
    assert "meeting form resolution failed for project=proj host=alice slug=meet" in caplog.text


def test_google_meet_rollback_failure_is_logged_and_still_502(rig, caplog):
    rig.forms = [FORM_MEET]
    rig.google_detail = MeetingProviderError("no meet")
    rig.delete_exc = RuntimeError("delete failed")
    with caplog.at_level(logging.ERROR, logger=routes_mod.logger.name):
        resp = post(rig, meeting_form_slug="meet")
    assert (resp.status_code, resp.json()) == (502, {"error": "meeting_form_unavailable"})
    assert "failed to roll back orphaned google_meet event=ev1 project=proj host=alice" in caplog.text


# ------------------------------------------------------------------
# Kalenderadaptrar och TOCTOU-omkoll
# ------------------------------------------------------------------


def test_read_adapter_auth_required_is_404(rig):
    rig.dav_filtered_exc = CalendarAuthRequired("x")
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert "dav_write" not in rig.names()
    assert "find_free" not in rig.names()


def test_write_adapter_auth_required_is_404(rig):
    rig.dav_write_exc = CalendarAuthRequired("x")
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert "find_free" not in rig.names()


def test_other_adapter_error_is_500_internal_error(rig):
    rig.dav_filtered_exc = RuntimeError("boom")
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (500, {"error": "internal_error"})


def test_slot_no_longer_free_is_409_and_nothing_created(rig):
    rig.free = []
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (409, {"error": "slot_unavailable"})
    assert "create" not in rig.names()
    assert "record" not in rig.names()


@pytest.mark.parametrize(
    "free",
    [
        [{"start": (START + timedelta(minutes=5)).isoformat(), "end": END.isoformat()}],
        [{"start": START.isoformat(), "end": (END - timedelta(minutes=5)).isoformat()}],
        [{"start": "junk", "end": END.isoformat()}],
        [{"start": START.isoformat(), "end": "junk"}],
        [{"end": END.isoformat()}],
        [{"start": START.isoformat()}],
        [{}],
    ],
)
def test_free_slots_that_do_not_cover_or_are_malformed_do_not_count(rig, free):
    rig.free = free
    assert post(rig).status_code == 409


def test_any_covering_slot_is_enough_and_malformed_rows_are_skipped(rig):
    rig.free = [
        {"start": "junk", "end": "junk"},
        {"start": (START - timedelta(hours=1)).isoformat(), "end": (END + timedelta(hours=1)).isoformat()},
    ]
    assert post(rig).status_code == 200


# ------------------------------------------------------------------
# Titel
# ------------------------------------------------------------------


def test_title_without_name_placeholder_is_used_verbatim(rig):
    rig.link = {**LINK, "title_template": "Introsamtal"}
    post(rig)
    assert rig.one("create")[4] == "Introsamtal"


def test_title_defaults_to_mote_when_template_absent(rig):
    rig.link = {k: v for k, v in LINK.items() if k != "title_template"}
    post(rig)
    assert rig.one("create")[4] == "Möte"


def test_title_formats_stripped_name(rig):
    post(rig, name="  Åsa  ")
    assert rig.one("create")[4] == "Möte med Åsa"


# ------------------------------------------------------------------
# Fel i skrivsteget
# ------------------------------------------------------------------


def test_calendar_create_failure_is_500_and_nothing_recorded(rig):
    rig.create_exc = RuntimeError("calendar down")
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (500, {"error": "internal_error"})
    assert "record" not in rig.names()
    assert "send" not in rig.names()
    assert not routes_mod._BOOKING_LOCKS[("proj", "alice")].locked()


def test_locks_are_per_project_and_host(rig):
    post(rig)
    assert set(routes_mod._BOOKING_LOCKS) == {("proj", "alice")}


def test_json_null_body_is_invalid_body(rig):
    resp = rig.client.post("/book/slug1", content=b"null", headers={"content-type": "application/json"})
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_body"})
    assert "turnstile" not in rig.names()
