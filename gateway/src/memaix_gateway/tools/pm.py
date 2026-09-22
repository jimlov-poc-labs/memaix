# SPDX-License-Identifier: AGPL-3.0-or-later
"""pm_* tools — project management (methodology, sprints, RAID, status reports)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .. import frontmatter as fm
from .. import gitvault
from ..acl import Acl
from ..paths import validate_id
from . import backlog as t_backlog
from . import memory as t_memory

RAID_TYPES = ("Risk", "Assumption", "Issue", "Dependency")
_PLAYBOOK_FILE = "playbook.md"
_RAID_FILE = "raid.md"


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _vault(acl: Acl, project: str) -> Path:
    v = acl.resource(project, "vault")
    if not v:
        raise ValueError(f"project {project!r} has no vault configured")
    return Path(v)


def _write(path: Path, text: str) -> None:
    fm.write_atomic(path, text)


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _split_fm(text: str) -> tuple[dict, str]:
    return fm.split(text)


def _join_fm(meta: dict, body: str) -> str:
    return fm.join(meta, body)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _git_commit(vault: Path, rel_paths: list[str], message: str) -> bool:
    """Commit *rel_paths*. False only when the vault keeps no history at all.

    The previous version wrapped both git calls in a bare `except Exception`
    and returned the commit's returncode as a bool nobody inspected. That
    swallowed `FileNotFoundError` when git is absent, `PermissionError` on
    the vault, and -- in production -- exit 128 on every single call, and
    reported all of it as an unremarkable `committed: false`.

    It also ran git without a committer identity, so even once the ownership
    is fixed these commits would have died on "Author identity unknown".
    gitvault supplies both the identity and the returncode check.
    """
    if not gitvault.is_repo(vault):
        return False
    gitvault.commit(vault, rel_paths, message)
    return True


# ------------------------------------------------------------------
# Backlog item PM-field stamping (mirrors backlog.py's _write_item)
# ------------------------------------------------------------------


def _stamp_backlog_field(vault: Path, item_id: str, **fields) -> bool:
    """Non-destructively update named frontmatter fields on a backlog item."""
    path = vault / "backlog" / f"{item_id}.md"
    if not path.exists():
        return False
    meta, body = fm.split(path.read_text(encoding="utf-8"))
    if not meta:
        return False
    meta.update(fields)
    meta["updated_at"] = _now_iso()
    fm.write_atomic(path, fm.join(meta, body))
    return True


# ------------------------------------------------------------------
# RAID helpers
# ------------------------------------------------------------------


def _parse_raid(text: str) -> list[dict]:
    entries = []
    blocks = text.split("<!-- raid -->")[1:]
    head = re.compile(r"###\s+(RAID-\d+)\s+·\s+(\w+)\s+·\s+(\w+)")
    # [ \t]* (not \s*) after the colon: \s* would swallow the newline after
    # an empty field (e.g. a blank "Owner:") and greedily capture the next
    # field's entire line as this field's value instead of leaving it empty.
    field_re = re.compile(r"-\s+\*\*(\w+):\*\*[ \t]*(.*)")
    for b in blocks:
        m = head.search(b)
        if not m:
            continue
        e = {
            "id": m.group(1), "type": m.group(2), "status": m.group(3),
            "created": "", "owner": "", "severity": "", "summary": "", "mitigation": "",
        }
        for fmatch in field_re.finditer(b):
            e[fmatch.group(1).lower()] = fmatch.group(2).strip()
        entries.append(e)
    return entries


# ------------------------------------------------------------------
# Public tool functions
# ------------------------------------------------------------------


def pm_set_methodology(
    acl: Acl,
    user_id: str,
    project: str,
    methodology: str,
    sprint_length_days: int = 14,
    capacity: dict | None = None,
) -> dict:
    acl.enforce(user_id, project, "owner")
    vault = _vault(acl, project)
    playbook = vault / _PLAYBOOK_FILE

    text = _read(playbook) or "# Playbook\n"
    meta, body = _split_fm(text)
    meta["methodology"] = methodology
    meta["sprint_length_days"] = int(sprint_length_days)
    meta["capacity"] = capacity or {}
    _write(playbook, _join_fm(meta, body))

    committed = _git_commit(vault, [_PLAYBOOK_FILE], f"pm: set methodology={methodology}")
    return {
        "ok": True,
        "playbook": _PLAYBOOK_FILE,
        "methodology": methodology,
        "sprint_length_days": int(sprint_length_days),
        "capacity": capacity or {},
        "committed": committed,
    }


def pm_status_report(
    acl: Acl,
    user_id: str,
    project: str,
    note: str = "status",
) -> dict:
    acl.enforce(user_id, project, "reader")
    vault = _vault(acl, project)

    items = t_backlog.backlog_list(acl, user_id, project)
    statuses = ["inbox", "triaged", "evaluated", "approved", "rejected", "in-dev", "done"]
    counts: dict[str, int] = dict.fromkeys(statuses, 0)
    for item in items:
        s = item.get("status", "inbox")
        if s in counts:
            counts[s] += 1

    pb_meta, _ = _split_fm(_read(vault / _PLAYBOOK_FILE) or "")
    methodology = pb_meta.get("methodology", "")

    raid_text = _read(vault / "pm" / _RAID_FILE) or ""
    open_raids = sum(1 for e in _parse_raid(raid_text) if e.get("status") == "open")

    notes_text = ""
    try:
        result = t_memory.memory_read(acl, user_id, project, note)
        notes_text = result.get("content", "")
    except Exception:
        pass

    today = _today()
    total = len(items)

    sprint_counts: dict[str, int] = {}
    for item in items:
        sp = item.get("sprint")
        if sp:
            sprint_counts[sp] = sprint_counts.get(sp, 0) + 1
    active_sprint = max(sprint_counts, key=lambda k: sprint_counts[k]) if sprint_counts else None

    in_dev = [i for i in items if i.get("status") == "in-dev"]
    recently_done = [i for i in items if i.get("status") == "done"][-5:]

    counts_table = "\n".join(f"| {s} | {counts.get(s, 0)} |" for s in statuses)
    in_dev_lines = "\n".join(
        f"- {i.get('id', '?')} — {i.get('title', '')}" for i in in_dev
    ) or "_Inga_"
    done_lines = "\n".join(
        f"- {i.get('id', '?')} — {i.get('title', '')}" for i in recently_done
    ) or "_Inga_"
    sprint_section = f"\n## Active sprint\n{active_sprint}\n" if active_sprint else ""

    content = f"""---
