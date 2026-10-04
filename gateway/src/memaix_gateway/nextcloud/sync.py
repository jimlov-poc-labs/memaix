# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deck ↔ backlog and Notes ↔ memory two-way sync (FEATURE-NEXTCLOUD-BACKEND.md
§7, Byggordning step 6).

Both syncs share one conflict rule, "senast ändrad vinner": a synced_at
baseline is kept per link, and on each sync call a side counts as "changed"
if its own last-modified timestamp is newer than that baseline.

  - only the Nextcloud side changed -> Memaix is updated from it
  - only the Memaix side changed    -> Nextcloud is updated from it
  - both changed                    -> conflict; whichever is more recently
                                        modified wins, and the conflict is
                                        reported (never silently dropped)
  - neither changed                 -> no-op

deck_sync links a backlog item to a Deck card via
`deck_board_id`/`deck_stack_id`/`deck_card_id`/`deck_synced_at` in the
item's YAML frontmatter (backlog items already have a metadata slot).
Only `title`/`description` sync — labels, due dates, assignees and
attachments are a documented v1 limitation.

notes_sync links a memory note to a Nextcloud Notes id via NotesLinkStore
(memory notes are a plain content blob with no metadata slot of their own,
so the link + baseline live in a small side table instead). Only note
*content* syncs — Nextcloud's own title isn't kept in sync after the note
is first created (it only exists to name the memaix file).

In both directions: a deletion on the Nextcloud side is never used to
delete the Memaix-side item (sync is additive/safe by default) — it's
just skipped.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .. import frontmatter as fm
from ..acl import Acl
from ..tools import backlog as t_backlog
from ..tools import memory as t_memory


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _backlog_dir(acl: Acl, project: str) -> Path:
    vault = acl.resource(project, "vault")
    if not vault:
        raise ValueError(f"project {project!r} has no vault configured")
    return Path(vault) / "backlog"


def _stamp(path: Path, *, description: str | None = None, **fields) -> None:
    meta, body = fm.split(path.read_text(encoding="utf-8"))
    if description is not None:
        body = description
    meta.update(fields)
    fm.write_atomic(path, fm.join(meta, body))


def _apply_deck_to_backlog(path: Path, card: dict) -> None:
    _stamp(path, title=card["title"], description=card["description"], updated_at=_now_iso(), deck_synced_at=_now_iso())


def _apply_backlog_to_deck(deck, board_id, stack_id, card_id: int, item: dict, path: Path) -> None:
    deck.update_card(board_id, stack_id, card_id, title=item["title"], description=item.get("description", ""))
    _stamp(path, deck_synced_at=_now_iso())


def deck_sync(acl: Acl, user_id: str, project: str, *, _deck, board_id, stack_id) -> dict:
    """Sync one Deck stack against the project's backlog. Owner only (it
    mutates both stores). Returns {created, updated_from_deck,
    updated_from_backlog, conflicts}."""
    acl.enforce(user_id, project, "owner")
    bl_dir = _backlog_dir(acl, project)

    cards = {str(c["id"]): c for c in _deck.list_cards(board_id, stack_id)}
    items = t_backlog.backlog_list(acl, user_id, project)
    linked = {str(i["deck_card_id"]): i for i in items if i.get("deck_card_id")}

    created: list[dict] = []
    updated_from_deck: list[str] = []
    updated_from_backlog: list[str] = []
    conflicts: list[dict] = []

    for card_id, card in cards.items():
        if card_id in linked:
            continue
        result = t_backlog.backlog_add(acl, user_id, project, card["title"], card["description"])
        path = bl_dir / f"{result['id']}.md"
        _stamp(path, deck_board_id=board_id, deck_stack_id=stack_id, deck_card_id=card_id, deck_synced_at=_now_iso())
        created.append({"backlog_id": result["id"], "deck_card_id": card_id})

    for card_id, item in linked.items():
        card = cards.get(card_id)
        if card is None:
            continue  # deleted on the Deck side — never delete the backlog item
        baseline = _parse_ts(item.get("deck_synced_at"))
        card_changed = card["last_modified"] > baseline
        item_changed = _parse_ts(item.get("updated_at")) > baseline
        path = bl_dir / f"{item['id']}.md"

        if card_changed and item_changed:
            if card["last_modified"] >= _parse_ts(item.get("updated_at")):
                _apply_deck_to_backlog(path, card)
                winner = "deck"
            else:
                _apply_backlog_to_deck(_deck, board_id, stack_id, card["id"], item, path)
                winner = "backlog"
            conflicts.append({"backlog_id": item["id"], "deck_card_id": card_id, "winner": winner})
        elif card_changed:
            _apply_deck_to_backlog(path, card)
            updated_from_deck.append(item["id"])
        elif item_changed:
            _apply_backlog_to_deck(_deck, board_id, stack_id, card["id"], item, path)
            updated_from_backlog.append(item["id"])

    return {
        "created": created,
        "updated_from_deck": updated_from_deck,
        "updated_from_backlog": updated_from_backlog,
        "conflicts": conflicts,
    }


