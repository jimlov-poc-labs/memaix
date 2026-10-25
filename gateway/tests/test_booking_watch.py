# SPDX-License-Identifier: AGPL-3.0-or-later
"""Booking watch: alert after repeated empty checks, say why, recover, never
leak the slug (booking/watch.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memaix_gateway.booking import watch
from memaix_gateway.booking.watch import LinkState, evaluate, run_checks
from memaix_gateway.tools import calendar as t_cal

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
LINK = {"project": "jimlov", "user": "jimmy", "duration_min": 30, "max_days_ahead": 60}
EMPTY = {"status": "empty", "reason": "x"}
OK = {"status": "ok", "reason": ""}


class _Dav:
    def __init__(self, events):
        self._events = events

    def find_events(self, start, end):
        return self._events


def _patch(monkeypatch, *, enabled=True, windows=()):
    monkeypatch.setattr(t_cal, "calendar_booking_enabled_get", lambda acl, u, p: {"enabled": enabled})
    monkeypatch.setattr(t_cal, "calendar_find_free", lambda *a, **k: list(windows))


def test_alerts_only_after_two_empty_checks():
    s = LinkState()
    assert evaluate(s, EMPTY, 0) is None
    assert evaluate(s, EMPTY, 1800) == "alert"


def test_no_repeat_alert_within_a_day_then_realerts():
    s = LinkState()
    evaluate(s, EMPTY, 0)
    assert evaluate(s, EMPTY, 1) == "alert"
    assert evaluate(s, EMPTY, 3600) is None
    assert evaluate(s, EMPTY, 86402) == "alert"


def test_single_blip_never_alerts_and_streak_resets():
    s = LinkState()
    evaluate(s, EMPTY, 0)
    assert evaluate(s, OK, 1) is None
    assert evaluate(s, EMPTY, 2) is None


def test_recovery_is_reported_once():
    s = LinkState()
    evaluate(s, EMPTY, 0)
    evaluate(s, EMPTY, 1)
    assert evaluate(s, OK, 2) == "recovered"
    assert evaluate(s, OK, 3) is None


def test_skipped_resets_without_alerting():
    s = LinkState()
    evaluate(s, EMPTY, 0)
    assert evaluate(s, {"status": "skipped", "reason": ""}, 1) is None
    assert s.empty_streak == 0


def test_disabled_booking_is_skipped(monkeypatch):
    _patch(monkeypatch, enabled=False)
    assert watch.check_link(LINK, None, lambda *a: _Dav([]), NOW)["status"] == "skipped"


def test_windows_mean_ok(monkeypatch):
    _patch(monkeypatch, windows=[{"start": "a", "end": "b"}])
    assert watch.check_link(LINK, None, lambda *a: _Dav([]), NOW)["status"] == "ok"


def test_empty_names_the_long_event(monkeypatch):
    _patch(monkeypatch)
    long_event = {"title": "Kontoret", "start": "2026-11-02T08:00:00Z", "end": "2030-11-08T16:00:00Z"}
    short = {"title": "Möte", "start": "2026-11-02T08:00:00Z", "end": "2026-11-02T09:00:00Z"}
    r = watch.check_link(LINK, None, lambda *a: _Dav([long_event, short]), NOW)
    assert r["status"] == "empty"
    assert "Kontoret" in r["reason"] and "Möte" not in r["reason"]
    assert "2 upptagna" in r["reason"]


def test_calendar_failure_is_an_error_not_a_crash(monkeypatch):
    _patch(monkeypatch)

    def boom(*a):
        raise RuntimeError("down")

    assert watch.check_link(LINK, None, boom, NOW)["status"] == "error"


def test_run_checks_mails_without_the_slug_and_survives_a_bad_link(monkeypatch):
    _patch(monkeypatch)
    sent, states = [], {}
    links = [("SECRETSLUG", {"project": "p"}), ("SECRETSLUG2", LINK)]
    for i in range(2):
        run_checks(links, states, None, lambda *a: _Dav([]), lambda s, b: sent.append((s, b)), NOW + timedelta(minutes=30 * i))
    assert len(sent) == 1
    subject, body = sent[0]
    assert "jimlov/jimmy" in subject
    assert "SECRETSLUG" not in subject + body
