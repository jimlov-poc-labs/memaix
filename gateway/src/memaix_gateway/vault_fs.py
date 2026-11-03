# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one way to touch the filesystem inside a vault.

Every tool, backend and indexer used to guard (or forget to guard) its own
path. Each guard looked at something slightly different -- the requested
path, the resolved path, only the target and not what a walk finds below it
-- and every gap was a way into `.git/config` (whose `core.fsmonitor` runs
commands) or `.memaix.db`. So there is exactly one check now:

* the path, *as given* and *after resolving symlinks*, must lie inside the
  vault, and
* no component of either may be reserved: `.git`, `.gitignore` (at any
  depth: a nested one can negate the `.memaix*` exclusion), or `.memaix*`.

Writes additionally open with O_NOFOLLOW, so a link swapped in after the
check is refused rather than followed. Fail closed: anything that cannot be
resolved is blocked.

Callers that only need a path decision use `check`/`is_blocked`; everything
that reads, writes or enumerates uses `read_text`/`write_text`/`iter_files`/
`iter_dir`.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path


class VaultPathError(ValueError):
    """A path is outside the vault or names reserved state."""


def is_reserved(component: str) -> bool:
    c = component.lower()
    return c in (".git", ".gitignore") or c.startswith(".memaix")


def check(vault: Path, path: Path | str) -> Path:
    """Return the resolved path, or raise VaultPathError."""
    vault = Path(vault)
    root = vault.resolve()
    p = Path(path)
    if not p.is_absolute():
        p = vault / p
    try:
        found = p.relative_to(vault).parts
    except ValueError:
        try:
            found = p.relative_to(root).parts
        except ValueError:
            raise VaultPathError(f"path outside vault: {str(path)!r}") from None
    try:
        resolved = p.resolve()
        real = resolved.relative_to(root).parts
    except (ValueError, OSError, RuntimeError):
        raise VaultPathError(f"path outside vault: {str(path)!r}") from None
    if any(is_reserved(c) for c in (*found, *real)):
        raise VaultPathError(f"reserved path: {str(path)!r}")
    return resolved


def is_blocked(vault: Path, path: Path | str) -> bool:
    try:
        check(vault, path)
    except VaultPathError:
        return True
    return False


def read_text(vault: Path, path: Path | str, encoding: str = "utf-8") -> str:
    resolved = check(vault, path)
    # O_NOFOLLOW: the resolved path has no link in its last component; refuse
    # one swapped in between check and open.
    fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding=encoding) as fh:
        return fh.read()


def exists(vault: Path, path: Path | str) -> bool:
    """False for blocked paths, so a blocked file looks absent."""
    try:
        return check(vault, path).exists()
    except VaultPathError:
        return False


def write_text(vault: Path, path: Path | str, text: str, encoding: str = "utf-8") -> None:
    resolved = check(vault, path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    check(vault, resolved)  # parents created: re-validate
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(resolved, flags, 0o644)
    with os.fdopen(fd, "w", encoding=encoding) as fh:
        fh.write(text)


def iter_dir(vault: Path, base: Path | str) -> list[Path]:
    """Immediate children of *base* that are not blocked."""
    return [p for p in check(vault, base).iterdir() if not is_blocked(vault, p)]


def iter_files(vault: Path, base: Path | str, pattern: str = "*", *, recursive: bool = False) -> list[Path]:
    """Sorted regular files under *base* matching *pattern*; blocked ones omitted.

    Reserved directories are pruned, not walked into.
    """
    top = check(vault, base)
    shown = Path(base) if Path(base).is_absolute() else Path(vault) / base
    out: list[Path] = []
    if not top.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not is_reserved(d))
        for name in sorted(filenames):
            if not fnmatch.fnmatch(name, pattern):
                continue
            # Report under the caller's own spelling of *base* (symlinked
            # tmp/mount prefixes must not change what callers see).
            f = shown / Path(dirpath).relative_to(top) / name
            if f.is_file() and not is_blocked(vault, f):
                out.append(f)
        if not recursive:
            break
    return sorted(out)
