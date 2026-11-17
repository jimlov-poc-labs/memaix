# SPDX-License-Identifier: AGPL-3.0-or-later
"""Board data-access helpers — read/write backlog and sprint files."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import frontmatter as fm
from .. import vault_fs
from ..paths import validate_id

VALID_STATUSES = frozenset(
    {"inbox", "triaged", "evaluated", "approved", "rejected", "in-dev", "done"}
)

COLUMNS = [
    ("inbox",     "Inbox"),
    ("triaged",   "Triaged"),
    ("evaluated", "Evaluated"),
    ("approved",  "Approved"),
    ("in-dev",    "In Dev"),
    ("done",      "Done"),
    ("rejected",  "Rejected"),
]


def _split_fm(text: str) -> tuple[dict, str]:
    return fm.split(text)


def _card_view(meta: dict) -> dict:
    return {
        "id":          meta.get("id", ""),
        "title":       meta.get("title", ""),
        "category":    meta.get("category") or "",
        "status":      meta.get("status", "inbox"),
        "assignee":    meta.get("assignee") or "",
        "value":       meta.get("value"),
        "complexity":  meta.get("complexity"),
        "risk":        meta.get("risk"),
        "sprint":      meta.get("sprint") or "",
        "estimate":    meta.get("estimate"),
        "updated_at":  str(meta.get("updated_at", "")),
        "version":     meta.get("version"),
    }


def list_backlog(vault: Path) -> list[dict]:
    """Return lightweight card dicts for all backlog items. Skips malformed files."""
    bl = vault / "backlog"
    if not bl.is_dir():
        return []
    cards = []
    for p in vault_fs.iter_files(vault, bl, "*.md"):
        try:
            meta, _ = _split_fm(vault_fs.read_text(vault, p))
            if meta.get("id"):
                cards.append(_card_view(meta))
        except Exception:
            cards.append({
                "id": p.stem, "title": f"⚠ parse error: {p.name}",
                "category": "", "status": "inbox", "assignee": "", "value": None,
                "complexity": None, "risk": None, "sprint": "",
                "estimate": None, "updated_at": "", "version": None,
            })
    return cards


def get_item(vault: Path, item_id: str) -> dict | None:
    """Return full item dict including markdown body, or None if not found."""
    try:
        validate_id(item_id, kind="item id")
    except ValueError:
        return None
    path = vault / "backlog" / f"{item_id}.md"
    if not vault_fs.exists(vault, path):
        return None
    try:
        meta, body = _split_fm(vault_fs.read_text(vault, path))
        result = {
            k: v.isoformat() if isinstance(v, datetime) else v
            for k, v in meta.items()
        }
        result["body"] = body
        return result
    except Exception:
        return None


def write_status(vault: Path, item_id: str, new_status: str, expected_version: int | None = None) -> dict:
    """Update item status. Raises ValueError on bad status, FileNotFoundError if missing.

    expected_version is optional (the board UI doesn't track it yet) — when
    given and it doesn't match the file's current version, returns
    {"conflict": True, "current_version": N} without writing, same
    convention as tools/backlog.py's mutating calls."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")
    validate_id(item_id, kind="item id")
    path = vault / "backlog" / f"{item_id}.md"
    if not vault_fs.exists(vault, path):
        raise FileNotFoundError(f"item not found: {item_id}")
    text = vault_fs.read_text(vault, path)
    meta, body = _split_fm(text)
    current_version = int(meta.get("version", 1))
    if expected_version is not None and current_version != expected_version:
        return {"conflict": True, "current_version": current_version}
    old_status = meta.get("status", "")
    meta["status"] = new_status
    meta["version"] = current_version + 1
    meta["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fm.write_atomic(path, fm.join(meta, body), vault=vault)
    card = _card_view(meta)
    card["_old_status"] = old_status
    return card


def list_sprints(vault: Path) -> tuple[list[dict], str | None]:
    """Return (sprint_list, active_id). sprint_list ordered newest first."""
    sprint_dir = vault / "pm" / "sprints"
    if not sprint_dir.is_dir():
        return [], None
    sprints = []
    active_id = None
    for p in reversed(vault_fs.iter_files(vault, sprint_dir, "*.md")):
        try:
            meta, _ = _split_fm(vault_fs.read_text(vault, p))
            sprint_id = meta.get("id", p.stem)
            status = meta.get("status", "planned")
            items = meta.get("items") or []
            sprints.append({
                "id":         sprint_id,
                "goal":       meta.get("goal", ""),
                "status":     status,
                "item_count": len(items),
                "items":      [i.get("id") for i in items if isinstance(i, dict)],
            })
            if status in ("active", "in-progress") and active_id is None:
                active_id = sprint_id
        except Exception:
            continue
    return sprints, active_id
