# SPDX-License-Identifier: AGPL-3.0-or-later
"""files_* tools — local-vault backend for Fas 1 (stdio).

All functions call acl.enforce() before touching the filesystem.
Path traversal is blocked via resolve() + relative_to() guard.
"""

from __future__ import annotations

from pathlib import Path

from ..acl import Acl


def _vault(acl: Acl, project: str) -> Path:
    vd = acl.resource(project, "vault")
    if not vd:
        raise ValueError(f"project {project!r} has no vault configured")
    return Path(vd)


def _safe(vault: Path, rel: str) -> Path:
    """Resolve rel inside vault; raise ValueError if path project-bs."""
    resolved = (vault / rel.lstrip("/")).resolve()
    vault_resolved = vault.resolve()
    try:
        inside = resolved.relative_to(vault_resolved)
    except ValueError:
        raise ValueError(f"path project-bs vault: {rel!r}")
    # Check both the requested and the resolved path: a symlink must not
    # smuggle a request into .git, and `sub/../.git` must not slip past.
    parts = [*Path(rel.lstrip("/")).parts, *inside.parts]
    if any(_is_reserved(c) for c in parts):
        raise ValueError(f"reserved path: {rel!r}")
    return resolved


def _is_reserved(component: str) -> bool:
    """.git (any case) and .memaix* hold repo config and internal state."""
    c = component.lower()
    return c == ".git" or c.startswith(".memaix")


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
            for p in target.iterdir()
            if not _is_reserved(p.name)
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
    lq = query.lower()
    results: list[dict] = []
    candidates = target.rglob("*") if target.is_dir() else [target]
    for f in candidates:
        if not f.is_file():
            continue
        if any(_is_reserved(c) for c in f.relative_to(vault.resolve()).parts):
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
            results.append({"path": str(f.relative_to(vault)), "matches": hits})
    return results
