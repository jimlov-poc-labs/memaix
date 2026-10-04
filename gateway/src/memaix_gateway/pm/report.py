# SPDX-License-Identifier: AGPL-3.0-or-later
"""Follow-up reporting: utilization (allocation vs capacity) and variance
(baseline vs actuals) — FEATURE-PM-ENGINE.md §4. Pure aggregation over
PMStore data; no scheduling logic lives here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .allocate import daily_capacity


def _daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _allocated_hours_in_period(resource_id: int, allocations: list[dict], p_start: date, p_end: date) -> float:
    allocated_hours = 0.0
    for a in allocations:
        if a["resource_id"] != resource_id:
            continue
        a_start, a_end = date.fromisoformat(a["start_date"]), date.fromisoformat(a["end_date"])
        span_days = (a_end - a_start).days + 1
        per_day = a["hours"] / span_days if span_days > 0 else a["hours"]
        overlap_start, overlap_end = max(a_start, p_start), min(a_end, p_end)
        if overlap_start <= overlap_end:
            allocated_hours += per_day * ((overlap_end - overlap_start).days + 1)
    return allocated_hours


def _utilization_row(store, r: dict, allocations: list[dict], p_start: date, p_end: date) -> dict:
    availability = store.list_availability(r["id"])
    capacity_hours = sum(daily_capacity(r, d, availability) for d in _daterange(p_start, p_end))
    allocated_hours = _allocated_hours_in_period(r["id"], allocations, p_start, p_end)
    pct = round(100 * allocated_hours / capacity_hours, 1) if capacity_hours > 0 else None
    return {
        "resource_id": r["id"], "name": r["name"],
        "capacity_hours": round(capacity_hours, 2),
        "allocated_hours": round(allocated_hours, 2),
        "utilization_pct": pct,
    }


def utilization(store, scenario_id: int, period_start: str, period_end: str, *, resource_id: int | None = None) -> dict:
    """Allocated hours vs capacity per resource over [period_start, period_end].

    An allocation's hours are assumed evenly spread across its own
    start_date..end_date span (the schema stores one aggregate row per
    task+resource, not per-day) — a documented approximation, not exact
    day-by-day truth.
    """
    scenario = store.get_scenario(scenario_id)
    if scenario is None:
        raise ValueError(f"no such scenario: {scenario_id}")
    p_start, p_end = date.fromisoformat(period_start), date.fromisoformat(period_end)

    resources = store.list_resources(scenario["project"], active_only=False)
    if resource_id is not None:
        resources = [r for r in resources if r["id"] == resource_id]
    allocations = store.list_allocations(scenario_id)

    results = [_utilization_row(store, r, allocations, p_start, p_end) for r in resources]
    return {"scenario_id": scenario_id, "period_start": period_start, "period_end": period_end, "resources": results}


def variance(store, project: str, *, today: date | None = None) -> dict:
    """Baseline schedule vs logged actuals — hours variance and schedule slippage."""
    baseline = store.latest_scenario(project, kind="baseline")
    if baseline is None:
        return {"ok": False, "error": "no baseline scenario yet — run allocate() then plan_commit() first"}

    # UTC, not server-local date.today() (OPEN-GAPS.md #16) — see the same
    # rationale in pm/allocate.py's project_start default.
    today = today or datetime.now(timezone.utc).date()
    schedule_rows = store.list_schedule(baseline["id"])
    tasks_by_id = {t["id"]: t for t in store.list_tasks(project)}

    results = []
    for sched in schedule_rows:
        task = tasks_by_id.get(sched["task_id"])
        if task is None:
            continue
        actuals = store.list_actuals(task["id"])
        hours_logged = sum(a["hours_logged"] or 0.0 for a in actuals)
        percent_complete = actuals[-1]["percent_complete"] if actuals and actuals[-1]["percent_complete"] is not None else task.get("percent_complete", 0.0)

        planned_finish = date.fromisoformat(sched["earliest_finish"]) if sched.get("earliest_finish") else None
        slippage_days = None
        if planned_finish is not None and percent_complete < 100 and today > planned_finish:
            slippage_days = (today - planned_finish).days

        estimate = task.get("estimate_hours") or 0.0
        results.append(
            {
                "task_id": task["id"], "title": task["title"],
                "estimate_hours": estimate, "hours_logged": round(hours_logged, 2),
                "hours_variance": round(hours_logged - estimate, 2),
                "percent_complete": percent_complete,
                "planned_finish": sched.get("earliest_finish"),
                "slippage_days": slippage_days,
            }
        )
    return {"ok": True, "baseline_scenario_id": baseline["id"], "tasks": results}
