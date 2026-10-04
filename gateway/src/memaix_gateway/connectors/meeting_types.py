# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-user named meeting types (duration/interval presets) — memaix-src
backlog card d0a1f633.

Purely advisory metadata for a future booking UI (2bef1062) to offer as
named choices ("30 min", "Djupdyk 90 min"). This module does NOT wire
into calendar_find_free — callers still pass duration_min explicitly;
a meeting type is just a labelled shortcut a client can resolve before
calling calendar_find_free, the same separation booking_settings.py
keeps from calendar_free_busy.

No stored types (or an unconfigured store) means [] — no shortcuts
defined, callers fall back to asking for duration_min directly.

Storage: one JSON file per (project, user) in the project vault, same
convention as booking_settings.py and working_hours.py.

Known limitation this card does NOT attempt to fix (see pm_raid_add):
calendar_find_free cannot return multi-day slots once a user has
configured working_hours, because apply_working_hours() chops every
free gap into day-sized windows before the duration filter runs. A
meeting type with a multi-day duration_min will silently find nothing
for such users.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
MAX_DURATION_MIN = 43200  # 30 days


def _meeting_types_path(acl, project: str, user: str) -> Path:
    vault = acl.resource(project, "vault")
    if not vault:
        raise ValueError(f"project {project!r} has no vault configured")
    directory = Path(vault) / "meeting_types"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user}.json"


def _check_minutes(slug: str, field: str, value) -> None:
    if not isinstance(value, int) or not (1 <= value <= MAX_DURATION_MIN):
        raise ValueError(f"{slug!r}: {field} must be an int in 1..{MAX_DURATION_MIN}")


def _validate_type(t, slugs: set[str]) -> dict:
    """Validate one entry and return its normalized copy; records its slug."""
    if not isinstance(t, dict):
        raise ValueError(f"each meeting type must be an object, got {t!r}")
    slug = t.get("slug", "")
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid slug {slug!r}: must be lowercase alphanumeric/hyphen")
    if slug in slugs:
        raise ValueError(f"duplicate slug {slug!r}")
    slugs.add(slug)

    name = t.get("name", "")
    if not name:
        raise ValueError(f"{slug!r}: name is required")

    duration_min = t.get("duration_min")
    _check_minutes(slug, "duration_min", duration_min)

    interval_min = t.get("interval_min", duration_min)
    _check_minutes(slug, "interval_min", interval_min)

    return {
        "slug": slug,
        "name": name,
        "duration_min": duration_min,
        "interval_min": interval_min,
        "default": bool(t.get("default", False)),
    }


def validate_types(types: list[dict]) -> list[dict]:
    """Raise ValueError if *types* is not a valid list of meeting types.
    Returns a normalized copy: interval_min defaulted to duration_min
    where omitted, and exactly one entry marked default (auto-promoting
    the first if none was marked)."""
    slugs: set[str] = set()
    out = [_validate_type(t, slugs) for t in types]

    defaults = [t for t in out if t["default"]]
    if len(defaults) > 1:
        raise ValueError("at most one meeting type may be marked default")
    if out and not defaults:
        out[0]["default"] = True

    return out


class MeetingTypesStore:
    def __init__(self, acl, project: str, user: str) -> None:
        self._path = _meeting_types_path(acl, project, user)

    def get(self) -> list[dict]:
        """[] if never configured — no named shortcuts defined."""
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text())

    def set(self, types: list[dict]) -> list[dict]:
        normalized = validate_types(types)
        self._path.write_text(json.dumps(normalized))
        return normalized

    def delete(self, slug: str) -> list[dict]:
        """Remove one type by slug and persist the rest. No error if the
        slug isn't present — deleting an already-gone type is a no-op."""
        remaining = [t for t in self.get() if t["slug"] != slug]
        return self.set(remaining)
