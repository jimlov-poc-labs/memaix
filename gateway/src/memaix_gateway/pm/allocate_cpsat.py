# SPDX-License-Identifier: AGPL-3.0-or-later
"""CP-SAT resource-constrained allocation — optional alternative to
allocate.py's greedy heuristic (FEATURE-PM-ENGINE.md §4/Byggordning steg 7).

Same division of labor as allocate.py: schedule.compute_schedule() still
supplies the resource-agnostic critical path (dependency floors, slack,
is_critical) unchanged — this module only replaces the second pass, WHO
gets assigned to WHAT and WHEN, by solving it as an optimization problem
(OR-Tools CP-SAT) instead of a priority-ordered greedy placement. The
`schedule` table this writes is byte-identical to what allocate() would
write for the same tasks/deps/project_start; only `allocation` differs.

v1 scope, narrower than the heuristic's, stated rather than hidden:
  - Whole-day, resource-exclusive occupancy: a resource works on at most
    one task per day (NoOverlap intervals). The heuristic lets several
    small tasks share a resource's leftover hours on the same day; CP-SAT
    v1 doesn't, because modeling fractional-capacity sharing as a
    cumulative constraint is real added complexity for a v1 optimizer this
    codebase doesn't need yet — see "Framtida arbete" in FEATURE-PM-ENGINE.md
    for resource leveling / cost minimization, which would need it anyway.
  - Availability exceptions (resource.py's per-date hour overrides) are
    NOT modeled — every day is a resource's flat capacity_hours_per_day,
    same simplification schedule.py already makes for the base CPM.
  - Objective is pure makespan minimization (finish the last task ASAP).
    Cost minimization and resource leveling are future work, not v1.
  - FS dependencies get an exact `start[succ] >= end[pred] + 1` constraint
    (mirroring allocate.py's own FS-only special-case); SS/FF/SF are
    already captured by compute_schedule()'s earliest_start floor, which
    every task's start is bounded below by regardless of type.

Requires the optional `ortools` dependency (`pip install memaix-gateway[pm]`).
Raises ImportError with a clear message if it isn't installed — callers
decide whether to fall back to the heuristic (see tools/pm_engine.py).
"""

from __future__ import annotations

from datetime import date, timedelta

from .schedule import compute_schedule


_EARLIEST_START = "earliest_start"


def _duration_days(estimate_hours: float, capacity_hours_per_day: float) -> int:
    import math

    if capacity_hours_per_day <= 0:
        return 0
    return max(1, math.ceil(estimate_hours / capacity_hours_per_day))


def _import_cp_model():
    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:
        raise ImportError(
            "allocate_cpsat requires the optional 'ortools' dependency "
            "(pip install 'memaix-gateway[pm]') — or use allocate.allocate() instead."
        ) from exc
    return cp_model


