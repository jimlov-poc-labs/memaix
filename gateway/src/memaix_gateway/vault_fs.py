# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one way to touch the filesystem inside a vault.

Every tool, backend and indexer used to guard (or forget to guard) its own
path. Each guard looked at something slightly different -- the requested
path, the resolved path, only the target and not what a walk finds below it
-- and every gap was a way into `.git/config` (whose `core.fsmonitor` runs
commands) or `.memaix.db`. So there is exactly one check now, and one name
list (`is_reserved`, `is_reserved_rel`) that the search index and the tool
layer use too:

* the path, *as given* and *after resolving symlinks*, must lie inside the
  vault, and
* no component of either may be reserved: `.git`, `.gitignore` (at any
  depth: a nested one can negate the `.memaix*` exclusion), `.gitmodules`,
  `.gitattributes` or `.memaix*`. Names are compared *exactly* after
  normalisation (`.github` and `.gitkeep` are ordinary names), where
  normalisation folds what a filesystem folds: Unicode compatibility forms,
  invisible/format characters, case, trailing dots and spaces, NTFS stream
  suffixes and 8.3 short names (`GIT~1`). Backslashes separate components.

Two levels. The default level (backends, onboarding, indexers: code that
legitimately writes `_system/` and `shared/om-<user>.md`) enforces the list
above. `user_facing=True` (the `files_*` tools, which act on behalf of a
collaborator) additionally refuses the per-user and system state directories
at the vault root (iCal URLs, booking settings, ...), refuses writes to the
profile files `whoami` feeds to the model, and caps the size of a write.

Writes open with O_NOFOLLOW, so a link swapped in after the check is refused
rather than followed. Fail closed: anything that cannot be resolved is blocked.

Callers that only need a path decision use `check`/`is_blocked`; everything
that reads, writes or enumerates uses `read_text`/`write_text`/`iter_files`/
`iter_dir`.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import unicodedata
from pathlib import Path


class VaultPathError(ValueError):
    """A path is outside the vault or names reserved state."""


# 1 MiB of UTF-8. Notes are text; this stops a collaborator filling the disk.
MAX_WRITE_BYTES = 1024 * 1024

# Per-user and gateway state directories at the vault root (written by
# connectors/*.py). `files_*` must never reach them: they hold other members'
# iCal URLs and booking settings.
RESERVED_ROOT = frozenset(
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

_GIT_NAMES = frozenset({".git", ".gitignore", ".gitmodules", ".gitattributes"})
# 8.3 short names Windows-like filesystems give the names above (GIT~1,
# GITIGN~1, GITMOD~1, GITATT~1, MEMAIX~1).
_SHORT_RE = re.compile(r"^(git|gitign|gitmod|gitatt|memaix)~\d+(\..*)?$")
_IGNORABLE = ("Cf", "Cc")


def _fold(component: str) -> str:
    """Fold a path component the way a filesystem might."""
    s = unicodedata.normalize("NFKC", component)
    s = "".join(
        ch for ch in s
        if unicodedata.category(ch) not in _IGNORABLE and not 0xFE00 <= ord(ch) <= 0xFE0F
    )
    s = unicodedata.normalize("NFKC", s).casefold()
    s = s.split(":", 1)[0]  # NTFS alternate data stream: ".git::$INDEX_ALLOCATION"
    return s.rstrip(". ")


def is_reserved(component: str) -> bool:
    """True if one path component names git or gateway state (any depth)."""
    c = _fold(component)
    return c in _GIT_NAMES or c.startswith(".memaix") or bool(_SHORT_RE.match(c))


def _split(path: str) -> list[str]:
    """Components of a relative path string; both separators, no '' or '.'."""
    return [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]


def _parts(p: Path) -> tuple[str, ...]:
    out: list[str] = []
    for part in p.parts:
        out.extend(_split(part))
    return tuple(out)


def is_reserved_root(component: str) -> bool:
    """True if *component*, at the vault root, names per-user/system state."""
    return _fold(component) in RESERVED_ROOT


def is_profile(parts: tuple[str, ...]) -> bool:
    """`shared/om-<user>.md`: the profile `whoami` feeds to the model."""
    if len(parts) != 2 or _fold(parts[0]) != "shared":
        return False
    name = _fold(parts[1])
    return name.startswith("om-") and name.endswith(".md")


def is_reserved_rel(rel: str, *, root_state: bool = True) -> bool:
    """Pure-string decision for a vault-relative path (no filesystem access).

    True if any component is reserved, or (root_state) the first one is
    per-user/system state. Used for index refs and anywhere only a string is
    at hand; `..` is resolved lexically, and escaping the root counts as
    reserved. Not a substitute for `check` on a real path.
    """
    if not isinstance(rel, str) or "\0" in rel:
        return True
    raw = _split(rel)
    if any(is_reserved(c) for c in raw):
        return True
    if not root_state:
        return False
    norm = posixpath.normpath("/".join(raw)) if raw else ""
    if norm == ".." or norm.startswith("../"):
        return True
    first = [c for c in norm.split("/") if c not in ("", ".")][:1]
    return bool(first) and is_reserved_root(first[0])


def check(vault: Path, path: Path | str, *, user_facing: bool = False) -> Path:
    """Return the resolved path, or raise VaultPathError."""
    if "\0" in str(path):
        raise VaultPathError("invalid path")
    vault = Path(vault)
    root = vault.resolve()
    p = Path(path)
    if not p.is_absolute():
        p = vault / p
    try:
        found = _parts(p.relative_to(vault))
    except ValueError:
        try:
            found = _parts(p.relative_to(root))
        except ValueError:
            raise VaultPathError(f"path outside vault: {str(path)!r}") from None
    try:
        resolved = p.resolve()
        real = _parts(resolved.relative_to(root))
    except (ValueError, OSError, RuntimeError):
        raise VaultPathError(f"path outside vault: {str(path)!r}") from None
    if any(is_reserved(c) for c in (*found, *real)):
        raise VaultPathError(f"reserved path: {str(path)!r}")
    if user_facing:
        # Both spellings: `dirlink/x` resolves into a state directory, and
        # `calendar_sources/../ok.md` names one on the way.
        if any(c and is_reserved_root(c[0]) for c in (found, real)):
            raise VaultPathError(f"reserved path: {str(path)!r}")
    return resolved


def is_blocked(vault: Path, path: Path | str, *, user_facing: bool = False) -> bool:
    try:
        check(vault, path, user_facing=user_facing)
    except VaultPathError:
        return True
    return False


def read_text(vault: Path, path: Path | str, encoding: str = "utf-8", *, user_facing: bool = False) -> str:
    resolved = check(vault, path, user_facing=user_facing)
    # O_NOFOLLOW: the resolved path has no link in its last component; refuse
    # one swapped in between check and open.
    fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding=encoding) as fh:
        return fh.read()