type: status-report
date: {today}
methodology: {methodology}
---
# Status Report — {today}

## Summary
- Total backlog items: {total}
- In development: {counts.get('in-dev', 0)}
- Done: {counts.get('done', 0)}
- Open RAID entries: {open_raids}

## Backlog by status
| Status | Count |
|--------|-------|
{counts_table}

## In development
{in_dev_lines}

## Recently done
{done_lines}
{sprint_section}## Notes
{notes_text.strip() or "_No notes._"}
"""

    rel = f"pm/reports/STATUS-{today}.md"
    _write(vault / "pm" / "reports" / f"STATUS-{today}.md", content)
    committed = _git_commit(vault, [rel], f"pm: status report {today}")

    return {
        "ok": True,
        "report": rel,
        "counts": counts,
        "total": total,
        "open_raids": open_raids,
        "committed": committed,
    }


def pm_plan_sprint(
    acl: Acl,
    user_id: str,
    project: str,
    sprint_id: str,
    item_ids: list[str],
    goal: str = "",
) -> dict:
    acl.enforce(user_id, project, "owner")
    validate_id(sprint_id, kind="sprint id")
    for _iid in item_ids:
        validate_id(_iid, kind="backlog id")
    vault = _vault(acl, project)

    pb_meta, _ = _split_fm(_read(vault / _PLAYBOOK_FILE) or "")
    capacity_map = pb_meta.get("capacity") or {}
    capacity_points: int | None = sum(int(v) for v in capacity_map.values()) if capacity_map else None
    sprint_length = int(pb_meta.get("sprint_length_days", 14))

    warnings: list[str] = []
    errors: list[str] = []
    items: list[dict] = []
    committed_points = 0

    if capacity_points is None:
        warnings.append("no playbook capacity; sprint uncapped")

    sprint_path = vault / "pm" / "sprints" / f"{sprint_id}.md"
    if sprint_path.exists():
        warnings.append(f"{sprint_id} already exists; overwriting")

    for item_id in item_ids:
        item_path = vault / "backlog" / f"{item_id}.md"
        if not item_path.exists():
            errors.append(f"{item_id}: not found")
            continue
        meta, _ = fm.split(item_path.read_text(encoding="utf-8"))
        estimate = meta.get("estimate")
        if estimate is None:
            warnings.append(f"{item_id}: no estimate, counted as 0")
            estimate = 0
        items.append({"id": item_id, "estimate": int(estimate)})
        committed_points += int(estimate)

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    over_capacity = capacity_points is not None and committed_points > capacity_points
    if over_capacity:
        warnings.append(f"committed {committed_points} > capacity {capacity_points}")

    items_yaml = "\n".join(f"  - id: {i['id']}\n    estimate: {i['estimate']}" for i in items)
    items_table = "\n".join(f"| {i['id']} | {i['estimate']} |" for i in items)
    cap_display = capacity_points if capacity_points is not None else "uncapped"
    sprint_content = f"""---
