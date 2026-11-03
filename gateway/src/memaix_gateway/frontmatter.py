# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared YAML-frontmatter parsing/serialisation + atomic file writes.

Backlog items, sprint files and PM reports are all markdown with a YAML
frontmatter block.  Before this module the split/join regex lived in three
places (backlog.py, board/store.py, pm.py) with subtly different error
handling.  Centralising it means one parser to review and test, and one
place to make writes crash-safe.

See docs/DEVELOPMENT-PROPOSALS.md §10.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import yaml

# Matches a leading "---\n...\n---" frontmatter block followed by the body.
_FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)


def split(text: str) -> tuple[dict, str]:
    """Split *text* into (meta, body).

    Returns ({}, text) when there is no parseable frontmatter block, so callers
    can treat a bodyless/frontmatterless file gracefully.  Raises no exception
    on malformed YAML beyond what yaml.safe_load raises — callers that must be
    lenient should catch yaml.YAMLError.
    """
    m = _FM_RE.match(text or "")
    if not m:
        return {}, (text or "").strip()
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, m.group(2).strip()


def join(meta: dict, body: str) -> str:
    """Serialise *meta* + *body* back into a frontmatter markdown document."""
    front = yaml.dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n{body}\n"


def write_atomic(path: Path, text: str, *, encoding: str = "utf-8", vault: Path | None = None) -> None:
    """Write *text* to *path* atomically (temp file in same dir + os.replace).

    Prevents a torn/half-written file if the process dies mid-write; readers
    always see either the old or the new content, never a truncated mix.
    """
    if vault is not None:
        from . import vault_fs

        path = vault_fs.check(vault, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
