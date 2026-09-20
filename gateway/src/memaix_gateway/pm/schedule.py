# SPDX-License-Identifier: AGPL-3.0-or-later
"""Critical-path scheduling — pure, deterministic graph math
(FEATURE-PM-ENGINE.md §4, PM-PLANNING-ENGINE.md "Motorvalet").

Resource-agnostic: this computes the theoretical earliest/latest dates and
slack assuming unlimited resources, exactly what "critical path method"
means classically. Resource contention (a real person can only work one
task at a time) is layered on top by allocate.py — that's why allocate.py
runs this first and then uses its `earliest_start` as a floor, not a
guarantee.

v1 scope: every day is a working day (no weekend/holiday calendar) — a
documented simplification, not a silent one. Dependency types FS/SS/FF/SF
are all supported with `lag_days`.
"""

from __future__ import annotations

from datetime import date, timedelta


class CyclicTaskGraphError(ValueError):
    pass


def _topological_order(task_ids: list[int], deps: list[dict]) -> list[int]:
    """Kahn's algorithm. Raises CyclicTaskGraphError if a cycle exists."""
    indegree = dict.fromkeys(task_ids, 0)
    successors: dict[int, list[int]] = {t: [] for t in task_ids}
    for d in deps:
        if d["predecessor_id"] not in indegree or d["successor_id"] not in indegree:
            continue  # dependency referencing a task outside this task set
        successors[d["predecessor_id"]].append(d["successor_id"])
        indegree[d["successor_id"]] += 1

    ready = sorted(t for t, deg in indegree.items() if deg == 0)
    order: list[int] = []
    while ready:
        ready.sort()
        node = ready.pop(0)
        order.append(node)
        for nxt in successors[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)

    if len(order) != len(task_ids):
        raise CyclicTaskGraphError("task graph contains a cycle — cannot schedule")
    return order


def _duration_days(estimate_hours: float | None, hours_per_day: float) -> float:
    if not estimate_hours or estimate_hours <= 0:
        return 0.0
    return estimate_hours / hours_per_day


def compute_schedule(
    tasks: list[dict], dependencies: list[dict], *, project_start: date, hours_per_day: float = 8.0,
) -> list[dict]:
    """Forward/backward pass critical-path method.

    tasks: [{"id": int, "estimate_hours": float | None}, ...]
    dependencies: [{"predecessor_id", "successor_id", "type": "FS"|"SS"|"FF"|"SF", "lag_days"}]

    Returns one row per task: {task_id, earliest_start, earliest_finish,
    latest_start, latest_finish, slack_days, is_critical} with dates as
    ISO 8601 strings relative to `project_start`.
    """
    task_ids = [t["id"] for t in tasks]
    order = _topological_order(task_ids, dependencies)
    duration = {t["id"]: _duration_days(t.get("estimate_hours"), hours_per_day) for t in tasks}

    predecessors: dict[int, list[dict]] = {t: [] for t in task_ids}
    successors: dict[int, list[dict]] = {t: [] for t in task_ids}
    for d in dependencies:
        if d["predecessor_id"] not in duration or d["successor_id"] not in duration:
            continue
        predecessors[d["successor_id"]].append(d)
        successors[d["predecessor_id"]].append(d)

    earliest_start: dict[int, float] = {}
    earliest_finish: dict[int, float] = {}
    for task_id in order:
        floor = 0.0
        for dep in predecessors[task_id]:
            pred = dep["predecessor_id"]
            floor = max(floor, _successor_floor(dep, earliest_start[pred], earliest_finish[pred], duration[task_id]))
        earliest_start[task_id] = floor
        earliest_finish[task_id] = floor + duration[task_id]

    project_duration = max(earliest_finish.values(), default=0.0)

    latest_finish: dict[int, float] = {}
    latest_start: dict[int, float] = {}
    for task_id in reversed(order):
        ceiling = project_duration
        for dep in successors[task_id]:
            succ = dep["successor_id"]
            ceiling = min(ceiling, _predecessor_ceiling(dep, latest_start[succ], latest_finish[succ], duration[task_id]))
        latest_finish[task_id] = ceiling
        latest_start[task_id] = ceiling - duration[task_id]

    rows = []
    for task_id in task_ids:
        slack = latest_start[task_id] - earliest_start[task_id]
        rows.append(
            {
                "task_id": task_id,
                "earliest_start": _to_iso(project_start, earliest_start[task_id]),
                "earliest_finish": _to_iso(project_start, earliest_finish[task_id]),
                "latest_start": _to_iso(project_start, latest_start[task_id]),
                "latest_finish": _to_iso(project_start, latest_finish[task_id]),
                "slack_days": round(slack, 6),
                "is_critical": slack <= 1e-9,
            }
        )
    return rows


def _to_iso(project_start: date, offset_days: float) -> str:
    return (project_start + timedelta(days=round(offset_days))).isoformat()


def _successor_floor(dep: dict, pred_start: float, pred_finish: float, succ_duration: float) -> float:
    """Earliest the successor may start, given this one predecessor edge."""
    lag = dep.get("lag_days", 0.0)
    dtype = dep.get("type", "FS")
    if dtype == "FS":
        return pred_finish + lag
    if dtype == "SS":
        return pred_start + lag
    if dtype == "FF":
        return pred_finish + lag - succ_duration
    if dtype == "SF":
        return pred_start + lag - succ_duration
    return pred_finish + lag  # unknown type — fall back to FS


def _predecessor_ceiling(dep: dict, succ_latest_start: float, succ_latest_finish: float, pred_duration: float) -> float:
    """Latest the predecessor may finish, given this one successor edge."""
    lag = dep.get("lag_days", 0.0)
    dtype = dep.get("type", "FS")
    if dtype == "FS":
        return succ_latest_start - lag
    if dtype == "SS":
        return succ_latest_start - lag + pred_duration
    if dtype == "FF":
        return succ_latest_finish - lag
    if dtype == "SF":
        return succ_latest_finish - lag + pred_duration
    return succ_latest_start - lag  # unknown type — fall back to FS
