# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import datetime, timezone

import pytest

from memaix_gateway.acl import Acl
from memaix_gateway.connectors.calendar_sources import resolve_effective_sources
from memaix_gateway.connectors.native_calendar import NativeCalendarAdapter
from memaix_gateway.tools.calendar import (
    calendar_create,
    calendar_find_free,
    get_status,
    setup_mode,
)
from tests.test_calendar_setup import _FakeStore


def _dt(s):
    return datetime.fromisoformat(s)


@pytest.fixture()
def acl(tmp_path):
    return Acl(users={"q": {"grants": {"p": "owner"}}}, projects={"p": {"vault": str(tmp_path)}})


def test_adapter_crud_roundtrip(tmp_path):
    cal = NativeCalendarAdapter(tmp_path / "c.json")
    start, end = _dt("2026-01-05T10:00:00+00:00"), _dt("2026-01-05T12:00:00+00:00")
    ev = cal.create_event("u1", "Session", start, end, ["a@b.se"])
    assert ev["id"] == "u1" and ev["source_busy"] is True
    assert [e["id"] for e in cal.find_events(_dt("2026-01-05T00:00:00+00:00"), _dt("2026-01-06T00:00:00+00:00"))] == ["u1"]
    assert cal.find_events(_dt("2026-01-05T12:00:00+00:00"), _dt("2026-01-06T00:00:00+00:00")) == []
    cal.update_event("u1", title="Ny", start="2026-01-05T14:00:00+00:00", end="2026-01-05T16:00:00+00:00")
    assert cal.find_events(_dt("2026-01-05T00:00:00+00:00"), _dt("2026-01-05T13:00:00+00:00")) == []
    cal.delete_event("u1")
    assert cal.find_events(_dt("2026-01-01T00:00:00+00:00"), _dt("2026-02-01T00:00:00+00:00")) == []


def test_adapter_missing_event_raises(tmp_path):
    cal = NativeCalendarAdapter(tmp_path / "c.json")
    with pytest.raises(KeyError):
        cal.delete_event("nope")
    with pytest.raises(KeyError):
        cal.update_event("nope", title="x")


def test_setup_native_status_and_none(acl):
    store = _FakeStore()
    assert setup_mode(acl, "q", "p", "native", store, "")["ok"] is True
    assert get_status("q", "p", acl, store)["active_mode"] == "native"
    assert setup_mode(acl, "q", "p", "none", store, "")["ok"] is True
    assert get_status("q", "p", acl, store)["active_mode"] == "none"


def test_booked_event_blocks_find_free(acl, tmp_path):
    cal = NativeCalendarAdapter(tmp_path / "c.json")
    calendar_create(acl, "q", "p", "Bokning", "2026-01-05T10:00:00+00:00", "2026-01-05T12:00:00+00:00",
                    _dav=cal, _confirmed=True)
    free = calendar_find_free(acl, "q", "p", 60, "2026-01-05T08:00:00+00:00", "2026-01-05T14:00:00+00:00", _dav=cal)
    assert free == [
        {"start": "2026-01-05T08:00:00+00:00", "end": "2026-01-05T10:00:00+00:00"},
        {"start": "2026-01-05T12:00:00+00:00", "end": "2026-01-05T14:00:00+00:00"},
    ]


def test_native_is_an_effective_source(acl):
    store = _FakeStore()
    setup_mode(acl, "q", "p", "native", store, "")
    labels = [label for label, _ in resolve_effective_sources(acl, store, "p", "q")]
    assert "native" in labels
