# SPDX-License-Identifier: AGPL-3.0-or-later
"""Karakteriseringstester för booking.routes.booking_reschedule.

Token-som-behörighet, publik yta. Alla beroenden på modulnivå byts mot
inspelande fakes så att varje gren, statuskod och varje anrop (argument och
ordning) är låsta oberoende av hur funktionen delas upp internt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.booking import routes as routes_mod
from memaix_gateway.tools import calendar as t_cal
from memaix_gateway.tools.calendar import CalendarAuthRequired

START = datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)
ACL = object()
DAV = object()
WRITE_DAV = object()

ROW = {
    "id": "row1", "status": "confirmed", "slug": "slug1", "project": "proj", "host_user": "alice",
    "event_id": "ev1", "visitor_email": "eva@example.com",
    "meeting_form_provider": "google_meet", "meeting_form_detail": "https://meet/x",
}
LINK = {"project": "proj", "user": "alice", "title_template": "Möte med {name}"}


class Rig:
    def __init__(self):
        self.calls: list = []
        self.rate_ok = True
        self.row: dict | None = dict(ROW)
        self.link: dict | None = dict(LINK)
        self.dav_filtered_exc: Exception | None = None
        self.dav_write_exc: Exception | None = None
        self.free = [{"start": START.isoformat(), "end": END.isoformat()}]
        self.event: dict = {"id": "ev1", "title": "Nytt", "start": "S", "end": "E"}
        self.lock_state: dict = {}

    def names(self):
        return [c[0] for c in self.calls]

    def one(self, name):
        found = [c for c in self.calls if c[0] == name]
        assert len(found) == 1, (name, self.names())
        return found[0]


@pytest.fixture()
def rig(monkeypatch):
    r = Rig()

    class Limiter:
        def check(self, key, limit, window_s):
            r.calls.append(("rate", key, limit, window_s))
            return r.rate_ok

    class Store:
        def get_by_manage_token(self, token):
            r.calls.append(("get_row", token))
            return r.row

        def update_booking(self, row_id, **kw):
            r.calls.append(("update", row_id, kw))

    def get_link(slug):
        r.calls.append(("get_link", slug))
        return r.link

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

    def find_free(acl, user, project, minutes, start, end, _dav=None, _exclude_event_id=None):
        r.lock_state["find_free_locked"] = routes_mod._BOOKING_LOCKS[(project, user)].locked()
        r.calls.append(("find_free", acl, user, project, minutes, start, end, _dav, _exclude_event_id))
        return r.free

    def update(acl, user, project, event_id, **kw):
        r.lock_state["update_locked"] = routes_mod._BOOKING_LOCKS[(project, user)].locked()
        r.calls.append(("cal_update", acl, user, project, event_id, kw))
        return r.event

    def detail_line(provider, detail):
        r.calls.append(("detail_line", provider, detail))
        return "LINE"

    def send(*args):
        r.lock_state["send_locked"] = routes_mod._BOOKING_LOCKS[("proj", "alice")].locked()
        r.calls.append(("send", args))

    monkeypatch.setattr(routes_mod, "_rate_limiter", lambda: Limiter())
    monkeypatch.setattr(routes_mod, "get_consent_store", lambda: Store())
    monkeypatch.setattr(routes_mod, "get_link", get_link)
    monkeypatch.setattr(routes_mod, "_get_acl", lambda: ACL)
    monkeypatch.setattr(routes_mod, "_resolve_dav_filtered", dav_filtered)
    monkeypatch.setattr(routes_mod, "_resolve_dav", dav_write)
    monkeypatch.setattr(routes_mod, "_format_meeting_detail_line", detail_line)
    monkeypatch.setattr(routes_mod, "_send_reschedule_emails", send)
    monkeypatch.setattr(t_cal, "calendar_find_free", find_free)
    monkeypatch.setattr(t_cal, "calendar_update", update)
    routes_mod._BOOKING_LOCKS.clear()
    r.client = TestClient(Starlette(routes=routes_mod.booking_routes), raise_server_exceptions=False)
    return r


def post(rig, payload=None, **over):
    b = {"start": START.isoformat(), "end": END.isoformat()}
    b.update(over)
    return rig.client.post("/booking/tok1/reschedule", json=b if payload is None else payload)


def test_happy_path_response_and_every_dependency_call(rig):
    resp = post(rig)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "start": "S", "end": "E"}
    assert rig.names() == [
        "rate", "get_row", "get_link", "dav_filtered", "dav_write", "find_free", "cal_update",
        "update", "detail_line", "send",
    ]
    assert rig.one("rate") == ("rate", "booking-manage:testclient", 10, 60)
    assert rig.one("get_row") == ("get_row", "tok1")
    assert rig.one("get_link") == ("get_link", "slug1")
    assert rig.one("dav_filtered") == ("dav_filtered", "proj", "alice", ACL)
    assert rig.one("dav_write") == ("dav_write", "proj", "alice", True)
    # Fönstret paddas med en minut; egna eventet exkluderas ur fri-kontrollen.
    assert rig.one("find_free") == (
        "find_free", ACL, "alice", "proj", 30, START.isoformat(),
        (END + timedelta(minutes=1)).isoformat(), DAV, "ev1",
    )
    assert rig.one("cal_update") == (
        "cal_update", ACL, "alice", "proj", "ev1",
        {"start": START.isoformat(), "end": END.isoformat(), "_dav": WRITE_DAV, "_confirmed": True},
    )
    assert rig.one("update") == (
        "update", "row1",
        {"event_id": "ev1", "meeting_start": int(START.timestamp()),
         "meeting_end": int(END.timestamp()), "status": "rescheduled"},
    )
    assert rig.one("detail_line") == ("detail_line", "google_meet", "https://meet/x")
    assert rig.one("send") == (
        "send", (ACL, "proj", rig.link, "Nytt", rig.event, "eva@example.com", START, END, "tok1",
                 "LINE", "https://meet/x"),
    )


def test_lock_held_for_find_free_and_update_but_released_for_email(rig):
    assert post(rig).status_code == 200
    assert rig.lock_state == {"find_free_locked": True, "update_locked": True, "send_locked": False}


def test_title_falls_back_to_link_template_when_event_has_none(rig):
    rig.event = {"id": "ev1", "start": "S", "end": "E"}
    post(rig)
    assert rig.one("send")[1][3] == "Möte med {name}"
    rig.calls.clear()
    rig.event = {"id": "ev1", "title": "", "start": "S", "end": "E"}
    post(rig)
    assert rig.one("send")[1][3] == "Möte med {name}"


def test_title_falls_back_to_default_moete_when_link_has_no_template(rig):
    rig.event = {"start": "S", "end": "E"}
    rig.link = {"project": "proj", "user": "alice"}
    post(rig)
    assert rig.one("send")[1][3] == "Möte"


def test_response_start_end_missing_from_event_are_null(rig):
    rig.event = {"title": "T"}
    resp = post(rig)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "start": None, "end": None}


def test_meeting_detail_line_uses_row_get_so_missing_columns_pass_none(rig):
    rig.row = {k: v for k, v in ROW.items() if not k.startswith("meeting_form")}
    assert post(rig).status_code == 200
    assert rig.one("detail_line") == ("detail_line", None, None)
    assert rig.one("send")[1][-1] is None


# --- refusals -----------------------------------------------------------


def test_rate_limited_is_429_and_nothing_else_runs(rig):
    rig.rate_ok = False
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (429, {"error": "rate_limited"})
    assert rig.names() == ["rate"]


def test_unknown_token_is_404(rig):
    rig.row = None
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert rig.names() == ["rate", "get_row"]


def test_cancelled_booking_is_409(rig):
    rig.row["status"] = "cancelled"
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (409, {"error": "already_cancelled"})
    assert rig.names() == ["rate", "get_row"]


def test_unparseable_json_is_treated_as_empty_body_and_refused_as_invalid(rig):
    resp = rig.client.post("/booking/tok1/reschedule", content=b"{not json")
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_body"})
    assert rig.names() == ["rate", "get_row"]


@pytest.mark.parametrize("raw", [b"[]", b'"x"', b"null", b"3"])
def test_non_object_json_is_invalid_body(rig, raw):
    resp = rig.client.post("/booking/tok1/reschedule", content=raw)
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_body"})
    assert rig.names() == ["rate", "get_row"]


@pytest.mark.parametrize(
    "over",
    [
        {"start": None}, {"end": None}, {"start": "nonsense"}, {"end": "nonsense"},
        {"start": "", "end": ""}, {"end": START.isoformat()},
        {"end": (START - timedelta(minutes=5)).isoformat()},
    ],
)
def test_bad_or_inverted_times_are_invalid_body(rig, over):
    b = {"start": START.isoformat(), "end": END.isoformat()}
    b.update(over)
    b = {k: v for k, v in b.items() if v is not None}
    resp = rig.client.post("/booking/tok1/reschedule", json=b)
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_body"})
    assert rig.names() == ["rate", "get_row"]


@pytest.mark.parametrize("minutes", [1, 14, 241, 1000])
def test_duration_outside_bounds_is_invalid_duration(rig, minutes):
    resp = post(rig, end=(START + timedelta(minutes=minutes)).isoformat())
    assert (resp.status_code, resp.json()) == (400, {"error": "invalid_duration"})
    assert rig.names() == ["rate", "get_row"]


@pytest.mark.parametrize("minutes", [15, 240])
def test_duration_bounds_are_inclusive(rig, minutes):
    end = START + timedelta(minutes=minutes)
    rig.free = [{"start": START.isoformat(), "end": end.isoformat()}]
    resp = post(rig, end=end.isoformat())
    assert resp.status_code == 200
    assert rig.one("find_free")[4] == minutes


def test_row_without_slug_is_404_and_get_link_not_called(rig):
    rig.row["slug"] = ""
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert "get_link" not in rig.names()


def test_row_with_none_slug_is_404(rig):
    rig.row["slug"] = None
    assert post(rig).status_code == 404
    assert "get_link" not in rig.names()


def test_unknown_link_is_404(rig):
    rig.link = None
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert rig.names() == ["rate", "get_row", "get_link"]


def test_calendar_auth_required_on_read_adapter_is_404(rig):
    rig.dav_filtered_exc = CalendarAuthRequired("https://link", [])
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert rig.names() == ["rate", "get_row", "get_link", "dav_filtered"]


def test_calendar_auth_required_on_write_adapter_is_404(rig):
    rig.dav_write_exc = CalendarAuthRequired("https://link", [])
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (404, {"error": "not_found"})
    assert rig.names() == ["rate", "get_row", "get_link", "dav_filtered", "dav_write"]


def test_slot_unavailable_is_409_and_nothing_written(rig):
    rig.free = []
    resp = post(rig)
    assert (resp.status_code, resp.json()) == (409, {"error": "slot_unavailable"})
    assert rig.names() == ["rate", "get_row", "get_link", "dav_filtered", "dav_write", "find_free"]


@pytest.mark.parametrize(
    "slot",
    [
        {"start": (START + timedelta(minutes=1)).isoformat(), "end": END.isoformat()},
        {"start": START.isoformat(), "end": (END - timedelta(minutes=1)).isoformat()},
        {"start": "garbage", "end": END.isoformat()},
        {"start": START.isoformat(), "end": "garbage"},
        {},
    ],
)
def test_slot_that_does_not_cover_the_window_is_409(rig, slot):
    rig.free = [slot]
    assert post(rig).status_code == 409
    assert "cal_update" not in rig.names()


def test_any_covering_slot_among_several_is_enough(rig):
    rig.free = [
        {"start": "garbage", "end": "garbage"},
        {"start": (START - timedelta(hours=1)).isoformat(), "end": (END + timedelta(hours=1)).isoformat()},
    ]
    assert post(rig).status_code == 200


def test_slot_exactly_matching_window_covers(rig):
    rig.free = [{"start": START.isoformat(), "end": END.isoformat()}]
    assert post(rig).status_code == 200


# --- failure propagation (locked, not endorsed) -------------------------


def test_calendar_update_error_propagates_as_500_and_does_not_touch_store(rig, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("cal down")

    monkeypatch.setattr(t_cal, "calendar_update", boom)
    resp = post(rig)
    assert resp.status_code == 500
    assert "update" not in rig.names()
    # Locket måste släppas även vid fel.
    assert not routes_mod._BOOKING_LOCKS[("proj", "alice")].locked()


def test_email_error_propagates_after_store_update(rig, monkeypatch):
    def boom(*a):
        raise RuntimeError("smtp")

    monkeypatch.setattr(routes_mod, "_send_reschedule_emails", boom)
    resp = post(rig)
    assert resp.status_code == 500
    assert "update" in rig.names()
