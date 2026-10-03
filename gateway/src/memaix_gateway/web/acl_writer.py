# SPDX-License-Identifier: AGPL-3.0-or-later
"""AclWriter — atomic acl.yaml mutation with backup rotation (MEX-025 Fas D).

Every write: load current YAML → mutate → in-place write (same inode, .bak1 first)
with the 3 previous versions kept as .bak1/.bak2/.bak3. Callers MUST call
server.reload_acl() after a successful write so the running gateway sees the
change (the Acl is cached in a module global). Lockout guards (self-disable,
last admin) live in the web route, not here — this class only writes.

Never write passwords, tokens or TOTP secrets in cleartext — only *_ref
values (docs/SECRETS.md).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

_BACKUPS = 3


class AclWriter:
    """Atomic writer for acl.yaml."""

    def __init__(self, acl_path: Path) -> None:
        self._path = Path(acl_path)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def set_user_disabled(self, uid: str, disabled: bool) -> None:
        with self._lock:
            data = self._load()
            user = data.setdefault("users", {}).setdefault(uid, {})
            if disabled:
                user["disabled"] = True
            else:
                user.pop("disabled", None)
            self._write_atomic(data)

    def set_grants(self, uid: str, grants: dict[str, str]) -> None:
        with self._lock:
            data = self._load()
            data.setdefault("users", {}).setdefault(uid, {})["grants"] = dict(grants)
            self._write_atomic(data)

    def set_user_field(self, uid: str, key: str, value: Any) -> None:
        """Set a single user field (e.g. totp_secret_ref). Value must never be
        a cleartext secret — refs only."""
        with self._lock:
            data = self._load()
            data.setdefault("users", {}).setdefault(uid, {})[key] = value
            self._write_atomic(data)

    def set_project_field(self, project: str, key: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            projects = data.setdefault("projects", {})
            if project not in projects:
                raise KeyError(f"unknown project: {project}")
            projects[project][key] = value
            self._write_atomic(data)

    def update(self, fn) -> Any:
        """Run ``fn(data)`` on the freshly loaded YAML under the write lock and
        persist the result. ``fn`` may raise to abort without writing, which
        makes check-then-write sequences atomic."""
        with self._lock:
            data = self._load()
            result = fn(data)
            self._write_atomic(data)
            return result

    def set_top_level(self, key: str, value: Any) -> None:
        """Set — eller med value=None: ta bort — en toppnivåsektion. Används
        för memaix.yaml:s model-block (admin_llm); acl.yaml-mutationer har
        egna metoder ovan."""
        with self._lock:
            data = self._load()
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            self._write_atomic(data)

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        return yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}

    def _write_atomic(self, data: dict) -> None:
        """Rewrite acl.yaml in place; keeps .bak1 (newest) … .bak3 (oldest).

        The current acl.yaml is COPIED into .bak1 first, so a crash
        mid-write always leaves a good previous version to restore."""
        import shutil

        # Rotate backups: bak2→bak3, bak1→bak2, current→(copy)→bak1.
        for i in range(_BACKUPS - 1, 0, -1):
            src = self._path.with_suffix(f".yaml.bak{i}")
            dst = self._path.with_suffix(f".yaml.bak{i + 1}")
            if src.exists():
                os.replace(src, dst)
        if self._path.exists():
            shutil.copy2(self._path, self._path.with_suffix(".yaml.bak1"))

        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        # In place, not os.replace: docker bind-mounts a single file by inode, so
        # a replaced acl.yaml would leave login-app reading the old one forever.
        # .bak1 above is the recovery copy if a crash lands mid-write.
        mode = "r+" if self._path.exists() else "w"
        with open(self._path, mode, encoding="utf-8") as fh:
            fh.seek(0)
            fh.write(text)
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())
