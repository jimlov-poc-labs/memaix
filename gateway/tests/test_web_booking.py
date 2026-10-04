# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for the host's booking settings (/app/api/booking): auth, RBAC and
save/read round trips. Same auth-bypass pattern as test_web_mvp_api.py —
_require_user is patched; role behaviour is exercised through the Acl."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.web import routes as web_routes_mod
from memaix_gateway.web.api import booking as booking_api

WEEK = {"mon": [{"start": "09:00", "end": "12:00"}], "tue": [{"start": "13:00", "end": "17:00"}]}


class _FakeTokenStore:
    def __init__(self):
        self.records = {}

    def list_accounts(self, user):
        return [
            {"provider": p, "account": a, "status": "active", "scopes": ""}
            for (u, p, a) in self.records
            if u == user
        ]

    def store(self, user, provider, account, data):
        self.records[(user, provider, account)] = data

    def delete(self, user, provider, account):
        return self.records.pop((user, provider, account), None) is not None

    def load_one(self, user, provider, account):
        return self.records.get((user, provider, account))


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    acl = Acl(
        users={
            "alice": {"grants": {"proj": "owner"}},
            "bob": {"grants": {"proj": "reader"}},
            "mallory": {"grants": {"other": "owner"}},
        },
        projects={"proj": {"vault": str(vault)}, "other": {"vault": str(tmp_path / "v2")}},
    )
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    current = {"user": "alice"}
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: current["user"])
    store = _FakeTokenStore()
    monkeypatch.setattr(booking_api, "_token_store", lambda: store)
    client = TestClient(Starlette(routes=web_routes_mod.web_routes))
    return client, current, store


def _post(client, path, **body):
    return client.post(f"/app/api/booking/{path}", json={"project": "proj", **body})


# ------------------------------------------------------------------
# Auth + RBAC
# ------------------------------------------------------------------


def test_401_without_login(rig, monkeypatch):
    client, _, _ = rig
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    assert client.get("/app/api/booking?project=proj").status_code == 401
    assert _post(client, "schedule", max_per_day=2).status_code == 401
    assert _post(client, "enabled", enabled=True).status_code == 401
    assert _post(client, "meeting-types", name="x", duration_min=30).status_code == 401
    assert client.delete("/app/api/booking/meeting-types/x?project=proj").status_code == 401


def test_user_without_grant_is_forbidden_everywhere(rig):
    client, current, _ = rig
    current["user"] = "mallory"
    assert client.get("/app/api/booking?project=proj").status_code == 403
    assert _post(client, "schedule", max_per_day=2).status_code == 403
    assert _post(client, "enabled", enabled=True).status_code == 403
    assert _post(client, "meeting-types", name="x", duration_min=30).status_code == 403
    assert client.delete("/app/api/booking/meeting-types/x?project=proj").status_code == 403


def test_reader_can_read_but_not_write(rig):
    client, current, _ = rig
    current["user"] = "bob"
    assert client.get("/app/api/booking?project=proj").status_code == 200
    assert _post(client, "schedule", week=WEEK).status_code == 403
    assert _post(client, "enabled", enabled=True).status_code == 403
    assert _post(client, "meeting-types", name="x", duration_min=30).status_code == 403
    assert client.delete("/app/api/booking/meeting-types/x?project=proj").status_code == 403


def test_page_is_served_and_nav_present(rig):
    client, _, _ = rig
    page = client.get("/app/booking")
    assert page.status_code == 200
    assert 'id="hours-form"' in page.text and 'href="/app/booking"' in page.text


# ------------------------------------------------------------------
# Round trips
# ------------------------------------------------------------------


def test_fresh_state_defaults(rig):
    client, _, _ = rig
    state = client.get("/app/api/booking?project=proj").json()
    assert state == {
        "enabled": False, "calendar_mode": "none", "tz": "Europe/Stockholm",
        "week": {}, "weeks": {}, "dates": {}, "blocks": [], "max_per_day": None,
        "meeting_types": [],
    }


