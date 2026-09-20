# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resource-constrained allocation — v1 priority list-scheduling heuristic
(FEATURE-PM-ENGINE.md §4). CP-SAT is documented future work, not built here.

Two passes, deliberately:
  1. schedule.compute_schedule() — the resource-agnostic critical path. Its
     `earliest_start` is a *floor*: a task can never start before the graph
     says it's ready, no matter how many people you throw at it.
  2. A greedy list-scheduling pass that actually assigns people to tasks,
     respecting required skill, daily capacity, and availability exceptions
     — this is where resource contention can push a task later than the
     floor from step 1 (two tasks needing the same person can't both start
     on day 0 even if the graph says they could).

v1 scope, stated rather than hidden: every day is a working day (no
weekend/holiday calendar, same simplification as schedule.py); one resource
per task (no split allocations); `scenario_change` overlay only supports
overriding `task.estimate_hours`/`task.priority`/`task.required_skill_id`
and `resource.active` — enough for the whatif use cases in
FEATURE-PM-ENGINE.md's examples, not a fully generic field patcher.
"""

from __future__ import annotations

import heapq
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .schedule import compute_schedule

_OVERLAY_PARSERS: dict[str, Callable[[Any], Any]] = {
    "estimate_hours": lambda v: float(v) if v is not None else None,
    "priority": lambda v: int(v),
    "required_skill_id": lambda v: int(v) if v is not None else None,
    "active": lambda v: str(v).lower() in ("1", "true", "yes"),
}


def _apply_overlay(tasks: list[dict], resources: list[dict], changes: list[dict]) -> tuple[list[dict], list[dict]]:
    tasks_by_id = {t["id"]: dict(t) for t in tasks}
    resources_by_id = {r["id"]: dict(r) for r in resources}
    for change in changes:
        if change["entity"] == "task":
            target = tasks_by_id.get(change["entity_id"])
        elif change["entity"] == "resource":
            target = resources_by_id.get(change["entity_id"])
        else:
            target = None
        parser = _OVERLAY_PARSERS.get(change["field"])
        if target is None or parser is None:
            continue
        target[change["field"]] = parser(change["value"])
    return list(tasks_by_id.values()), list(resources_by_id.values())


def daily_capacity(resource: dict, day: date, availability: list[dict]) -> float:
    for a in availability:
        start = date.fromisoformat(a["start_date"])
        end = date.fromisoformat(a["end_date"])
        if start <= day <= end:
            return a["hours_per_day"]
    return resource["capacity_hours_per_day"]


def _simulate_placement(
    resource: dict, ready_date: date, hours_needed: float, availability: list[dict], ledger: dict,
) -> tuple[date, date]:
    """Walk forward day by day from ready_date, consuming this resource's
    free capacity, until hours_needed is met. Returns (start_date, end_date)."""
    if hours_needed <= 0:
        return ready_date, ready_date
    day = ready_date
    remaining = hours_needed
    start_date = None
    while remaining > 1e-9:
        capacity = daily_capacity(resource, day, availability)
        used = ledger.get((resource["id"], day), 0.0)
        free = max(capacity - used, 0.0)
        if free > 1e-9:
            if start_date is None:
                start_date = day
            take = min(free, remaining)
            ledger[(resource["id"], day)] = used + take
            remaining -= take
        day = day + timedelta(days=1)
    return start_date or ready_date, day - timedelta(days=1)


def _best_resource(eligible, ready_date, estimate, availability_by_resource, ledger):
    """Return (resource, end_date, start_date, updated_ledger) for the resource
    that finishes the task earliest. eligible is guaranteed non-empty."""
    best = None
    for r in eligible:
        trial_ledger = dict(ledger)
        start, end = _simulate_placement(r, ready_date, estimate, availability_by_resource[r["id"]], trial_ledger)
        if best is None or end < best[1]:
            best = (r, end, start, trial_ledger)
    assert best is not None  # noqa: S101
    return best


def allocate(store, scenario_id: int, *, project_start: date | None = None) -> dict:
    """Recompute a scenario's plan from scratch: critical path + resource
    assignment. Idempotent — replaces any existing allocation/schedule rows
    for this scenario. Returns {scenario_id, allocations, schedule, warnings}."""
    scenario = store.get_scenario(scenario_id)
    if scenario is None:
        raise ValueError(f"no such scenario: {scenario_id}")
    project = scenario["project"]
    # UTC, not server-local date.today() (OPEN-GAPS.md #16) — the server's
    # own timezone is an arbitrary deployment detail, unrelated to any
    # project/user. Callers who need a specific calendar day (e.g. "today"
    # in a project's or user's own timezone) pass project_start explicitly.
    project_start = project_start or datetime.now(timezone.utc).date()

    tasks = store.list_tasks(project)
    deps = store.list_dependencies(project)
    resources = store.list_resources(project, active_only=False)
    changes = store.list_scenario_changes(scenario_id)
    tasks, resources = _apply_overlay(tasks, resources, changes)
    resources = [r for r in resources if r["active"]]

    cpm_rows = compute_schedule(tasks, deps, project_start=project_start)
    cpm_by_task = {r["task_id"]: r for r in cpm_rows}

    skill_ids_by_resource = {r["id"]: store.list_resource_skill_ids(r["id"]) for r in resources}
    availability_by_resource = {r["id"]: store.list_availability(r["id"]) for r in resources}

    # List-scheduling: process tasks in dependency order, breaking ties by
    # (critical first, then lower priority number = higher priority).
    task_by_id = {t["id"]: t for t in tasks}
    successors: dict[int, list[int]] = {t["id"]: [] for t in tasks}
    fs_predecessors: dict[int, list[int]] = {t["id"]: [] for t in tasks}
    indegree = {t["id"]: 0 for t in tasks}
    for d in deps:
        if d["predecessor_id"] not in task_by_id or d["successor_id"] not in task_by_id:
            continue
        successors[d["predecessor_id"]].append(d["successor_id"])
        indegree[d["successor_id"]] += 1
        if d.get("type", "FS") == "FS":
            fs_predecessors[d["successor_id"]].append(d["predecessor_id"])

    def sort_key(task_id: int) -> tuple:
        cpm = cpm_by_task[task_id]
        return (0 if cpm["is_critical"] else 1, task_by_id[task_id].get("priority", 3), task_id)

    ready = [t for t, deg in indegree.items() if deg == 0]
    heap = [(sort_key(t), t) for t in ready]
    heapq.heapify(heap)

    warnings: list[str] = []
    ledger: dict[tuple[int, date], float] = {}
    finish_date: dict[int, date] = {}
    allocations: list[dict] = []
    processed = 0

    while heap:
        _, task_id = heapq.heappop(heap)
        processed += 1
        task = task_by_id[task_id]
        cpm = cpm_by_task[task_id]
        ready_date = date.fromisoformat(cpm["earliest_start"])
        for pred in fs_predecessors[task_id]:
            if pred in finish_date:
                ready_date = max(ready_date, finish_date[pred] + timedelta(days=1))

        estimate = task.get("estimate_hours")
        if estimate is None:
            warnings.append(f"task {task_id} ({task['title']!r}): no estimate — treated as zero-duration")
            finish_date[task_id] = ready_date
        else:
            required_skill = task.get("required_skill_id")
            eligible = [
                r for r in resources
                if required_skill is None or required_skill in skill_ids_by_resource[r["id"]]
            ]
            if not eligible:
                warnings.append(f"task {task_id} ({task['title']!r}): no eligible resource for required skill — unallocated")
                finish_date[task_id] = ready_date
            else:
                r, end, start, trial_ledger = _best_resource(eligible, ready_date, estimate, availability_by_resource, ledger)
                ledger.update(trial_ledger)
                finish_date[task_id] = end
                allocations.append(
                    {"task_id": task_id, "resource_id": r["id"], "start_date": start.isoformat(),
                     "end_date": end.isoformat(), "hours": estimate}
                )

        for succ in successors[task_id]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                heapq.heappush(heap, (sort_key(succ), succ))

    if processed != len(tasks):
        warnings.append("task graph contains a cycle beyond FS dependencies — some tasks were not scheduled")

    store.clear_allocation(scenario_id)
    store.clear_schedule(scenario_id)
    for a in allocations:
        store.add_allocation(scenario_id, a["task_id"], a["resource_id"], a["start_date"], a["end_date"], a["hours"])
    for row in cpm_rows:
        store.set_schedule_row(
            scenario_id, row["task_id"], earliest_start=row["earliest_start"], earliest_finish=row["earliest_finish"],
            latest_start=row["latest_start"], latest_finish=row["latest_finish"],
            slack_days=row["slack_days"], is_critical=row["is_critical"],
        )

    return {"scenario_id": scenario_id, "allocations": allocations, "schedule": cpm_rows, "warnings": warnings}
