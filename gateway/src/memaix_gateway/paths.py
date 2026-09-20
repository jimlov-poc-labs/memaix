# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared path/id validation — the single place that stops path traversal.

Every user-supplied identifier that becomes part of a filesystem path
(backlog item id, sprint id, note path) must pass through here before it
touches disk.  Keeping this in one module means the traversal defence can be
reviewed and tested once instead of re-implemented per tool.

See docs/DEVELOPMENT-PROPOSALS.md §1.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

def data_dir() -> Path:
    """Return the base directory for memaix runtime data files.

    Reads MEMAIX_DATA_DIR; falls back to /var/lib/memaix so databases are never
    placed in /tmp where they would be world-readable and ephemeral.
    """
    return Path(os.environ.get("MEMAIX_DATA_DIR", "/var/lib/memaix"))


# Safe id: starts alphanumeric, then alphanumerics / dot / dash / underscore.
# Matches backlog ids ("a1b2c3d4"), sprint ids ("SPRINT-01"), report names, etc.
# Rejects "/", "\\", "..", leading dot, and empty strings.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_id(value: str, *, kind: str = "id") -> str:
    """Return *value* unchanged if it is a safe single path segment.

    Raises ValueError otherwise.  Blocks path traversal ("../"), absolute
    paths and separator injection in ids that get interpolated into
    filesystem paths like ``vault / "backlog" / f"{id}.md"``.
    """
    if not value or not isinstance(value, str):
        raise ValueError(f"{kind} cannot be empty")
    if "/" in value or "\\" in value or ".." in value or "\x00" in value:
        raise ValueError(f"invalid {kind}: {value!r}")
    if not _SAFE_ID_RE.match(value):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


def validate_relative_path(path: str, *, kind: str = "path") -> str:
    """Validate a multi-segment relative path (e.g. a note like "a/b.md").

    Must be relative, non-empty, and contain no ".." components or NUL bytes.
    """
    if not path or not path.strip():
        raise ValueError(f"{kind} cannot be empty")
    if path.startswith("/") or "\x00" in path:
        raise ValueError(f"{kind} must be relative: {path!r}")
    if ".." in Path(path).parts:
        raise ValueError(f"{kind} cannot contain '..': {path!r}")
    return path


def safe_join(base: Path, *parts: str) -> Path:
    """Join *parts* onto *base* and guarantee the result stays inside *base*.

    Belt-and-suspenders against traversal even if a caller forgets to validate:
    resolves symlinks and raises ValueError if the result escapes *base*.
    """
    base_resolved = base.resolve()
    target = base_resolved.joinpath(*parts).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"path escapes base directory: {parts!r}")
    return target