def _slugify(title: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"notes/{slug or fallback}.md"


def _unique_note_path(acl: Acl, user_id: str, project: str, path: str) -> str:
    if not _memory_note_exists(acl, user_id, project, path):
        return path
    stem, _, ext = path.rpartition(".")
    for n in range(2, 1000):
        candidate = f"{stem}-{n}.{ext}"
        if not _memory_note_exists(acl, user_id, project, candidate):
            return candidate
    raise RuntimeError(f"could not find a free note path near {path!r}")


def _memory_note_exists(acl: Acl, user_id: str, project: str, path: str) -> bool:
    try:
        t_memory.memory_read(acl, user_id, project, path)
        return True
    except FileNotFoundError:
        return False


class _NotesSync:
    """One notes_sync run: the stores involved plus the per-note sync steps."""

    def __init__(self, acl: Acl, user_id: str, project: str, notes, link_store):
        self.acl = acl
        self.user_id = user_id
        self.project = project
        self.notes = notes
        self.link_store = link_store

    def _write_memory(self, path: str, content: str) -> None:
        t_memory.memory_write(self.acl, self.user_id, self.project, path, content)

    def _push_memory_to_notes(self, path: str, nc_id: str) -> None:
        content = t_memory.memory_read(self.acl, self.user_id, self.project, path)["content"]
        self.notes.update_note(nc_id, content=content)

    def _restamp(self, path: str, nc_id: str) -> None:
        self.link_store.set_link(self.project, path, nc_id, _now_iso())

    def pull_new(self, nc_notes: dict, linked: dict) -> list[dict]:
        """Nextcloud notes without a link become new memory notes."""
        created: list[dict] = []
        for nc_id, note in nc_notes.items():
            if nc_id in linked:
                continue
            path = _unique_note_path(
                self.acl, self.user_id, self.project, _slugify(note["title"], nc_id)
            )
            self._write_memory(path, note["content"])
            self._restamp(path, nc_id)
            created.append({"note_path": path, "nc_note_id": nc_id})
        return created

    def _resolve_conflict(self, path: str, nc_id: str, note: dict, memory_ts) -> str:
        """Both sides changed: the more recently modified one wins (ties -> notes)."""
        if note["last_modified"] >= _parse_ts(memory_ts):
            self._write_memory(path, note["content"])
            return "notes"
        self._push_memory_to_notes(path, nc_id)
        return "memory"

    def sync_linked(self, link: dict, nc_id: str, note: dict, result: dict) -> None:
        """Sync one already-linked note; appends to *result* when something moved."""
        path = link["note_path"]
        if not _memory_note_exists(self.acl, self.user_id, self.project, path):
            return  # deleted on the memaix side — leave the Nextcloud note alone

        baseline = _parse_ts(link.get("synced_at"))
        note_changed = note["last_modified"] > baseline
        memory_ts = t_memory._get_store(self.acl, self.project).get_updated_at(path)
        memory_changed = _parse_ts(memory_ts) > baseline

        if note_changed and memory_changed:
            winner = self._resolve_conflict(path, nc_id, note, memory_ts)
            self._restamp(path, nc_id)
            result["conflicts"].append({"note_path": path, "nc_note_id": nc_id, "winner": winner})
        elif note_changed:
            self._write_memory(path, note["content"])
            self._restamp(path, nc_id)
            result["updated_from_notes"].append(path)
        elif memory_changed:
            self._push_memory_to_notes(path, nc_id)
            self._restamp(path, nc_id)
            result["updated_from_memory"].append(path)


def notes_sync(acl: Acl, user_id: str, project: str, *, _notes, link_store) -> dict:
    """Sync a Nextcloud Notes account against the project's memory notes.
    Owner only (it mutates both stores). Returns {created, updated_from_notes,
    updated_from_memory, conflicts}."""
    acl.enforce(user_id, project, "owner")

    nc_notes = {str(n["id"]): n for n in _notes.list_notes()}
    links = link_store.list_links(project)
    linked_by_nc_id = {link["nc_note_id"]: link for link in links}

    run = _NotesSync(acl, user_id, project, _notes, link_store)
    result: dict = {
        "created": run.pull_new(nc_notes, linked_by_nc_id),
        "updated_from_notes": [],
        "updated_from_memory": [],
        "conflicts": [],
    }

    for nc_id, link in linked_by_nc_id.items():
        note = nc_notes.get(nc_id)
        if note is None:
            continue  # deleted on the Nextcloud side — never delete the memory note
        run.sync_linked(link, nc_id, note, result)

    return result
