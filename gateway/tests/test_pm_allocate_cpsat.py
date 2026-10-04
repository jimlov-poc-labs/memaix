# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for pm.allocate_cpsat — the optional OR-Tools CP-SAT allocator
(FEATURE-PM-ENGINE.md Byggordning steg 7). Same constraints as the
heuristic (capacity/skill/dependencies), solved via optimization instead
of greedy placement. Skipped entirely if ortools isn't installed."""

from __future__ import annotations

import sys
from datetime import date

import pytest

pytest.importorskip("ortools")

from memaix_gateway.pm.allocate_cpsat import allocate_cpsat
from memaix_gateway.pm.store import PMStore

START = date(2025, 1, 6)  # a Monday, matching test_pm_allocate.py's convention


@pytest.fixture()
def store(tmp_path):
    return PMStore.for_path(tmp_path / "pm.db")


def test_single_task_single_resource(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    t = store.add_task("acme", "Design", estimate_hours=16)

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert result["warnings"] == []
    allocs = store.list_allocations(scenario["id"])
    assert len(allocs) == 1
    assert allocs[0]["resource_id"] == r["id"]
    assert allocs[0]["task_id"] == t["id"]
    assert allocs[0]["start_date"] == "2025-01-06"
    assert allocs[0]["end_date"] == "2025-01-07"  # 16h at 8h/day = 2 days


def test_two_tasks_same_resource_do_not_overlap(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_task("acme", "A", estimate_hours=8)
    store.add_task("acme", "B", estimate_hours=8)

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    allocs = sorted(result["allocations"], key=lambda a: a["start_date"])
    a_start, a_end = date.fromisoformat(allocs[0]["start_date"]), date.fromisoformat(allocs[0]["end_date"])
    b_start = date.fromisoformat(allocs[1]["start_date"])
    assert b_start > a_end  # no overlap on the shared resource


def test_independent_tasks_on_separate_resources_run_in_parallel(store):
    # Proves the objective actually minimizes makespan rather than always
    # serializing: two tasks with no dependency and no resource contention
    # must both start on day 0, not one after the other.
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_resource("acme", "Bob", capacity_hours_per_day=8.0)
    store.add_task("acme", "A", estimate_hours=8)
    store.add_task("acme", "B", estimate_hours=8)

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    starts = {a["start_date"] for a in result["allocations"]}
    assert starts == {"2025-01-06"}


def test_dependency_respected(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    a = store.add_task("acme", "A", estimate_hours=8)
    b = store.add_task("acme", "B", estimate_hours=8)
    store.add_dependency(a["id"], b["id"], type="FS")

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    by_task = {alloc["task_id"]: alloc for alloc in result["allocations"]}
    assert date.fromisoformat(by_task[b["id"]]["start_date"]) > date.fromisoformat(by_task[a["id"]]["end_date"])


def test_required_skill_respected(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    python_skill = store.get_or_create_skill("acme", "python")
    anna = store.add_resource("acme", "Anna")
    bob = store.add_resource("acme", "Bob")
    store.set_resource_skill(anna["id"], python_skill["id"])
    t = store.add_task("acme", "Backend work", estimate_hours=8, required_skill_id=python_skill["id"])

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert result["allocations"][0]["resource_id"] == anna["id"]
    assert result["allocations"][0]["resource_id"] != bob["id"]


def test_no_estimate_is_zero_duration_with_warning(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "Vague task")

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert result["allocations"] == []
    assert any(str(t["id"]) in w and "no estimate" in w for w in result["warnings"])


def test_no_eligible_resource_is_unallocated_with_warning(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    skill = store.get_or_create_skill("acme", "rust")
    t = store.add_task("acme", "Needs rust", estimate_hours=8, required_skill_id=skill["id"])

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert result["allocations"] == []
    assert any(str(t["id"]) in w and "no eligible resource" in w for w in result["warnings"])


def test_schedule_matches_cpm_regardless_of_allocation(store):
    from memaix_gateway.pm.allocate import allocate as allocate_heuristic

    scenario = store.add_scenario("acme", "Plan", "baseline")
    scenario2 = store.add_scenario("acme", "Plan2", "baseline")
    store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    store.add_task("acme", "A", estimate_hours=16)

    heuristic_result = allocate_heuristic(store, scenario["id"], project_start=START)
    cpsat_result = allocate_cpsat(store, scenario2["id"], project_start=START)

    assert cpsat_result["schedule"] == heuristic_result["schedule"]


def test_no_schedulable_tasks_returns_empty_cleanly(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_task("acme", "Unestimated")

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert result["allocations"] == []
    assert len(result["schedule"]) == 1


def test_unknown_scenario_raises(store):
    with pytest.raises(ValueError):
        allocate_cpsat(store, 999)


def test_missing_ortools_raises_clear_import_error(store, monkeypatch):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    monkeypatch.setitem(sys.modules, "ortools.sat.python", None)

    with pytest.raises(ImportError, match="ortools"):
        allocate_cpsat(store, scenario["id"], project_start=START)


# --- Karakteriseringstester (Sonar S3776-refaktorering): låser beteende
# --- som de ursprungliga testerna inte täckte. Oförändrade efter uppdelningen.


def _patch_solve_status(monkeypatch, forced_status):
    """Kör den riktiga lösaren men rapportera en påtvingad status."""
    from ortools.sat.python import cp_model

    real_solve = cp_model.CpSolver.solve

    def fake_solve(self, model, *args, **kwargs):
        real_solve(self, model, *args, **kwargs)
        return forced_status

    monkeypatch.setattr(cp_model.CpSolver, "solve", fake_solve)


def test_duration_days_rounds_up_and_has_floor_of_one():
    from memaix_gateway.pm.allocate_cpsat import _duration_days

    assert _duration_days(16, 8) == 2
    assert _duration_days(17, 8) == 3
    assert _duration_days(1, 8) == 1
    assert _duration_days(0, 8) == 1  # golv på en dag


def test_duration_days_nonpositive_capacity_is_zero():
    from memaix_gateway.pm.allocate_cpsat import _duration_days

    assert _duration_days(8, 0) == 0
    assert _duration_days(8, -1) == 0


def test_faster_resource_is_chosen_to_minimize_makespan(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Slow", capacity_hours_per_day=4.0)
    fast = store.add_resource("acme", "Fast", capacity_hours_per_day=8.0)
    store.add_task("acme", "Big", estimate_hours=16)

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    a = result["allocations"][0]
    assert a["resource_id"] == fast["id"]
    assert (a["start_date"], a["end_date"], a["hours"]) == ("2025-01-06", "2025-01-07", 16)


def test_inactive_resource_is_ignored(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Gone", active=False)
    t = store.add_task("acme", "Orphan", estimate_hours=8)

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert result["allocations"] == []
    assert len(result["warnings"]) == 1
    assert str(t["id"]) in result["warnings"][0] and "no eligible resource" in result["warnings"][0]


def test_scenario_overlay_changes_estimate_and_deactivates_resource(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    anna = store.add_resource("acme", "Anna", capacity_hours_per_day=8.0)
    bob = store.add_resource("acme", "Bob", capacity_hours_per_day=8.0)
    t = store.add_task("acme", "Work", estimate_hours=8)
    store.add_scenario_change(scenario["id"], "task", t["id"], "estimate_hours", 24)
    store.add_scenario_change(scenario["id"], "resource", anna["id"], "active", "false")

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    a = result["allocations"][0]
    assert a["resource_id"] == bob["id"]
    assert a["hours"] == 24
    assert a["end_date"] == "2025-01-08"  # 24h / 8h = 3 dagar


def test_non_fs_dependency_adds_no_precedence_constraint(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    store.add_resource("acme", "Bob")
    a = store.add_task("acme", "A", estimate_hours=8)
    b = store.add_task("acme", "B", estimate_hours=8)
    store.add_dependency(a["id"], b["id"], type="SS")

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert {x["start_date"] for x in result["allocations"]} == {"2025-01-06"}


def test_fs_dependency_on_unschedulable_predecessor_is_skipped(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    pred = store.add_task("acme", "No estimate")
    succ = store.add_task("acme", "B", estimate_hours=8)
    store.add_dependency(pred["id"], succ["id"], type="FS")

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert [x["task_id"] for x in result["allocations"]] == [succ["id"]]
    assert len(result["warnings"]) == 1 and "no estimate" in result["warnings"][0]


def test_rerun_replaces_previous_allocation_and_persists_schedule(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    t = store.add_task("acme", "A", estimate_hours=8)

    first = allocate_cpsat(store, scenario["id"], project_start=START)
    second = allocate_cpsat(store, scenario["id"], project_start=START)

    assert first["allocations"] == second["allocations"]
    assert len(store.list_allocations(scenario["id"])) == 1
    sched = store.list_schedule(scenario["id"])
    assert len(sched) == 1 and sched[0]["task_id"] == t["id"]
    assert sched[0]["earliest_start"] == second["schedule"][0]["earliest_start"]


def test_empty_path_persists_schedule_and_clears_old_allocation(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    r = store.add_resource("acme", "Anna")
    t = store.add_task("acme", "A", estimate_hours=8)
    allocate_cpsat(store, scenario["id"], project_start=START)
    assert len(store.list_allocations(scenario["id"])) == 1
    store.add_scenario_change(scenario["id"], "task", t["id"], "estimate_hours", None)

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    assert result["allocations"] == []
    assert store.list_allocations(scenario["id"]) == []
    assert len(store.list_schedule(scenario["id"])) == 1
    assert r["id"]  # resursen finns kvar, uppgiften saknar bara estimat


def test_project_start_defaults_to_today(store):
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    store.add_task("acme", "A", estimate_hours=8)

    result = allocate_cpsat(store, scenario["id"])

    assert result["allocations"][0]["start_date"] == date.today().isoformat()


def test_feasible_but_not_optimal_adds_time_limit_warning(store, monkeypatch):
    from ortools.sat.python import cp_model

    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    store.add_task("acme", "A", estimate_hours=8)
    _patch_solve_status(monkeypatch, cp_model.FEASIBLE)

    result = allocate_cpsat(store, scenario["id"], project_start=START, time_limit_seconds=3.0)

    assert result["warnings"] == [
        "CP-SAT hit its 3.0s time limit — this allocation is feasible but not proven optimal"
    ]
    assert len(result["allocations"]) == 1


def test_infeasible_raises_runtime_error_and_leaves_store_untouched(store, monkeypatch):
    from ortools.sat.python import cp_model

    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    store.add_task("acme", "A", estimate_hours=8)
    _patch_solve_status(monkeypatch, cp_model.INFEASIBLE)

    with pytest.raises(RuntimeError, match="no feasible allocation for scenario"):
        allocate_cpsat(store, scenario["id"], project_start=START)

    assert store.list_allocations(scenario["id"]) == []
    assert store.list_schedule(scenario["id"]) == []


def test_fs_dependency_leaves_one_gap_day_after_predecessor(store):
    # Låser dagens beteende: end-variabeln är exklusiv och precedensen är
    # start[succ] >= end[pred] + 1, så en lucka på en dag uppstår (A: 6/1,
    # B: tidigast 8/1).
    scenario = store.add_scenario("acme", "Plan", "baseline")
    store.add_resource("acme", "Anna")
    a = store.add_task("acme", "A", estimate_hours=8)
    b = store.add_task("acme", "B", estimate_hours=8)
    store.add_dependency(a["id"], b["id"], type="FS")

    result = allocate_cpsat(store, scenario["id"], project_start=START)

    by_task = {x["task_id"]: x for x in result["allocations"]}
    assert (by_task[a["id"]]["start_date"], by_task[a["id"]]["end_date"]) == ("2025-01-06", "2025-01-06")
    assert by_task[b["id"]]["start_date"] == "2025-01-08"


# --- Rena hjälpfunktioner (ingen lösare behövs för dessa) ---


def test_horizon_days_sums_slowest_durations_plus_floor_and_margin():
    from memaix_gateway.pm.allocate_cpsat import _horizon_days

    cpm_rows = [{"earliest_start": "2025-01-06"}, {"earliest_start": "2025-01-09"}]
    schedulable = [
        {"estimate": 16, "eligible": [{"capacity_hours_per_day": 8.0}, {"capacity_hours_per_day": 4.0}]},  # 4 d
        {"estimate": 8, "eligible": [{"capacity_hours_per_day": 8.0}]},  # 1 d
    ]
    assert _horizon_days(cpm_rows, START, schedulable) == 3 + 4 + 1 + 30


class _FakeSolver:
    def __init__(self, values):
        self._values = values

    def value(self, var):
        return self._values[var]


def test_extract_allocations_zero_duration_never_ends_before_start():
    from memaix_gateway.pm.allocate_cpsat import _extract_allocations

    schedulable = [{"task": {"id": 7}, "estimate": 3.5, "eligible": []}]
    solver = _FakeSolver({"s": 2, "e": 2, "a1": 0, "a2": 1})  # end == start (varaktighet 0)
    out = _extract_allocations(solver, schedulable, {7: "s"}, {7: "e"}, {7: {10: "a1", 11: "a2"}}, START)
    assert out == [{
        "task_id": 7, "resource_id": 11, "start_date": "2025-01-08", "end_date": "2025-01-08", "hours": 3.5,
    }]


def test_extract_allocations_end_is_last_day_inclusive():
    from memaix_gateway.pm.allocate_cpsat import _extract_allocations

    schedulable = [{"task": {"id": 1}, "estimate": 16, "eligible": []}]
    solver = _FakeSolver({"s": 0, "e": 2, "a": 1})
    out = _extract_allocations(solver, schedulable, {1: "s"}, {1: "e"}, {1: {5: "a"}}, START)
    assert (out[0]["start_date"], out[0]["end_date"]) == ("2025-01-06", "2025-01-07")


def test_classify_task_outcomes():
    from memaix_gateway.pm.allocate_cpsat import _classify_task

    res = [{"id": 1}, {"id": 2}]
    skills = {1: [9], 2: []}
    task = {"id": 5, "title": "T", "estimate_hours": 8, "required_skill_id": None}
    entry = _classify_task(task, res, skills)
    assert entry["eligible"] == res and entry["estimate"] == 8
    entry = _classify_task({**task, "required_skill_id": 9}, res, skills)
    assert entry["eligible"] == [res[0]]
    warning = _classify_task({**task, "required_skill_id": 3}, res, skills)
    assert warning == "task 5 ('T'): no eligible resource for required skill — unallocated"
    warning = _classify_task({**task, "estimate_hours": None}, res, skills)
    assert warning == "task 5 ('T'): no estimate — treated as zero-duration"
