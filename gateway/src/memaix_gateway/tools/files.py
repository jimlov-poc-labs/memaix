# SPDX-License-Identifier: AGPL-3.0-or-later
"""files_* tools — local-vault backend for Fas 1 (stdio).

All functions call acl.enforce() before touching the filesystem.
Path traversal is blocked via resolve() + relative_to() guard.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from ..acl import Acl


def _vault(acl: Acl, project: str) -> Path:
    vd = acl.resource(project, "vault")
    if not vd:
        raise ValueError(f"project {project!r} has no vault configured")
    return Path(vd)


# Names under the vault root that files_* must never reach. `.git` is code
# execution (a writable .git/config runs core.fsmonitor as the gateway);
# the per-user directories hold other members' secrets (iCal URLs, booking
# settings); `.memaix.db*` and `_system` are the gateway's own state.
_RESERVED_ROOT = frozenset(
    {
        "_system",
        "calendar_sources",
        "calendar_cache",
        "calendar_overrides",
        "calendar_event_overrides",
        "booking_settings",
        "meeting_forms",
        "meeting_types",
        "native_calendar",
        "working_hours",
    }
)

# 1 MiB of UTF-8. Notes are text; this stops a collaborator filling the disk.
MAX_WRITE_BYTES = 1024 * 1024

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))


def _norm(part: str) -> str:
    """Fold a path component the way a filesystem might: case, Unicode
    compatibility forms, ignorable characters, trailing dots/spaces (NTFS)."""
    folded = unicodedata.normalize("NFKC", part).translate(_INVISIBLE)
    return folded.casefold().rstrip(". ")


def _is_git_name(part: str) -> bool:
    n = _norm(part)
    return n.startswith(".git") or n == "git~1" or n.startswith(".memaix.db")


def _check_parts(parts: tuple[str, ...], rel: str) -> None:
    """Reject if any component is git state or (at the root) system state."""
    for i, part in enumerate(parts):
        if _is_git_name(part) or (i == 0 and _norm(part) in _RESERVED_ROOT):
            raise ValueError(f"path is reserved: {rel!r}")


def is_reserved_rel(rel: str) -> bool:
    """True if a vault-relative path is git state or system/per-user state.

    Shared with the search indexer so both use one list and one normalisation.
    """
    parts = tuple(x for x in rel.replace("\\", "/").split("/") if x not in ("", "."))
    try:
        _check_parts(parts, rel)
    except ValueError:
        return True
    return False


def _is_profile(inside: tuple[str, ...]) -> bool:
    """shared/om-<user>.md: the profile whoami feeds to the model."""
    if len(inside) != 2 or _norm(inside[0]) != "shared":
        return False
    name = _norm(inside[1])
    return name.startswith("om-") and name.endswith(".md")


def _is_reserved(vault_resolved: Path, p: Path) -> bool:
    try:
        _check_parts(p.resolve().relative_to(vault_resolved).parts, str(p))
    except ValueError:
        return True
    return False


def _safe(vault: Path, rel: str) -> Path:
    """Resolve rel inside vault; raise ValueError if it escapes the vault or
    touches reserved paths (.git at any depth, per-user and system state).

    The raw components are checked first (so `x/../.git` and `.GIT` fail
    even if they would normalise away), then the symlink-resolved path.
    """
    if not isinstance(rel, str) or "\0" in rel:
        raise ValueError("invalid path")
    cleaned = rel.replace("\\", "/")
    raw_parts = tuple(x for x in cleaned.split("/") if x not in ("", "."))
    _check_parts(tuple(x for x in raw_parts if x != ".."), rel)
    vault_resolved = vault.resolve()
    resolved = (vault_resolved / cleaned.lstrip("/")).resolve()
    try:
        inside = resolved.relative_to(vault_resolved)
    except ValueError:
        raise ValueError(f"path outside vault: {rel!r}")
    _check_parts(inside.parts, rel)
    return resolved


def list_files(acl: Acl, user_id: str, project: str, path: str = "/") -> list[dict]:
    """List files and directories at path inside project vault."""
    acl.enforce(user_id, project, "collaborator")
    vault = _vault(acl, project)
    target = _safe(vault, path)
    vault_resolved = vault.resolve()
    if not target.exists():
        return []
    if target.is_file():
        return [{"name": target.name, "type": "file", "size": target.stat().st_size}]
    return sorted(
        [
            {
                "name": p.name,
                "type": "dir" if p.is_dir() else "file",
                "size": p.stat().st_size if p.is_file() else None,
            }
            for p in target.iterdir()
            if not _is_reserved(vault_resolved, p)
        ],
        key=lambda e: (e["type"] == "file", e["name"]),
    )


def read_file(acl: Acl, user_id: str, project: str, path: str) -> str:
    """Read text content of a file inside project vault."""
    acl.enforce(user_id, project, "collaborator")
    vault = _vault(acl, project)
    target = _safe(vault, path)
    if not target.is_file():
        raise FileNotFoundError(f"not a file: {path!r}")
    return target.read_text()


def write_file(acl: Acl, user_id: str, project: str, path: str, content: str) -> str:
    """Write text content to a file inside project vault."""
    acl.enforce(user_id, project, "collaborator")
    vault = _vault(acl, project)
    target = _safe(vault, path)
    if _is_profile(target.relative_to(vault.resolve()).parts):
        # Profiles are written by the onboarding tool only; whoami reads them,
        # so a collaborator overwriting one could inject into another's session.
        raise ValueError(f"path is reserved: {path!r}")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError(f"content exceeds {MAX_WRITE_BYTES} bytes")
    if target.is_dir():
        raise ValueError(f"is a directory: {path!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"ok: {path}"


def search_files(
    acl: Acl, user_id: str, project: str, query: str, path: str = "/"
) -> list[dict]:
    """Case-insensitive line search across all text files under path."""
    acl.enforce(user_id, project, "collaborator")
    vault = _vault(acl, project)
    target = _safe(vault, path)
    vault_resolved = vault.resolve()
    lq = query.lower()
    results: list[dict] = []
    candidates = target.rglob("*") if target.is_dir() else [target]
    for f in candidates:
        if _is_reserved(vault_resolved, f) or not f.is_file():
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        hits = [
            {"line": i + 1, "text": line}
            for i, line in enumerate(text.splitlines())
            if lq in line.lower()
        ]
        if hits:
            results.append({"path": str(f.resolve().relative_to(vault_resolved)), "matches": hits})
    return results
