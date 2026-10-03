# SPDX-License-Identifier: AGPL-3.0-or-later
"""_PerUserGoogleAdapter.list_events must not turn an auth failure into an
empty calendar (card a1cc5afd). Only network trouble is tolerated."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import requests

from memaix_gateway.tools.calendar import _PerUserGoogleAdapter

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 8, tzinfo=timezone.utc)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._body


def _event(i):
    return {"id": f"e{i}", "summary": f"Möte {i}", "start": {"dateTime": "2026-10-02T09:00:00Z"},
            "end": {"dateTime": "2026-10-02T10:00:00Z"}}


def _route(monkeypatch, routes):
    """routes: substring of URL -> _Resp or Exception to raise."""
    def fake_get(url, **kwargs):
        for needle, outcome in routes.items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected URL {url}")
    monkeypatch.setattr(requests, "get", fake_get)


def test_calendar_list_401_bubbles_up(monkeypatch):
    _route(monkeypatch, {"calendarList": _Resp(401), "/events": _Resp(200, {"items": [_event(1)]})})
    with pytest.raises(requests.HTTPError):
        _PerUserGoogleAdapter("tok").list_events(START, END)


def test_calendar_list_403_falls_back_to_primary(monkeypatch):
    # Accounts linked with only calendar.events: calendarList is 403, "primary" works.
    _route(monkeypatch, {"calendarList": _Resp(403), "/calendars/primary/events": _Resp(200, {"items": [_event(1)]})})
    assert [e["id"] for e in _PerUserGoogleAdapter("tok").list_events(START, END)] == ["e1"]


def test_calendar_list_500_bubbles_up(monkeypatch):
    _route(monkeypatch, {"calendarList": _Resp(500), "/events": _Resp(200, {"items": [_event(1)]})})
    with pytest.raises(requests.HTTPError):
        _PerUserGoogleAdapter("tok").list_events(START, END)


@pytest.mark.parametrize("status", [401, 403])
def test_events_auth_error_bubbles_up(monkeypatch, status):
    _route(monkeypatch, {"calendarList": _Resp(200, {"items": [{"id": "primary"}]}), "/events": _Resp(status)})
    with pytest.raises(requests.HTTPError):
        _PerUserGoogleAdapter("tok").list_events(START, END)


@pytest.mark.parametrize("exc", [requests.ConnectionError("down"), requests.Timeout("slow")])
def test_network_trouble_on_calendar_list_falls_back_to_primary(monkeypatch, exc):
    _route(monkeypatch, {"calendarList": exc, "/calendars/primary/events": _Resp(200, {"items": [_event(1)]})})
    assert [e["id"] for e in _PerUserGoogleAdapter("tok").list_events(START, END)] == ["e1"]


def test_network_trouble_on_one_calendar_keeps_the_others(monkeypatch):
    _route(monkeypatch, {
        "calendarList": _Resp(200, {"items": [{"id": "a"}, {"id": "b"}]}),
        "/calendars/a/events": requests.Timeout("slow"),
        "/calendars/b/events": _Resp(200, {"items": [_event(2)]}),
    })
    assert [e["id"] for e in _PerUserGoogleAdapter("tok").list_events(START, END)] == ["e2"]


def test_healthy_account_still_lists_events(monkeypatch):
    _route(monkeypatch, {"calendarList": _Resp(200, {"items": [{"id": "primary"}]}),
                         "/events": _Resp(200, {"items": [_event(1), _event(2)]})})
    assert len(_PerUserGoogleAdapter("tok").list_events(START, END)) == 2
