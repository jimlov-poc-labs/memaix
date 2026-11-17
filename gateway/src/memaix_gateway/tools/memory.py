# SPDX-License-Identifier: AGPL-3.0-or-later
"""memory_* tools — structured note store backed by MemoryStore.

All functions are pure (acl + user_id injected, no global state).
ACL is enforced before any backend call (SAFETY.md §2).

Path rules for notes:
  - Must be relative (no leading /)
  - Must not contain '..' components
  - e.g. "ideas/feature-x.md", "standup/2024-06-29.md"

Minnestrappan (SELF-IMPROVING-SYSTEM.md Fas B): en notering är `hypotes`
eller `verifierad`, buret i frontmatter (samma konvention och parser som
backlogens items — frontmatter.py). **Saknad status = hypotes**: det säkra
defaultet gör att innehåll aldrig behöver tvångsstämplas — externa flöden
(Nextcloud-synk) behåller sin innehållstrohet, och ändå kan ingen omärkt
notering läsas som faktum. Frontmatter skrivs bara vid uttryckligt statusval
(memory_write med status, memory_set_status). Befordran till verifierad
kräver källbekräftelse eller mänskligt besked, aldrig "låter rimligt"
(anti-hype-listan).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .. import frontmatter as fm
from ..acl import Acl
from ..backends.memory_store import MemoryStore
from ..paths import validate_relative_path

VALID_STATUS = ("hypotes", "verifierad")

# Matches absolute paths with ≥2 segments: /foo/bar, /srv/vaults/x.md
# Negative lookbehind prevents matching after :// (URLs) or word chars (identifiers).
_PATH_RE = re.compile(r"(?<![:/\w])((?:/[\w][\w.\-]*){2,})")


def _check_provenance(content: str) -> dict:
    """Extract absolute file-path references from content and verify they exist.

    Returns {"grade": "verified"|"unverifiable", "dead_refs": [...]}.
    Grade is "verified" when no path-like references are found, or all found
    paths resolve. Informational only — never changes the note's status.
    """
    refs = _PATH_RE.findall(content)
    dead = [r for r in refs if not os.path.exists(r)]
    if dead:
        return {"grade": "unverifiable", "dead_refs": dead}
    return {"grade": "verified", "dead_refs": []}


def note_status(content: str) -> str:
    """Statusen ur en noterings frontmatter — okänd/ogiltig/saknad → hypotes."""
    try:
        meta, _ = fm.split(content or "")
    except yaml.YAMLError:
        return "hypotes"
    status = meta.get("status")
    return status if status in VALID_STATUS else "hypotes"


def set_note_status(content: str, status: str) -> str:
    """Returnera innehållet med status satt i frontmatter (skapas vid behov).
    Övriga frontmatter-fält bevaras."""
    if status not in VALID_STATUS:
        raise ValueError(f"status must be one of {VALID_STATUS}, got {status!r}")
    try:
        meta, body = fm.split(content or "")
    except yaml.YAMLError:
        meta, body = {}, (content or "").strip()
    meta["status"] = status
    return fm.join(meta, body)

# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


def _validate_note_path(note: str) -> None:
    validate_relative_path(note, kind="note path", forbid_git=True)


def _get_store(acl: Acl, project: str) -> MemoryStore:
    vault_str = acl.resource(project, "vault")
    if not vault_str:
        raise ValueError(f"project {project!r} has no vault configured")
    return MemoryStore.for_vault(Path(vault_str))


# ------------------------------------------------------------------
# Read tools — require "reader"
# ------------------------------------------------------------------


def memory_list(acl: Acl, user_id: str, project: str) -> list[dict]:
    """List all notes in the project vault. Returns [{path, mtime, status}]
    where mtime is the store's updated_at timestamp (empty string if unknown)."""
    acl.enforce(user_id, project, "reader")
    store = _get_store(acl, project)
    return [
        {
            "path": note,
            "mtime": store.get_updated_at(note) or "",
            "status": note_status(store.read(note) or ""),
        }
        for note in store.list_all()
    ]