id: {sprint_id}
goal: {goal}
status: planned
length_days: {sprint_length}
capacity_points: {cap_display}
committed_points: {committed_points}
created: {_today()}
items:
{items_yaml}
---
# {sprint_id} — {goal}

Capacity: {cap_display} pts · Committed: {committed_points} pts

| Item | Estimate |
|------|----------|
{items_table}
"""
    _write(sprint_path, sprint_content)

    stamped_rels = []
    for i in items:
        if _stamp_backlog_field(vault, i["id"], sprint=sprint_id):
            stamped_rels.append(f"backlog/{i['id']}.md")

    sprint_rel = f"pm/sprints/{sprint_id}.md"
    committed = _git_commit(
        vault,
        [sprint_rel, *stamped_rels],
        f"pm: plan {sprint_id} ({committed_points} pts)",
    )

    return {
        "ok": True,
        "sprint": sprint_id,
        "goal": goal,
        "items": items,
        "committed_points": committed_points,
        "capacity_points": capacity_points,
        "over_capacity": over_capacity,
        "warnings": warnings,
        "errors": [],
        "committed": committed,
    }


def pm_sprint_status(
    acl: Acl,
    user_id: str,
    project: str,
    sprint_id: str,
) -> dict:
    acl.enforce(user_id, project, "reader")
    validate_id(sprint_id, kind="sprint id")
    vault = _vault(acl, project)

    text = _read(vault / "pm" / "sprints" / f"{sprint_id}.md")
    if text is None:
        return {"ok": False, "error": f"{sprint_id} not found"}

    meta, _ = _split_fm(text)
    committed = meta.get("items", [])

    all_items = t_backlog.backlog_list(acl, user_id, project)
    by_id = {i.get("id"): i for i in all_items}

    warnings: list[str] = []
    item_results = []
    total_points = 0
    done_points = 0

    for c in committed:
        item_id = c.get("id", "")
        estimate = int(c.get("estimate", 0))
        live = by_id.get(item_id)
        if live is None:
            warnings.append(f"{item_id}: not found in backlog (deleted?)")
            status = "missing"
            done = False
        else:
            status = live.get("status", "")
            done = status == "done"

        total_points += estimate
        if done:
            done_points += estimate
        item_results.append({"id": item_id, "estimate": estimate, "status": status, "done": done})

    remaining_points = total_points - done_points
    done_count = sum(1 for i in item_results if i["done"])
    pct_complete = round(100 * done_points / total_points) if total_points else 0

    return {
        "ok": True,
        "sprint": sprint_id,
        "goal": meta.get("goal", ""),
        "total_count": len(item_results),
        "done_count": done_count,
        "total_points": total_points,
        "done_points": done_points,
        "remaining_points": remaining_points,
        "pct_complete": pct_complete,
        "items": item_results,
        "warnings": warnings,
    }


def pm_raid_add(
    acl: Acl,
    user_id: str,
    project: str,
    raid_type: str,
    summary: str,
    owner: str = "",
    severity: str = "medium",
    mitigation: str = "",
) -> dict:
    acl.enforce(user_id, project, "collaborator")

    raid_type = raid_type.title()
    if raid_type not in RAID_TYPES:
        return {"ok": False, "error": f"raid_type must be one of {RAID_TYPES}"}

    vault = _vault(acl, project)
    raid_path = vault / "pm" / _RAID_FILE
    text = _read(raid_path) or "# RAID Log\n"

    existing = re.findall(r"RAID-(\d+)", text)
    next_num = max((int(n) for n in existing), default=0) + 1
    raid_id = f"RAID-{next_num:04d}"

    entry = f"""
<!-- raid -->
### {raid_id} · {raid_type} · open
- **Created:** {_now_iso()}
- **Owner:** {owner}
- **Severity:** {severity}
- **Summary:** {summary}
- **Mitigation:** {mitigation}
"""
    _write(raid_path, text.rstrip() + "\n" + entry)
    committed = _git_commit(vault, ["pm/raid.md"], f"pm: raid {raid_id} {raid_type}")

    return {"ok": True, "raid_id": raid_id, "type": raid_type, "committed": committed}


def pm_raid_list(
    acl: Acl,
    user_id: str,
    project: str,
    raid_type: str = "",
) -> dict:
    acl.enforce(user_id, project, "reader")
    vault = _vault(acl, project)

    text = _read(vault / "pm" / _RAID_FILE)
    if text is None:
        return {"ok": True, "entries": [], "count": 0}

    entries = _parse_raid(text)
    if raid_type:
        entries = [e for e in entries if e["type"].lower() == raid_type.lower()]

    return {"ok": True, "entries": entries, "count": len(entries)}
