# SPDX-License-Identifier: AGPL-3.0-or-later
"""files_* tools — local-vault backend for Fas 1 (stdio).

All functions call acl.enforce() before touching the filesystem.
Path traversal is blocked via resolve() + relative_to() guard.
"""

from __future__ import annotations

from pathlib import Path

from .. import vault_fs
from ..acl import Acl

# Kept as a name here for callers/tests; the limit itself lives in vault_fs.
MAX_WRITE_BYTES = vault_fs.MAX_WRITE_BYTES


def _vault(acl: Acl, project: str) -> Path:
    vd = acl.resource(project, "vault")
    if not vd:
        raise ValueError(f"project {project!r} has no vault configured")
    return Path(vd)


def _safe(vault: Path, rel: str) -> Path:
    """Resolve rel inside vault via the shared vault_fs check (ValueError).

    user_facing: also refuses per-user/system state at the vault root.
    """
    if not isinstance(rel, str):
        raise ValueError("invalid path")
    return vault_fs.check(vault, vault / rel.lstrip("/"), user_facing=True)


def list_files(acl: Acl, user_id: str, project: str, path: str = "/") -> list[dict]:
    """List files and directories at path inside project vault."""
    acl.enforce(user_id, project, "collaborator")
    vault = _vault(acl, project)
    target = _safe(vault, path)
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
            for p in vault_fs.iter_dir(vault, target, user_facing=True)
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
    return vault_fs.read_text(vault, target, user_facing=True)


def write_file(acl: Acl, user_id: str, project: str, path: str, content: str) -> str:
    """Write text content to a file inside project vault."""
    acl.enforce(user_id, project, "collaborator")
    vault = _vault(acl, project)
    target = _safe(vault, path)
    vault_fs.write_text(vault, target, content, user_facing=True)
    return f"ok: {path}"


def search_files(
    acl: Acl, user_id: str, project: str, query: str, path: str = "/"
) -> list[dict]:
    """Case-insensitive line search across all text files under path."""
    acl.enforce(user_id, project, "collaborator")
    vault = _vault(acl, project)
    target = _safe(vault, path)
    lq = query.lower()
    results: list[dict] = []
    candidates = vault_fs.iter_files(vault, target, recursive=True, user_facing=True) if target.is_dir() else [target]
    for f in candidates:
        if not f.is_file():
            continue
        try:
            text = vault_fs.read_text(vault, f, user_facing=True)
        except (UnicodeDecodeError, OSError):
            continue
        hits = [
            {"line": i + 1, "text": line}
            for i, line in enumerate(text.splitlines())
            if lq in line.lower()
        ]
        if hits:
            results.append({"path": str(f.relative_to(vault.resolve())), "matches": hits})
    return results
