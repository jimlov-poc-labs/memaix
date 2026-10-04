# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import date

import pytest

from memaix_gateway.connectors.working_hours import (
    apply_day_cap,
    apply_schedule,
    validate_schedule,
)

TZ = "Europe/Stockholm"


def _gap(start, end):
    return [{"start": start, "end": end}]


# 2026-01-05 is Monday of ISO week 2 (even); 2026-01-12 is week 3 (odd).
EVENING = [{"start": "17:00", "end": "22:00"}]


def test_no_schedule_is_wide_open():
    free = _gap("2026-01-05T00:00:00+00:00", "2026-01-06T00:00:00+00:00")
    assert apply_schedule(free, {"tz": TZ}) == free


def test_parity_even_week_released_odd_week_closed():
    sched = {"tz": TZ, "week": {}, "weeks": {"even": {"mon": EVENING}, "odd": {}}}
    free = _gap("2026-01-05T00:00:00+00:00", "2026-01-13T00:00:00+00:00")
    out = apply_schedule(free, sched)
    assert out == [{"start": "2026-01-05T16:00:00+00:00", "end": "2026-01-05T21:00:00+00:00"}]


def test_parity_falls_back_to_base_week_when_variant_missing():
    sched = {"tz": TZ, "week": {"mon": EVENING}, "weeks": {"odd": {}}}
    free = _gap("2026-01-05T00:00:00+00:00", "2026-01-13T00:00:00+00:00")
    assert len(apply_schedule(free, sched)) == 1  # even Monday uses base, odd Monday closed


def test_date_lock_removes_day():
    sched = {"tz": TZ, "week": {"mon": EVENING}, "dates": {"2026-01-05": []}}
    free = _gap("2026-01-05T00:00:00+00:00", "2026-01-06T00:00:00+00:00")
    assert apply_schedule(free, sched) == []


def test_date_release_opens_otherwise_closed_day():
    sched = {"tz": TZ, "week": {"mon": EVENING}, "dates": {"2026-01-06": [{"start": "10:00", "end": "12:00"}]}}
    free = _gap("2026-01-06T00:00:00+00:00", "2026-01-07T00:00:00+00:00")
    assert apply_schedule(free, sched) == [
        {"start": "2026-01-06T09:00:00+00:00", "end": "2026-01-06T11:00:00+00:00"}
    ]


def test_single_block_splits_window():
    sched = {
        "tz": TZ,
        "week": {"mon": [{"start": "09:00", "end": "17:00"}]},
        "blocks": [{"start": "2026-01-05T10:00:00+00:00", "end": "2026-01-05T12:00:00+00:00"}],
    }
    free = _gap("2026-01-05T00:00:00+00:00", "2026-01-06T00:00:00+00:00")
    assert apply_schedule(free, sched) == [
        {"start": "2026-01-05T08:00:00+00:00", "end": "2026-01-05T10:00:00+00:00"},
        {"start": "2026-01-05T12:00:00+00:00", "end": "2026-01-05T16:00:00+00:00"},
    ]


def test_recurring_block_with_parity_only_hits_that_week():
    sched = {
        "tz": TZ,
        "week": {"mon": [{"start": "09:00", "end": "17:00"}]},
        "blocks": [{"weekday": "mon", "start": "00:00", "end": "24:00", "parity": "odd"}],
    }
    free = _gap("2026-01-05T00:00:00+00:00", "2026-01-13T00:00:00+00:00")
    out = apply_schedule(free, sched)
    assert [s["start"][:10] for s in out] == ["2026-01-05"]


def test_blocks_only_schedule_leaves_rest_open():
    sched = {"tz": TZ, "blocks": [{"weekday": "tue", "start": "00:00", "end": "24:00"}]}
    free = _gap("2026-01-05T00:00:00+00:00", "2026-01-08T00:00:00+00:00")
    out = apply_schedule(free, sched)
    assert not any(s["start"].startswith("2026-01-06T") and s["end"] > "2026-01-06T00:00:00+00:00"
                   and s["start"] < "2026-01-06T23:00:00+00:00" for s in out)
    assert out[0]["start"] == "2026-01-05T00:00:00+00:00"


def test_day_cap_removes_whole_day_once_full():
    free = _gap("2026-01-05T08:00:00+00:00", "2026-01-06T20:00:00+00:00")
    out = apply_day_cap(free, TZ, {date(2026, 1, 5): 1}, 1)
    assert out == [{"start": "2026-01-05T23:00:00+00:00", "end": "2026-01-06T20:00:00+00:00"}]


def test_day_cap_none_is_noop():
    free = _gap("2026-01-05T08:00:00+00:00", "2026-01-06T20:00:00+00:00")
    assert apply_day_cap(free, TZ, {date(2026, 1, 5): 5}, None) == free


@pytest.mark.parametrize(
    "bad",
    [
        {"tz": TZ, "weeks": {"third": {}}},
        {"tz": TZ, "dates": {"nope": []}},
        {"tz": TZ, "dates": {"2026-01-05": [{"start": "12:00", "end": "10:00"}]}},
        {"tz": TZ, "blocks": [{"start": "2026-01-05T10:00:00", "end": "2026-01-05T12:00:00"}]},
        {"tz": TZ, "blocks": [{"weekday": "mon", "start": "10:00", "end": "12:00", "parity": "x"}]},
        {"tz": TZ, "max_per_day": 0},
        {"tz": TZ, "max_per_day": True},
    ],
)
def test_validate_schedule_rejects(bad):
    with pytest.raises(ValueError):
        validate_schedule(bad)


def test_validate_schedule_accepts_full():
    validate_schedule({
        "tz": TZ, "week": {"mon": EVENING}, "weeks": {"even": {"mon": EVENING}},
        "dates": {"2026-01-05": []},
        "blocks": [{"start": "2026-01-05T10:00:00+00:00", "end": "2026-01-05T12:00:00+00:00"},
                   {"weekday": "mon", "start": "10:00", "end": "12:00", "parity": "even"}],
        "max_per_day": 1,
    })