def exists(vault: Path, path: Path | str, *, user_facing: bool = False) -> bool:
    """False for blocked paths, so a blocked file looks absent."""
    try:
        return check(vault, path, user_facing=user_facing).exists()
    except VaultPathError:
        return False


def write_text(
    vault: Path, path: Path | str, text: str, encoding: str = "utf-8", *, user_facing: bool = False
) -> None:
    resolved = check(vault, path, user_facing=user_facing)
    if user_facing:
        if not isinstance(text, str):
            raise ValueError("content must be a string")
        if len(text.encode(encoding)) > MAX_WRITE_BYTES:
            raise ValueError(f"content exceeds {MAX_WRITE_BYTES} bytes")
        if is_profile(_parts(resolved.relative_to(Path(vault).resolve()))):
            # Written by onboarding only; whoami reads it, so a collaborator
            # overwriting one could inject into another user's session.
            raise VaultPathError(f"reserved path: {str(path)!r}")
        if resolved.is_dir():
            raise ValueError(f"is a directory: {str(path)!r}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    check(vault, resolved, user_facing=user_facing)  # parents created: re-validate
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(resolved, flags, 0o644)
    with os.fdopen(fd, "w", encoding=encoding) as fh:
        fh.write(text)


def iter_dir(vault: Path, base: Path | str, *, user_facing: bool = False) -> list[Path]:
    """Immediate children of *base* that are not blocked."""
    return [
        p for p in check(vault, base, user_facing=user_facing).iterdir()
        if not is_blocked(vault, p, user_facing=user_facing)
    ]


def iter_files(
    vault: Path, base: Path | str, pattern: str = "*", *, recursive: bool = False, user_facing: bool = False
) -> list[Path]:
    """Sorted regular files under *base* matching *pattern*; blocked ones omitted.

    Reserved directories are pruned, not walked into.
    """
    top = check(vault, base, user_facing=user_facing)
    shown = Path(base) if Path(base).is_absolute() else Path(vault) / base
    out: list[Path] = []
    if not top.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if not is_blocked(vault, Path(dirpath) / d, user_facing=user_facing)
        )
        for name in sorted(filenames):
            if not fnmatch.fnmatch(name, pattern):
                continue
            # Report under the caller's own spelling of *base* (symlinked
            # tmp/mount prefixes must not change what callers see).
            f = shown / Path(dirpath).relative_to(top) / name
            if f.is_file() and not is_blocked(vault, f, user_facing=user_facing):
                out.append(f)
        if not recursive:
            break
    return sorted(out)
