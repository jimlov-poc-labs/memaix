# SPDX-License-Identifier: AGPL-3.0-or-later
"""Karakteriseringstester för booking.reminders.send_due_reminders.

Store, länkuppslag och mejlutskick är fakes, så att varje gren och varje
anrop (argument, ordning, räknare) är låsta oberoende av intern uppdelning.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from memaix_gateway.booking import reminders as reminders_mod
from memaix_gateway.booking import routes as routes_mod
from memaix_gateway.booking.reminders import REMINDER_OFFSETS_MIN, send_due_reminders

MEETING_START = 1_900_000_000
ACL = object()


class FakeStore:
    def __init__(self, rows, claim=True):
        self.rows = rows
        self.claim = claim
        self.calls: list = []
        self.claims: dict = {}

    def reminders_due(self, now_epoch, offsets):
        self.calls.append(("due", now_epoch, offsets))
        return self.rows

    def mark_reminder_sent(self, row_id, offset):
        self.calls.append(("mark", row_id, offset))
        c = self.claim
        return c(row_id, offset) if callable(c) else c


def row(**over):
    r = {
        "id": "r1", "project": "proj", "host_user": "alice", "event_id": "ev1",
        "visitor_email": "eva@example.com", "slug": "slug1", "manage_token": "mt",
        "meeting_start": MEETING_START, "meeting_end": MEETING_START + 1800,
        "reminders_sent": "", "meeting_form_provider": "phone", "meeting_form_detail": "+46",
    }
    r.update(over)
    return r


def at(offset_min, extra_s=0):
    return datetime.fromtimestamp(MEETING_START - offset_min * 60 + extra_s, tz=timezone.utc)


@pytest.fixture()
def emails(monkeypatch):
    sent: list = []
    monkeypatch.setattr(routes_mod, "_send_reminder_email", lambda *a, **kw: sent.append((a, kw)))
    monkeypatch.setattr(routes_mod, "_format_meeting_detail_line", lambda p, d: f"LINE:{p}:{d}")
    return sent


def link_fn(link=None, calls=None):
    def f(slug):
        if calls is not None:
            calls.append(slug)
        return link
    return f


LINK = {"project": "proj", "user": "alice", "title_template": "Samtal"}


def test_offsets_constant_is_what_the_tests_assume():
    assert 1440 in REMINDER_OFFSETS_MIN


def test_due_reminder_is_sent_with_exact_arguments_then_claimed(emails):
    store = FakeStore([row()])
    slugs: list = []
    now = at(1440)
    n = send_due_reminders(store, lambda: ACL, link_fn(LINK, slugs), now)
    assert n == 1
    assert slugs == ["slug1"]
    assert store.calls == [("due", int(now.timestamp()), REMINDER_OFFSETS_MIN), ("mark", "r1", 1440)]
    (args, kw), = emails
    assert kw == {}
    assert args == (
        ACL, "proj", LINK, "Samtal", "ev1", "eva@example.com",
        datetime.fromtimestamp(MEETING_START, tz=timezone.utc),
        datetime.fromtimestamp(MEETING_START + 1800, tz=timezone.utc),
        1440, "mt", "LINE:phone:+46", "+46",
    )


def test_title_defaults_to_moete_without_template(emails):
    send_due_reminders(FakeStore([row()]), lambda: ACL, link_fn({"project": "proj"}), at(1440))
    assert emails[0][0][3] == "Möte"


def test_missing_meeting_end_falls_back_to_start(emails):
    send_due_reminders(FakeStore([row(meeting_end=None)]), lambda: ACL, link_fn(LINK), at(1440))
    a = emails[0][0]
    assert a[6] == a[7] == datetime.fromtimestamp(MEETING_START, tz=timezone.utc)


def test_zero_meeting_end_falls_back_to_start(emails):
    send_due_reminders(FakeStore([row(meeting_end=0)]), lambda: ACL, link_fn(LINK), at(1440))
    assert emails[0][0][7] == datetime.fromtimestamp(MEETING_START, tz=timezone.utc)


def test_missing_optional_columns_use_defaults(emails):
    r = row()
    for k in ("manage_token", "meeting_form_provider", "meeting_form_detail"):
        del r[k]
    send_due_reminders(FakeStore([r]), lambda: ACL, link_fn(LINK), at(1440))
    a = emails[0][0]
    assert a[9] == ""
    assert a[10] == "LINE:None:None"
    assert a[11] is None


def test_no_slug_skips_without_lookup_or_claim(emails):
    store = FakeStore([row(slug="")])
    slugs: list = []
    assert send_due_reminders(store, lambda: ACL, link_fn(LINK, slugs), at(1440)) == 0
    assert slugs == [] and emails == []
    assert [c[0] for c in store.calls] == ["due"]


def test_none_slug_skips(emails):
    store = FakeStore([row(slug=None)])
    assert send_due_reminders(store, lambda: ACL, link_fn(LINK), at(1440)) == 0
    assert emails == [] and [c[0] for c in store.calls] == ["due"]


def test_unknown_link_skips_without_claim(emails):
    store = FakeStore([row()])
    assert send_due_reminders(store, lambda: ACL, link_fn(None), at(1440)) == 0
    assert emails == [] and [c[0] for c in store.calls] == ["due"]


def test_lost_claim_after_send_is_not_counted(emails):
    store = FakeStore([row()], claim=False)
    assert send_due_reminders(store, lambda: ACL, link_fn(LINK), at(1440)) == 0
    assert len(emails) == 1  # skickat, men inte räknat
    assert ("mark", "r1", 1440) in store.calls


def test_already_sent_offset_is_not_resent(emails):
    store = FakeStore([row(reminders_sent="1440")])
    assert send_due_reminders(store, lambda: ACL, link_fn(LINK), at(1440)) == 0
    assert emails == []
    assert [c[0] for c in store.calls] == ["due"]


def test_stale_offsets_are_closed_out_without_sending(emails):
    # 10 minuter före mötet: alla större offsets har passerat grace-fönstret.
    store = FakeStore([row()])
    now = at(10)
    n = send_due_reminders(store, lambda: ACL, link_fn(LINK), now)
    from memaix_gateway.booking.reminders import stale_offsets

    expected = stale_offsets(MEETING_START, set(), int(now.timestamp()))
    assert expected  # sanity
    assert n == 0 and emails == []
    assert [c for c in store.calls if c[0] == "mark"] == [("mark", "r1", o) for o in expected]


def test_stale_offsets_closed_before_due_ones_sent_in_same_row(emails):
    store = FakeStore([row()])
    now = at(1440)
    now_epoch = int(now.timestamp())
    from memaix_gateway.booking.reminders import due_offsets, stale_offsets

    stale = stale_offsets(MEETING_START, set(), now_epoch)
    due = due_offsets(MEETING_START, set(), now_epoch)
    assert due == [1440]
    send_due_reminders(store, lambda: ACL, link_fn(LINK), now)
    marks = [c[2] for c in store.calls if c[0] == "mark"]
    assert marks == stale + due


def test_already_sent_set_ignores_blank_entries(emails):
    store = FakeStore([row(reminders_sent=",1440,")])
    assert send_due_reminders(store, lambda: ACL, link_fn(LINK), at(1440)) == 0


def test_send_failure_is_logged_not_claimed_and_does_not_block_next_row(emails, monkeypatch, caplog):
    calls: list = []

    def flaky(*a, **kw):
        calls.append(a[4])
        if a[4] == "ev1":
            raise RuntimeError("smtp")

    monkeypatch.setattr(routes_mod, "_send_reminder_email", flaky)
    store = FakeStore([row(), row(id="r2", event_id="ev2")])
    with caplog.at_level(logging.ERROR, logger=reminders_mod.logger.name):
        n = send_due_reminders(store, lambda: ACL, link_fn(LINK), at(1440))
    assert n == 1
    assert calls == ["ev1", "ev2"]
    assert ("mark", "r1", 1440) not in store.calls
    assert ("mark", "r2", 1440) in store.calls
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert msgs == ["booking reminder failed for row=r1 project=proj host_user=alice offset=1440"]


def test_link_lookup_error_is_isolated_per_offset(emails, caplog):
    def bad(slug):
        raise RuntimeError("db")

    store = FakeStore([row()])
    with caplog.at_level(logging.ERROR, logger=reminders_mod.logger.name):
        assert send_due_reminders(store, lambda: ACL, bad, at(1440)) == 0
    assert emails == [] and len(caplog.records) == 1


def test_acl_factory_called_per_send(emails):
    n_acl: list = []

    def acl():
        n_acl.append(1)
        return ACL

    send_due_reminders(FakeStore([row(), row(id="r2")]), acl, link_fn(LINK), at(1440))
    assert len(n_acl) == 2


def test_multiple_rows_counted_and_empty_store_returns_zero(emails):
    assert send_due_reminders(FakeStore([]), lambda: ACL, link_fn(LINK), at(1440)) == 0
    assert send_due_reminders(FakeStore([row(), row(id="r2")]), lambda: ACL, link_fn(LINK), at(1440)) == 2


def test_two_due_offsets_in_one_row_both_sent(emails, monkeypatch):
    monkeypatch.setattr(reminders_mod, "due_offsets", lambda s, sent, n: [1440, 60])
    store = FakeStore([row()])
    assert send_due_reminders(store, lambda: ACL, link_fn(LINK), at(1440)) == 2
    assert [c[2] for c in store.calls if c[0] == "mark"][-2:] == [1440, 60]
