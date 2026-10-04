# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for pm.allocate.allocate and pm.report.{utilization,variance}
(FEATURE-PM-ENGINE.md §4)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from memaix_gateway.pm.allocate import allocate
from memaix_gateway.pm.report import utilization, variance
from memaix_gateway.pm.store import PMStore

START = date(2025, 1, 6)


@pytest.fixture()
def store(tmp_path):
    return PMStore.for_path(tmp_path / "pm.db")


def test_allocate_assigns_single_task_to_only_resource(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t = store.add_task("acme", "Design", estimate_hours=16)

    result = allocate(store, scenario["id"], project_start=START)

    assert result["warnings"] == []
    allocs = store.list_allocations(scenario["id"])
    assert len(allocs) == 1
    assert allocs[0]["resource_id"] == r["id"]
    assert allocs[0]["task_id"] == t["id"]
    assert allocs[0]["start_date"] == "2025-01-06"
    assert allocs[0]["end_date"] == "2025-01-07"  # 16h at 8h/day = 2 days


def test_allocate_respects_required_skill(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    python_skill = store.get_or_create_skill("acme", "python")
    anna = store.add_resource("acme", "Anna")
    erik = store.add_resource("acme", "Erik")
    store.set_resource_skill(anna["id"], python_skill["id"])
    t = store.add_task("acme", "Backend work", estimate_hours=8, required_skill_id=python_skill["id"])

    allocate(store, scenario["id"], project_start=START)

    allocs = store.list_allocations(scenario["id"])
    assert allocs[0]["resource_id"] == anna["id"]
    assert erik["id"] not in [a["resource_id"] for a in allocs]


def test_allocate_warns_when_no_resource_has_required_skill(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    rare_skill = store.get_or_create_skill("acme", "cobol")
    store.add_resource("acme", "Anna")
    store.add_task("acme", "Legacy migration", estimate_hours=8, required_skill_id=rare_skill["id"])

    result = allocate(store, scenario["id"], project_start=START)

    assert store.list_allocations(scenario["id"]) == []
    assert any("no eligible resource" in w for w in result["warnings"])


def test_allocate_warns_on_missing_estimate(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    store.add_task("acme", "Unclear scope")  # no estimate_hours

    result = allocate(store, scenario["id"], project_start=START)

    assert store.list_allocations(scenario["id"]) == []
    assert any("no estimate" in w for w in result["warnings"])


def test_allocate_does_not_overbook_a_resource(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t1 = store.add_task("acme", "Task 1", estimate_hours=8, priority=1)
    t2 = store.add_task("acme", "Task 2", estimate_hours=8, priority=1)

    allocate(store, scenario["id"], project_start=START)

    allocs = {a["task_id"]: a for a in store.list_allocations(scenario["id"])}
    # Same resource, same 8h/day capacity, two independent 8h tasks -> must land on different days.
    assert allocs[t1["id"]]["start_date"] != allocs[t2["id"]]["start_date"]


def test_allocate_places_second_task_after_first_when_only_one_resource(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t1 = store.add_task("acme", "Task 1", estimate_hours=8, priority=1)
    t2 = store.add_task("acme", "Task 2", estimate_hours=8, priority=2)

    allocate(store, scenario["id"], project_start=START)
    allocs = {a["task_id"]: a for a in store.list_allocations(scenario["id"])}
    assert allocs[t1["id"]]["start_date"] == "2025-01-06"
    assert allocs[t2["id"]]["start_date"] == "2025-01-07"


def test_allocate_respects_dependency_order(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t1 = store.add_task("acme", "Design", estimate_hours=8)
    t2 = store.add_task("acme", "Build", estimate_hours=8)
    store.add_dependency(t1["id"], t2["id"])

    allocate(store, scenario["id"], project_start=START)
    allocs = {a["task_id"]: a for a in store.list_allocations(scenario["id"])}
    assert allocs[t2["id"]]["start_date"] > allocs[t1["id"]]["start_date"]


def test_allocate_respects_availability_exception(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_availability(r["id"], "2025-01-06", "2025-01-06", 0.0, reason="holiday")
    store.add_task("acme", "Task", estimate_hours=4)

    allocate(store, scenario["id"], project_start=START)
    allocs = store.list_allocations(scenario["id"])
    assert allocs[0]["start_date"] == "2025-01-07"  # skipped the 0-capacity day


def test_allocate_is_idempotent_replacing_prior_run(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_task("acme", "Task", estimate_hours=8)

    allocate(store, scenario["id"], project_start=START)
    allocate(store, scenario["id"], project_start=START)

    assert len(store.list_allocations(scenario["id"])) == 1


def test_allocate_writes_critical_path_schedule(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Task", estimate_hours=8)

    allocate(store, scenario["id"], project_start=START)

    sched = store.list_schedule(scenario["id"])
    assert len(sched) == 1
    assert sched[0]["task_id"] == t["id"]
    assert sched[0]["is_critical"] == 1


def test_allocate_unknown_scenario_raises(store):
    with pytest.raises(ValueError):
        allocate(store, 999)


# ------------------------------------------------------------------
# utilization
# ------------------------------------------------------------------


def test_utilization_full_capacity_used(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_task("acme", "Task", estimate_hours=16)
    allocate(store, scenario["id"], project_start=START)

    report = utilization(store, scenario["id"], "2025-01-06", "2025-01-07")
    assert report["resources"][0]["allocated_hours"] == 16.0
    assert report["resources"][0]["capacity_hours"] == 16.0
    assert report["resources"][0]["utilization_pct"] == 100.0


def test_utilization_zero_when_unallocated(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    report = utilization(store, scenario["id"], "2025-01-06", "2025-01-10")
    assert report["resources"][0]["allocated_hours"] == 0.0
    assert report["resources"][0]["utilization_pct"] == 0.0


def test_utilization_unknown_scenario_raises(store):
    with pytest.raises(ValueError):
        utilization(store, 999, "2025-01-06", "2025-01-10")


# ------------------------------------------------------------------
# variance
# ------------------------------------------------------------------


def test_variance_without_baseline_returns_error(store):
    store.add_task("acme", "Task", estimate_hours=8)
    result = variance(store, "acme")
    assert result["ok"] is False


def test_variance_flags_slippage_when_incomplete_past_planned_finish(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Task", estimate_hours=8)
    allocate(store, scenario["id"], project_start=START)
    store.commit_scenario(scenario["id"], "alice")

    result = variance(store, "acme", today=date(2025, 1, 20))
    row = result["tasks"][0]
    assert row["task_id"] == t["id"]
    assert row["slippage_days"] is not None
    assert row["slippage_days"] > 0


def test_variance_no_slippage_when_done_on_time(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Task", estimate_hours=8)
    allocate(store, scenario["id"], project_start=START)
    store.commit_scenario(scenario["id"], "alice")
    store.add_actual(t["id"], "2025-01-06", hours_logged=8.0, percent_complete=100.0)

    result = variance(store, "acme", today=date(2025, 1, 20))
    row = result["tasks"][0]
    assert row["slippage_days"] is None
    assert row["percent_complete"] == 100.0


def test_variance_hours_variance_computed(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Task", estimate_hours=8)
    allocate(store, scenario["id"], project_start=START)
    store.commit_scenario(scenario["id"], "alice")
    store.add_actual(t["id"], "2025-01-06", hours_logged=12.0, percent_complete=100.0)

    result = variance(store, "acme", today=date(2025, 1, 20))
    assert result["tasks"][0]["hours_variance"] == 4.0


def test_allocate_defaults_project_start_to_utc_today_not_server_local(store):
    # OPEN-GAPS.md #16 — must never fall back to server-local date.today().
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    store.add_task("acme", "Task", estimate_hours=8)

    result = allocate(store, scenario["id"])  # no project_start override

    utc_today = datetime.now(timezone.utc).date().isoformat()
    assert result["schedule"][0]["earliest_start"] == utc_today


def test_variance_defaults_today_to_utc_not_server_local(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Task", estimate_hours=8)
    allocate(store, scenario["id"], project_start=START)
    store.commit_scenario(scenario["id"], "alice")
    store.add_actual(t["id"], "2025-01-06", hours_logged=8.0, percent_complete=50.0)

    result = variance(store, "acme")  # no today override

    utc_today = datetime.now(timezone.utc).date()
    planned_finish = date.fromisoformat(result["tasks"][0]["planned_finish"])
    expected_slippage = (utc_today - planned_finish).days
    assert result["tasks"][0]["slippage_days"] == expected_slippage


# ------------------------------------------------------------------
# Characterization: overlay, dependency types, warnings, edge paths
# ------------------------------------------------------------------


def _alloc_by_task(store, scenario_id):
    return {a["task_id"]: a for a in store.list_allocations(scenario_id)}


def test_allocate_overlay_overrides_estimate_hours(store):
    scenario = store.add_scenario("acme", "Plan", "whatif")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t = store.add_task("acme", "Task", estimate_hours=8)
    store.add_scenario_change(scenario["id"], "task", t["id"], "estimate_hours", 24)

    result = allocate(store, scenario["id"], project_start=START)

    a = _alloc_by_task(store, scenario["id"])[t["id"]]
    assert a["hours"] == 24.0
    assert a["end_date"] == "2025-01-08"  # 3 days
    assert result["allocations"][0]["hours"] == 24.0


def test_allocate_overlay_none_estimate_gives_no_estimate_warning(store):
    scenario = store.add_scenario("acme", "Plan", "whatif")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Task", estimate_hours=8)
    store.add_scenario_change(scenario["id"], "task", t["id"], "estimate_hours", None)

    result = allocate(store, scenario["id"], project_start=START)

    assert result["allocations"] == []
    assert result["warnings"] == [f"task {t['id']} ('Task'): no estimate — treated as zero-duration"]


def test_allocate_overlay_priority_decides_order(store):
    scenario = store.add_scenario("acme", "Plan", "whatif")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t1 = store.add_task("acme", "A", estimate_hours=8, priority=1)
    t2 = store.add_task("acme", "B", estimate_hours=8, priority=2)
    store.add_scenario_change(scenario["id"], "task", t1["id"], "priority", 5)

    allocate(store, scenario["id"], project_start=START)

    allocs = _alloc_by_task(store, scenario["id"])
    assert allocs[t2["id"]]["start_date"] == "2025-01-06"
    assert allocs[t1["id"]]["start_date"] == "2025-01-07"


def test_allocate_overlay_required_skill(store):
    scenario = store.add_scenario("acme", "Plan", "whatif")
    skill = store.get_or_create_skill("acme", "go")
    anna = store.add_resource("acme", "Anna")
    erik = store.add_resource("acme", "Erik")
    store.set_resource_skill(erik["id"], skill["id"])
    t = store.add_task("acme", "Task", estimate_hours=8)
    store.add_scenario_change(scenario["id"], "task", t["id"], "required_skill_id", skill["id"])

    allocate(store, scenario["id"], project_start=START)

    assert _alloc_by_task(store, scenario["id"])[t["id"]]["resource_id"] == erik["id"]
    assert anna["id"] != erik["id"]


@pytest.mark.parametrize("value, deactivated", [("false", True), ("0", True), ("no", True), ("true", False), ("YES", False), ("1", False)])
def test_allocate_overlay_resource_active_parsing(store, value, deactivated):
    scenario = store.add_scenario("acme", "Plan", "whatif")
    r = store.add_resource("acme", "Anna")
    store.add_task("acme", "Task", estimate_hours=8)
    store.add_scenario_change(scenario["id"], "resource", r["id"], "active", value)

    result = allocate(store, scenario["id"], project_start=START)

    if deactivated:
        assert result["allocations"] == []
        assert any("no eligible resource" in w for w in result["warnings"])
    else:
        assert len(result["allocations"]) == 1


def test_allocate_overlay_ignores_unknown_entity_field_and_target(store):
    scenario = store.add_scenario("acme", "Plan", "whatif")
    r = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t = store.add_task("acme", "Task", estimate_hours=8)
    store.add_scenario_change(scenario["id"], "banana", t["id"], "estimate_hours", 99)
    store.add_scenario_change(scenario["id"], "task", t["id"], "title", "x")
    store.add_scenario_change(scenario["id"], "task", 9999, "estimate_hours", 99)
    store.add_scenario_change(scenario["id"], "resource", 9999, "active", "false")

    result = allocate(store, scenario["id"], project_start=START)

    assert [(a["task_id"], a["resource_id"], a["hours"]) for a in result["allocations"]] == [(t["id"], r["id"], 8.0)]
    assert result["warnings"] == []


def test_allocate_inactive_resource_is_excluded(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna")
    store.add_task("acme", "Task", estimate_hours=8)
    with store._connect() as conn:
        conn.execute("UPDATE resource SET active = 0 WHERE id = ?", (r["id"],))
        conn.commit()

    result = allocate(store, scenario["id"], project_start=START)

    assert result["allocations"] == []
    assert len(result["warnings"]) == 1


def test_allocate_picks_resource_finishing_earliest(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    slow = store.add_resource("acme", "Slow", capacity_hours_per_day=2.0)
    fast = store.add_resource("acme", "Fast", capacity_hours_per_day=8.0)
    t = store.add_task("acme", "Task", estimate_hours=8)

    allocate(store, scenario["id"], project_start=START)

    assert _alloc_by_task(store, scenario["id"])[t["id"]]["resource_id"] == fast["id"]
    assert slow["id"] != fast["id"]


def test_allocate_tie_between_resources_keeps_first(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    first = store.add_resource("acme", "A", capacity_hours_per_day=8.0)
    store.add_resource("acme", "B", capacity_hours_per_day=8.0)
    t = store.add_task("acme", "Task", estimate_hours=8)

    allocate(store, scenario["id"], project_start=START)

    assert _alloc_by_task(store, scenario["id"])[t["id"]]["resource_id"] == first["id"]


def test_allocate_zero_estimate_task_is_allocated_with_zero_duration(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Milestone", estimate_hours=0)

    result = allocate(store, scenario["id"], project_start=START)

    a = result["allocations"][0]
    assert (a["task_id"], a["start_date"], a["end_date"], a["hours"]) == (t["id"], "2025-01-06", "2025-01-06", 0.0)
    assert result["warnings"] == []


def test_allocate_no_estimate_task_delays_fs_successor(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t1 = store.add_task("acme", "Unestimated")
    t2 = store.add_task("acme", "After", estimate_hours=8)
    store.add_dependency(t1["id"], t2["id"])

    result = allocate(store, scenario["id"], project_start=START)

    assert len(result["warnings"]) == 1 and "no estimate" in result["warnings"][0]
    assert [a["task_id"] for a in result["allocations"]] == [t2["id"]]


def test_allocate_fs_successor_waits_for_resource_finish_of_predecessor(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=4.0)
    t1 = store.add_task("acme", "First", estimate_hours=16)
    t2 = store.add_task("acme", "Second", estimate_hours=4)
    store.add_dependency(t1["id"], t2["id"])

    allocate(store, scenario["id"], project_start=START)

    allocs = _alloc_by_task(store, scenario["id"])
    assert allocs[t1["id"]]["end_date"] == "2025-01-09"
    assert allocs[t2["id"]]["start_date"] == "2025-01-10"


def test_allocate_non_fs_dependency_does_not_push_ready_date(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_resource("acme", "Erik", capacity_hours_per_day=8.0)
    t1 = store.add_task("acme", "First", estimate_hours=24)
    t2 = store.add_task("acme", "Second", estimate_hours=8)
    store.add_dependency(t1["id"], t2["id"], type="SS")

    allocate(store, scenario["id"], project_start=START)

    allocs = _alloc_by_task(store, scenario["id"])
    # SS imposes no finish-to-start wait in the list-scheduling pass.
    assert allocs[t2["id"]]["start_date"] <= allocs[t1["id"]]["end_date"]


def test_allocate_dependency_with_unknown_task_is_ignored(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Task", estimate_hours=8)
    other = store.add_task("acme", "Other", estimate_hours=8)
    store.add_dependency(t["id"], other["id"])
    with store._connect() as conn:
        conn.execute("DELETE FROM task WHERE id = ?", (other["id"],))
        conn.commit()

    result = allocate(store, scenario["id"], project_start=START)

    assert [a["task_id"] for a in result["allocations"]] == [t["id"]]


def test_allocate_critical_task_goes_before_non_critical(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    head = store.add_task("acme", "Head", estimate_hours=8, priority=5)
    tail = store.add_task("acme", "Tail", estimate_hours=24, priority=5)
    store.add_dependency(head["id"], tail["id"])
    side = store.add_task("acme", "Side", estimate_hours=8, priority=1)

    allocate(store, scenario["id"], project_start=START)

    allocs = _alloc_by_task(store, scenario["id"])
    # critical chain (head) is scheduled before the higher-priority but non-critical side task
    assert allocs[head["id"]]["start_date"] < allocs[side["id"]]["start_date"]


def test_allocate_result_shape_and_stored_schedule(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Task", estimate_hours=8)

    result = allocate(store, scenario["id"], project_start=START)

    assert set(result) == {"scenario_id", "allocations", "schedule", "warnings"}
    assert result["scenario_id"] == scenario["id"]
    assert result["allocations"] == [
        {"task_id": t["id"], "resource_id": result["allocations"][0]["resource_id"],
         "start_date": "2025-01-06", "end_date": "2025-01-06", "hours": 8}
    ]
    stored = store.list_schedule(scenario["id"])
    assert stored[0]["earliest_start"] == result["schedule"][0]["earliest_start"]
    assert stored[0]["slack_days"] == result["schedule"][0]["slack_days"]


def test_allocate_cycle_is_rejected_by_cpm_before_scheduling(store):
    # compute_schedule raises first, so allocate()'s own "cycle beyond FS
    # dependencies" warning is effectively unreachable today; lock the raise.
    from memaix_gateway.pm.schedule import CyclicTaskGraphError

    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t1 = store.add_task("acme", "A", estimate_hours=8)
    t2 = store.add_task("acme", "B", estimate_hours=8)
    store.add_dependency(t1["id"], t2["id"])
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO dependency (predecessor_id, successor_id, type, lag_days) VALUES (?, ?, 'FS', 0)",
            (t2["id"], t1["id"]),
        )
        conn.commit()

    with pytest.raises(CyclicTaskGraphError, match="cannot schedule"):
        allocate(store, scenario["id"], project_start=START)
    assert store.list_allocations(scenario["id"]) == []


def test_allocate_diamond_successor_waits_for_all_predecessors(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_resource("acme", "Erik", capacity_hours_per_day=8.0)
    top = store.add_task("acme", "Top", estimate_hours=8)
    left = store.add_task("acme", "Left", estimate_hours=8)
    right = store.add_task("acme", "Right", estimate_hours=24)
    bottom = store.add_task("acme", "Bottom", estimate_hours=8)
    for p, s in ((top, left), (top, right), (left, bottom), (right, bottom)):
        store.add_dependency(p["id"], s["id"])

    allocate(store, scenario["id"], project_start=START)

    allocs = _alloc_by_task(store, scenario["id"])
    assert allocs[bottom["id"]]["start_date"] > allocs[right["id"]]["end_date"]
    assert allocs[bottom["id"]]["start_date"] > allocs[left["id"]]["end_date"]


def test_allocate_availability_partial_hours_spread_over_days(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_availability(r["id"], "2025-01-06", "2025-01-07", 2.0)
    t = store.add_task("acme", "Task", estimate_hours=8)

    allocate(store, scenario["id"], project_start=START)

    a = _alloc_by_task(store, scenario["id"])[t["id"]]
    assert (a["start_date"], a["end_date"]) == ("2025-01-06", "2025-01-08")


# ------------------------------------------------------------------
# Characterization: utilization
# ------------------------------------------------------------------


def _util_setup(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    anna = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    erik = store.add_resource("acme", "Erik", capacity_hours_per_day=4.0)
    t1 = store.add_task("acme", "T1", estimate_hours=16)
    t2 = store.add_task("acme", "T2", estimate_hours=8)
    store.add_allocation(scenario["id"], t1["id"], anna["id"], "2025-01-06", "2025-01-07", 16)
    store.add_allocation(scenario["id"], t2["id"], erik["id"], "2025-01-20", "2025-01-20", 8)
    return scenario, anna, erik


def test_utilization_filters_by_resource_id(store):
    scenario, anna, erik = _util_setup(store)

    res = utilization(store, scenario["id"], "2025-01-06", "2025-01-07", resource_id=erik["id"])

    assert [r["resource_id"] for r in res["resources"]] == [erik["id"]]
    assert res["resources"][0]["allocated_hours"] == 0.0
    assert res["resources"][0]["utilization_pct"] == 0.0
    assert res["resources"][0]["capacity_hours"] == 8.0


def test_utilization_unfiltered_lists_all_resources_and_ignores_other_allocs(store):
    scenario, anna, erik = _util_setup(store)

    res = utilization(store, scenario["id"], "2025-01-06", "2025-01-07")

    by_id = {r["resource_id"]: r for r in res["resources"]}
    assert set(by_id) == {anna["id"], erik["id"]}
    assert by_id[anna["id"]]["allocated_hours"] == 16.0
    assert by_id[anna["id"]]["utilization_pct"] == 100.0
    assert by_id[erik["id"]]["allocated_hours"] == 0.0
    assert res["period_start"] == "2025-01-06" and res["period_end"] == "2025-01-07"
    assert res["scenario_id"] == scenario["id"]
    assert by_id[anna["id"]]["name"] == "Anna"


def test_utilization_partial_overlap_prorates_hours(store):
    scenario, anna, _ = _util_setup(store)

    res = utilization(store, scenario["id"], "2025-01-07", "2025-01-09", resource_id=anna["id"])

    r = res["resources"][0]
    assert r["allocated_hours"] == 8.0  # one of two allocation days falls in the period
    assert r["capacity_hours"] == 24.0
    assert r["utilization_pct"] == 33.3


def test_utilization_allocation_before_and_after_period_is_ignored(store):
    scenario, anna, _ = _util_setup(store)

    before = utilization(store, scenario["id"], "2025-01-08", "2025-01-10", resource_id=anna["id"])
    after = utilization(store, scenario["id"], "2025-01-01", "2025-01-05", resource_id=anna["id"])

    assert before["resources"][0]["allocated_hours"] == 0.0
    assert after["resources"][0]["allocated_hours"] == 0.0


def test_utilization_zero_capacity_gives_none_pct(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_availability(r["id"], "2025-01-06", "2025-01-07", 0.0)

    res = utilization(store, scenario["id"], "2025-01-06", "2025-01-07")

    assert res["resources"][0]["capacity_hours"] == 0
    assert res["resources"][0]["utilization_pct"] is None


def test_utilization_reversed_period_has_zero_capacity(store):
    scenario, anna, _ = _util_setup(store)

    res = utilization(store, scenario["id"], "2025-01-09", "2025-01-06", resource_id=anna["id"])

    assert res["resources"][0]["capacity_hours"] == 0
    assert res["resources"][0]["utilization_pct"] is None


def test_utilization_reversed_allocation_span_uses_whole_hours_per_day(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t = store.add_task("acme", "T", estimate_hours=6)
    # end before start -> span_days <= 0 -> per_day = hours; overlap is empty so nothing counts
    store.add_allocation(scenario["id"], t["id"], r["id"], "2025-01-08", "2025-01-06", 6)

    res = utilization(store, scenario["id"], "2025-01-06", "2025-01-10")

    assert res["resources"][0]["allocated_hours"] == 0.0


def test_utilization_uses_availability_for_capacity(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_availability(r["id"], "2025-01-07", "2025-01-07", 0.0)

    res = utilization(store, scenario["id"], "2025-01-06", "2025-01-08")

    assert res["resources"][0]["capacity_hours"] == 16.0


def test_utilization_includes_inactive_resources(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna")
    with store._connect() as conn:
        conn.execute("UPDATE resource SET active = 0 WHERE id = ?", (r["id"],))
        conn.commit()

    res = utilization(store, scenario["id"], "2025-01-06", "2025-01-06")

    assert [x["resource_id"] for x in res["resources"]] == [r["id"]]