def test_week_and_tz_roundtrip(rig):
    client, _, _ = rig
    resp = _post(client, "schedule", tz="Europe/Oslo", week=WEEK)
    assert resp.status_code == 200
    state = client.get("/app/api/booking?project=proj").json()
    assert state["tz"] == "Europe/Oslo" and state["week"] == WEEK


def test_even_odd_weeks_roundtrip_and_clear(rig):
    client, _, _ = rig
    weeks = {"even": WEEK, "odd": {"wed": [{"start": "10:00", "end": "14:00"}]}}
    assert _post(client, "schedule", week=WEEK, weeks=weeks).status_code == 200
    assert client.get("/app/api/booking?project=proj").json()["weeks"] == weeks
    assert _post(client, "schedule", weeks=None).status_code == 200
    state = client.get("/app/api/booking?project=proj").json()
    assert state["weeks"] == {} and state["week"] == WEEK


def test_dates_closed_and_open_then_remove(rig):
    client, _, _ = rig
    dates = {"2030-12-24": [], "2030-12-28": [{"start": "10:00", "end": "12:00"}]}
    assert _post(client, "schedule", dates=dates).status_code == 200
    assert client.get("/app/api/booking?project=proj").json()["dates"] == dates
    assert _post(client, "schedule", dates={"2030-12-28": dates["2030-12-28"]}).status_code == 200
    assert list(client.get("/app/api/booking?project=proj").json()["dates"]) == ["2030-12-28"]


def test_blocks_single_and_recurring_roundtrip(rig):
    client, _, _ = rig
    blocks = [
        {"start": "2030-01-10T09:00:00+01:00", "end": "2030-01-10T11:00:00+01:00"},
        {"weekday": "fri", "start": "14:00", "end": "16:00", "parity": "odd"},
    ]
    assert _post(client, "schedule", blocks=blocks).status_code == 200
    assert client.get("/app/api/booking?project=proj").json()["blocks"] == blocks
    assert _post(client, "schedule", blocks=blocks[:1]).status_code == 200
    assert client.get("/app/api/booking?project=proj").json()["blocks"] == blocks[:1]


def test_daily_cap_set_and_clear(rig):
    client, _, _ = rig
    assert _post(client, "schedule", max_per_day=3).status_code == 200
    assert client.get("/app/api/booking?project=proj").json()["max_per_day"] == 3
    assert _post(client, "schedule", max_per_day=None).status_code == 200
    assert client.get("/app/api/booking?project=proj").json()["max_per_day"] is None


def test_schedule_keys_not_sent_are_left_alone(rig):
    client, _, _ = rig
    _post(client, "schedule", week=WEEK, max_per_day=2)
    _post(client, "schedule", dates={"2030-05-01": []})
    state = client.get("/app/api/booking?project=proj").json()
    assert state["week"] == WEEK and state["max_per_day"] == 2


def test_enabled_toggle(rig):
    client, _, _ = rig
    assert _post(client, "enabled", enabled=True).json() == {"ok": True, "enabled": True}
    assert client.get("/app/api/booking?project=proj").json()["enabled"] is True
    _post(client, "enabled", enabled=False)
    assert client.get("/app/api/booking?project=proj").json()["enabled"] is False


def test_meeting_types_add_edit_default_delete(rig):
    client, _, _ = rig
    assert _post(client, "meeting-types", name="Kort samtal", duration_min=30).status_code == 200
    assert _post(client, "meeting-types", name="Djupdykning", duration_min=90).status_code == 200
    types = client.get("/app/api/booking?project=proj").json()["meeting_types"]
    assert [(t["slug"], t["duration_min"], t["default"]) for t in types] == [
        ("kort-samtal", 30, True), ("djupdykning", 90, False),
    ]

    # Edit by slug (rename + new length) and move the default.
    resp = _post(client, "meeting-types", slug="djupdykning", name="Lång session",
                 duration_min=120, default=True)
    assert resp.status_code == 200
    types = client.get("/app/api/booking?project=proj").json()["meeting_types"]
    assert [(t["slug"], t["name"], t["duration_min"], t["default"]) for t in types] == [
        ("kort-samtal", "Kort samtal", 30, False), ("djupdykning", "Lång session", 120, True),
    ]

    assert client.delete("/app/api/booking/meeting-types/djupdykning?project=proj").status_code == 200
    types = client.get("/app/api/booking?project=proj").json()["meeting_types"]
    assert [(t["slug"], t["default"]) for t in types] == [("kort-samtal", True)]