def _load_inputs(store, scenario_id: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Scenario + shared overlay -> (tasks, deps, active resources)."""
    scenario = store.get_scenario(scenario_id)
    if scenario is None:
        raise ValueError(f"no such scenario: {scenario_id}")
    project = scenario["project"]

    from .allocate import _apply_overlay  # shared scenario_change overlay logic

    tasks = store.list_tasks(project)
    deps = store.list_dependencies(project)
    resources = store.list_resources(project, active_only=False)
    changes = store.list_scenario_changes(scenario_id)
    tasks, resources = _apply_overlay(tasks, resources, changes)
    return tasks, deps, [r for r in resources if r["active"]]


def _classify_task(task: dict, resources: list[dict], skill_ids_by_resource: dict[int, list]) -> tuple[dict | None, str | None]:
    """Return (schedulable entry, None) or (None, warning)."""
    estimate = task.get("estimate_hours")
    if estimate is None:
        return None, f"task {task['id']} ({task['title']!r}): no estimate — treated as zero-duration"
    required_skill = task.get("required_skill_id")
    eligible = [r for r in resources if required_skill is None or required_skill in skill_ids_by_resource[r["id"]]]
    if not eligible:
        return None, f"task {task['id']} ({task['title']!r}): no eligible resource for required skill — unallocated"
    return {"task": task, "estimate": estimate, "eligible": eligible}, None


def _split_schedulable(
    tasks: list[dict], resources: list[dict], skill_ids_by_resource: dict[int, list],
) -> tuple[list[dict], list[str]]:
    """Tasks with an estimate and >=1 eligible resource, plus warnings for the rest."""
    schedulable: list[dict] = []
    warnings: list[str] = []
    for t in tasks:
        entry, warning = _classify_task(t, resources, skill_ids_by_resource)
        if entry is not None:
            schedulable.append(entry)
        else:
            warnings.append(warning)
    return schedulable, warnings


def _persist(store, scenario_id: int, allocations: list[dict], cpm_rows: list[dict]) -> None:
    store.clear_allocation(scenario_id)
    store.clear_schedule(scenario_id)
    for a in allocations:
        store.add_allocation(scenario_id, a["task_id"], a["resource_id"], a["start_date"], a["end_date"], a["hours"])
    for row in cpm_rows:
        store.set_schedule_row(
            scenario_id, row["task_id"], earliest_start=row[_EARLIEST_START], earliest_finish=row["earliest_finish"],
            latest_start=row["latest_start"], latest_finish=row["latest_finish"],
            slack_days=row["slack_days"], is_critical=row["is_critical"],
        )


def _horizon_days(cpm_rows: list[dict], project_start: date, schedulable: list[dict]) -> int:
    """Safe UPPER bound for every start/end IntVar's domain — the true worst
    case if every schedulable task were serialized on its slowest eligible
    resource (min capacity_hours_per_day -> longest duration), starting from
    the latest CPM floor. Must over-, not under-, estimate: this only bounds
    solver domain size, but too small a horizon would make a real feasible
    solution unrepresentable."""
    horizon = max((date.fromisoformat(r[_EARLIEST_START]) - project_start).days for r in cpm_rows) or 0
    horizon += sum(
        _duration_days(s["estimate"], min(r["capacity_hours_per_day"] for r in s["eligible"]))
        for s in schedulable
    )
    return horizon + 30


def _add_task_variables(model, schedulable: list[dict], floors: dict[int, int], horizon: int) -> tuple[dict, dict, dict, dict]:
    """Per task: start/end IntVars, one optional interval per eligible
    resource, exactly-one assignment and end == start + chosen duration.
    Returns (starts, ends, assign, intervals_by_resource)."""
    starts: dict[int, object] = {}
    ends: dict[int, object] = {}
    assign: dict[int, dict[int, object]] = {}
    intervals_by_resource: dict[int, list] = {}
    for s in schedulable:
        tid = s["task"]["id"]
        starts[tid] = model.new_int_var(floors[tid], horizon, f"start_{tid}")
        ends[tid] = model.new_int_var(floors[tid], horizon, f"end_{tid}")
        assign[tid] = {}
        chosen_duration = []
        for r in s["eligible"]:
            rid = r["id"]
            dur = _duration_days(s["estimate"], r["capacity_hours_per_day"])
            chosen = model.new_bool_var(f"assign_{tid}_{rid}")
            assign[tid][rid] = chosen
            chosen_duration.append(dur * chosen)
            interval = model.new_optional_interval_var(starts[tid], dur, ends[tid], chosen, f"iv_{tid}_{rid}")
            intervals_by_resource.setdefault(rid, []).append(interval)
        model.add_exactly_one(list(assign[tid].values()))
        model.add(ends[tid] == starts[tid] + sum(chosen_duration))
    return starts, ends, assign, intervals_by_resource


def _add_precedence(model, deps: list[dict], starts: dict, ends: dict) -> None:
    """FS dependencies: exact precedence beyond compute_schedule()'s floor,
    since resource contention can push a predecessor's *actual* finish
    later than its CPM earliest_finish (mirrors allocate.py's own
    fs_predecessors bump)."""
    for d in deps:
        if d.get("type", "FS") != "FS":
            continue
        pred, succ = d["predecessor_id"], d["successor_id"]
        if pred in ends and succ in starts:
            model.add(starts[succ] >= ends[pred] + 1)


def _build_model(cp_model, schedulable: list[dict], deps: list[dict], floors: dict[int, int], horizon: int):
    model = cp_model.CpModel()
    starts, ends, assign, intervals_by_resource = _add_task_variables(model, schedulable, floors, horizon)
    for intervals in intervals_by_resource.values():
        if len(intervals) > 1:
            model.add_no_overlap(intervals)
    _add_precedence(model, deps, starts, ends)
    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, list(ends.values()))
    model.minimize(makespan)
    return model, starts, ends, assign


def _solve(cp_model, model, scenario_id: int, time_limit_seconds: float, warnings: list[str]):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            f"CP-SAT found no feasible allocation for scenario {scenario_id} "
            f"(solver status: {solver.status_name(status)})"
        )
    if status == cp_model.FEASIBLE:
        warnings.append(
            f"CP-SAT hit its {time_limit_seconds}s time limit — this allocation is feasible but not proven optimal"
        )
    return solver


def _extract_allocations(solver, schedulable: list[dict], starts: dict, ends: dict, assign: dict, project_start: date) -> list[dict]:
    allocations: list[dict] = []
    for s in schedulable:
        tid = s["task"]["id"]
        chosen_rid = next(rid for rid, v in assign[tid].items() if solver.value(v))
        start_day = solver.value(starts[tid])
        end_day = solver.value(ends[tid]) - 1  # end_var is exclusive-of-last-day (start + duration)
        start_d = project_start + timedelta(days=start_day)
        end_d = project_start + timedelta(days=max(end_day, start_day))
        allocations.append({
            "task_id": tid, "resource_id": chosen_rid,
            "start_date": start_d.isoformat(), "end_date": end_d.isoformat(), "hours": s["estimate"],
        })
    return allocations


def allocate_cpsat(store, scenario_id: int, *, project_start: date | None = None, time_limit_seconds: float = 10.0) -> dict:
    """Same signature/return shape as allocate.allocate() — a drop-in
    alternative selected by tools/pm_engine.py's allocator config."""
    cp_model = _import_cp_model()
    tasks, deps, resources = _load_inputs(store, scenario_id)
    project_start = project_start or date.today()

    cpm_rows = compute_schedule(tasks, deps, project_start=project_start)
    skill_ids_by_resource = {r["id"]: store.list_resource_skill_ids(r["id"]) for r in resources}
    schedulable, warnings = _split_schedulable(tasks, resources, skill_ids_by_resource)

    if not schedulable:
        _persist(store, scenario_id, [], cpm_rows)
        return {"scenario_id": scenario_id, "allocations": [], "schedule": cpm_rows, "warnings": warnings}

    floors = {r["task_id"]: (date.fromisoformat(r[_EARLIEST_START]) - project_start).days for r in cpm_rows}
    horizon = _horizon_days(cpm_rows, project_start, schedulable)
    model, starts, ends, assign = _build_model(cp_model, schedulable, deps, floors, horizon)
    solver = _solve(cp_model, model, scenario_id, time_limit_seconds, warnings)
    allocations = _extract_allocations(solver, schedulable, starts, ends, assign, project_start)
    _persist(store, scenario_id, allocations, cpm_rows)
    return {"scenario_id": scenario_id, "allocations": allocations, "schedule": cpm_rows, "warnings": warnings}