def memory_read(acl: Acl, user_id: str, project: str, note: str) -> dict:
    """Return {path, content, status} for a note. Raises FileNotFoundError if absent."""
    acl.enforce(user_id, project, "reader")
    _validate_note_path(note)
    store = _get_store(acl, project)
    content = store.read(note)
    if content is None:
        raise FileNotFoundError(f"note not found: {note!r}")
    return {"path": note, "content": content, "status": note_status(content)}


def memory_search(acl: Acl, user_id: str, project: str, query: str) -> list[dict]:
    """FTS5 search.  Returns [{path, snippet, status}] — statusen följer med
    så en hypotes aldrig kan citeras som faktum utan att det syns."""
    acl.enforce(user_id, project, "reader")
    store = _get_store(acl, project)
    return [
        {**hit, "status": note_status(store.read(hit["path"]) or "")}
        for hit in store.search(query)
    ]


def memory_history(
    acl: Acl,
    user_id: str,
    project: str,
    note: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Git log for a note or the whole vault.  Returns [{hash, author, date, message}]."""
    acl.enforce(user_id, project, "reader")
    store = _get_store(acl, project)
    return store.history(note, limit)


# ------------------------------------------------------------------
# Write tools — require "collaborator"
# ------------------------------------------------------------------


def memory_write(
    acl: Acl, user_id: str, project: str, note: str, content: str,
    status: str | None = None,
) -> dict:
    """Overwrite note.  Returns {path, commit, status, provenance}.

    status=None: innehållet skrivs orört (extern trohet — synkflöden);
    dess status läses ur befintlig frontmatter, saknas den gäller hypotes.
    Explicit status stämplas i frontmatter och vinner över innehållets.

    provenance: informationsfält — anger om filreferenser i innehållet
    löser sig på disk. Påverkar aldrig status (mänskligt besked vinner)."""
    acl.enforce(user_id, project, "collaborator")
    _validate_note_path(note)
    if status is not None:
        content = set_note_status(content, status)
    store = _get_store(acl, project)
    commit = store.write(note, content, user_id)
    return {
        "path": note,
        "commit": commit,
        "status": note_status(content),
        "provenance": _check_provenance(content),
    }


def memory_append(
    acl: Acl, user_id: str, project: str, note: str, text: str
) -> dict:
    """Append text to note (creates if absent).  Returns {path, commit, status}.
    Ny notering utan uttalad status är hypotes (defaultet, ingen stämpel)."""
    acl.enforce(user_id, project, "collaborator")
    _validate_note_path(note)
    store = _get_store(acl, project)
    commit = store.append(note, text, user_id)
    return {"path": note, "commit": commit,
            "status": note_status(store.read(note) or "")}


def memory_set_status(
    acl: Acl, user_id: str, project: str, note: str, status: str
) -> dict:
    """Flytta en notering i trappan (hypotes ↔ verifierad) utan att röra
    innehållet. Befordran till verifierad förutsätter källbekräftelse eller
    mänskligt besked — det kontraktet bärs av prompten (whoami.memory_rules),
    spårbarheten av git-historiken. Returns {path, commit, status}."""
    acl.enforce(user_id, project, "collaborator")
    _validate_note_path(note)
    if status not in VALID_STATUS:
        raise ValueError(f"status must be one of {VALID_STATUS}, got {status!r}")
    store = _get_store(acl, project)
    content = store.read(note)
    if content is None:
        raise FileNotFoundError(f"note not found: {note!r}")
    commit = store.write(note, set_note_status(content, status), user_id)
    return {"path": note, "commit": commit, "status": status}


def memory_revert(
    acl: Acl, user_id: str, project: str, commit: str
) -> dict:
    """Revert a git commit.  Returns {reverted_to, new_commit}."""
    acl.enforce(user_id, project, "collaborator")
    store = _get_store(acl, project)
    new_commit = store.revert(commit)
    return {"reverted_to": commit, "new_commit": new_commit}