def test_meeting_type_slug_collision_gets_suffix(rig):
    client, _, _ = rig
    _post(client, "meeting-types", name="Möte", duration_min=30)
    _post(client, "meeting-types", name="Möte", duration_min=45)
    slugs = [t["slug"] for t in client.get("/app/api/booking?project=proj").json()["meeting_types"]]
    assert slugs == ["mote", "mote-2"]


def test_calendar_mode_reflects_native(rig):
    client, _, store = rig
    from memaix_gateway.tools import calendar as t_cal
    from memaix_gateway.web import routes as w

    t_cal.setup_mode(w._get_acl(), "alice", "proj", "native", store, "https://x")
    assert client.get("/app/api/booking?project=proj").json()["calendar_mode"] == "native"


def test_response_never_contains_booking_link_slug(rig, monkeypatch):
    client, _, _ = rig
    monkeypatch.setattr(
        "memaix_gateway.booking.links.get_link",
        lambda slug: {"slug": slug, "user": "alice", "project": "proj"},
    )
    body = client.get("/app/api/booking?project=proj").text
    assert "link" not in body.lower() and "/book/" not in body


# ------------------------------------------------------------------
# Invalid input -> 400
# ------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"week": {"mon": [{"start": "12:00", "end": "09:00"}]}},                       # end before start
    {"week": {"mon": [{"start": "09:00", "end": "12:00"}, {"start": "11:00", "end": "13:00"}]}},  # overlap
    {"week": {"xyz": [{"start": "09:00", "end": "12:00"}]}},                       # unknown weekday
    {"week": {"mon": "9-12"}},                                                      # wrong shape
    {"week": [1, 2]},
    {"tz": "Mars/Olympus"},
    {"tz": 5},
    {"weeks": {"third": {}}},
    {"weeks": []},
    {"dates": {"2030-13-45": []}},
    {"dates": {"2030-01-01": [{"start": "10:00"}]}},
    {"blocks": [{"start": "2030-01-10T09:00:00", "end": "2030-01-10T11:00:00"}]},   # naive times
    {"blocks": [{"weekday": "fri", "start": "14:00", "end": "16:00", "parity": "third"}]},
    {"blocks": "nope"},
    {"max_per_day": -1},
    {"max_per_day": 2.5},
    {"max_per_day": "many"},
    {"max_per_day": True},
])
def test_invalid_schedule_is_400_and_stores_nothing(rig, payload):
    client, _, _ = rig
    resp = _post(client, "schedule", **payload)
    assert resp.status_code == 400
    assert resp.json()["error"]
    state = client.get("/app/api/booking?project=proj").json()
    assert state["week"] == {} and state["blocks"] == [] and state["max_per_day"] is None


def test_invalid_enabled_is_400(rig):
    client, _, _ = rig
    assert _post(client, "enabled", enabled="yes").status_code == 400
    assert _post(client, "enabled").status_code == 400


@pytest.mark.parametrize("payload", [
    {"name": "", "duration_min": 30},
    {"name": "x"},
    {"name": "x", "duration_min": 0},
    {"name": "x", "duration_min": 999999},
    {"name": "x", "duration_min": "30"},
    {"name": "x", "duration_min": True},
    {"duration_min": 30},
])
def test_invalid_meeting_type_is_400(rig, payload):
    client, _, _ = rig
    assert _post(client, "meeting-types", **payload).status_code == 400
    assert client.get("/app/api/booking?project=proj").json()["meeting_types"] == []


def test_non_json_body_is_400(rig):
    client, _, _ = rig
    for path in ("schedule", "enabled", "meeting-types"):
        assert client.post(f"/app/api/booking/{path}", content=b"not json").status_code == 400
