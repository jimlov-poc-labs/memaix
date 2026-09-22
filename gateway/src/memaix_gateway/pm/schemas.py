# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic input schemas for PMStore's write methods (DEVELOPMENT-PROPOSALS.md §10).

SQLite is dynamically typed — it accepts whatever a column is handed, so
nothing today stops a caller from writing `priority="high"` or
`percent_complete=250` into the plan. The worst offender is
`update_task(**fields)`, which builds its UPDATE from arbitrary kwargs with
no check on field name or value type at all.

These models validate at each store method's entry point (constructed from
the method's own arguments, then discarded — they don't change what's
stored, only what's accepted). Bounds are only added where the surrounding
code already treats them as invariants (percent_complete is compared against
100 in pm/report.py; dependency/scenario literals are the only values any
code path ever produces) — undocumented fields like `priority` (no upper
bound anywhere in FEATURE-PM-ENGINE.md or the allocator) are typed but not
range-constrained, to avoid inventing a limit the product never specified.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

DependencyType = Literal["FS", "SS", "FF", "SF"]
ScenarioKind = Literal["baseline", "committed", "whatif"]
_EMPTY_MSG = "must not be empty"


class ResourceInput(BaseModel):
    project: str
    name: str
    user_sub: str | None = None
    cost_per_hour: float | None = None
    capacity_hours_per_day: float = 8.0
    active: bool = True

    @field_validator("name", "project")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(_EMPTY_MSG)
        return v

    @field_validator("cost_per_hour")
    @classmethod
    def _non_negative_cost(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("cost_per_hour must be >= 0")
        return v

    @field_validator("capacity_hours_per_day")
    @classmethod
    def _positive_capacity(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("capacity_hours_per_day must be > 0")
        return v


class AvailabilityInput(BaseModel):
    resource_id: int
    start_date: str
    end_date: str
    hours_per_day: float
    reason: str | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def _iso_date(cls, v: str) -> str:
        date.fromisoformat(v)  # raises ValueError on bad format
        return v

    @field_validator("hours_per_day")
    @classmethod
    def _non_negative_hours(cls, v: float) -> float:
        if v < 0:
            raise ValueError("hours_per_day must be >= 0")
        return v

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "AvailabilityInput":
        if date.fromisoformat(self.end_date) < date.fromisoformat(self.start_date):
            raise ValueError("end_date must not be before start_date")
        return self


class MilestoneInput(BaseModel):
    project: str
    name: str
    target_date: str | None = None
    status: str = "open"

    @field_validator("name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(_EMPTY_MSG)
        return v

    @field_validator("target_date")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v is not None:
            date.fromisoformat(v)
        return v


class TaskInput(BaseModel):
    project: str
    title: str
    backlog_id: str | None = None
    estimate_hours: float | None = None
    required_skill_id: int | None = None
    priority: int = 3
    milestone_id: int | None = None
    status: str = "todo"
    percent_complete: float = 0.0

    @field_validator("title")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(_EMPTY_MSG)
        return v

    @field_validator("estimate_hours")
    @classmethod
    def _non_negative_estimate(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("estimate_hours must be >= 0")
        return v

    @field_validator("percent_complete")
    @classmethod
    def _percent_range(cls, v: float) -> float:
        if not 0 <= v <= 100:
            raise ValueError("percent_complete must be between 0 and 100")
        return v


class TaskUpdate(BaseModel):
    """Validates update_task's **fields — only real, correctly-typed columns."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    backlog_id: str | None = None
    estimate_hours: float | None = None
    required_skill_id: int | None = None
    priority: int | None = None
    milestone_id: int | None = None
    status: str | None = None
    percent_complete: float | None = None

    @field_validator("percent_complete")
    @classmethod
    def _percent_range(cls, v: float | None) -> float | None:
        if v is not None and not 0 <= v <= 100:
            raise ValueError("percent_complete must be between 0 and 100")
        return v

    @field_validator("estimate_hours")
    @classmethod
    def _non_negative_estimate(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("estimate_hours must be >= 0")
        return v


class DependencyInput(BaseModel):
    predecessor_id: int
    successor_id: int
    type: DependencyType = "FS"
    lag_days: float = 0.0


class ScenarioInput(BaseModel):
    project: str
    name: str
    kind: ScenarioKind
    parent_id: int | None = None
    note: str | None = None

    @field_validator("name", "project")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(_EMPTY_MSG)
        return v
