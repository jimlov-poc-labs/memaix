# SPDX-License-Identifier: AGPL-3.0-or-later
"""Booking page speed and correctness: transparent Google events must not
block, sources are fetched in parallel, and the visitor-facing free-window
lookup is cached briefly."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from memaix_gateway.booking import routes
from memaix_gateway.tools.calendar import (
    _MultiCalendarAdapter,
    _ServiceAccountGoogleCalendarAdapter,
)

START = datetime(2026, 11, 2, tzinfo=timezone.utc)
END = datetime(2026, 11, 9, tzinfo=timezone.utc)


def test_service_account_event_marked_free_is_not_busy():
    free = _ServiceAccountGoogleCalendarAdapter._to_dict(
        {"id": "a", "summary": "Kontoret", "transparency": "transparent",
         "start": {"dateTime": "2026-11-02T08:00:00Z"}, "end": {"dateTime": "2030-11-08T16:00:00Z"}}
    )
    busy = _ServiceAccountGoogleCalendarAdapter._to_dict(
        {"id": "b", "summary": "Möte", "start": {"dateTime": "2026-11-02T08:00:00Z"},
         "end": {"dateTime": "2026-11-02T09:00:00Z"}}
    )
    assert free["source_busy"] is False
    assert busy["source_busy"] is True


class _Slow:
    def __init__(self, title, delay, barrier=None):
        self.title, self.delay = title, delay

    def list_events(self, start, end):
        time.sleep(self.delay)
        return [{"title": self.title, "start": "2026-11-02T10:00:00Z", "end": "2026-11-02T11:00:00Z"}]


class _Boom:
    def list_events(self, start, end):
        raise RuntimeError("down")


def test_multi_adapter_fetches_sources_in_parallel_and_survives_a_failure():
    multi = _MultiCalendarAdapter([_Slow("a", 0.4), _Boom(), _Slow("b", 0.4), _Slow("c", 0.4)])
    t = time.monotonic()
    events = multi.list_events(START, END)
    elapsed = time.monotonic() - t
    assert sorted(e["title"] for e in events) == ["a", "b", "c"]
    assert elapsed < 0.9  # sequential would be >= 1.2 s


def test_multi_adapter_dedupe_keeps_first_source():
    first = type("F", (), {"list_events": lambda s, a, b: [{"title": "x", "start": "s", "marker": 1}]})()
    second = type("S", (), {"list_events": lambda s, a, b: [{"title": "x", "start": "s", "marker": 2}]})()
    out = _MultiCalendarAdapter([first, second]).list_events(START, END)
    assert [e["marker"] for e in out] == [1]


def test_free_windows_cached_within_ttl_then_recomputed(monkeypatch):
    calls = []
    key = ("p", "u", 30, "a", "b")

    def compute():
        calls.append(1)
        return [{"n": len(calls)}]

    assert routes._free_windows_cached(key, compute) == [{"n": 1}]
    assert routes._free_windows_cached(key, compute) == [{"n": 1}]
    assert len(calls) == 1
    other = ("p", "u", 30, "a", "c")
    assert routes._free_windows_cached(other, compute) == [{"n": 2}]

    monkeypatch.setattr(routes, "_FREE_CACHE_TTL_S", 0)
    assert routes._free_windows_cached(key, compute) == [{"n": 3}]


def test_free_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(routes, "_FREE_CACHE_MAX", 4)
    for i in range(20):
        routes._free_windows_cached(("k", i), lambda: [])
    assert len(routes._free_cache) <= 4
