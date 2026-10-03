# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-use invitation tokens for set-your-own-password links.

Only the SHA-256 of a token is stored, so a copy of the database cannot be
turned into working links. One open invite per user: issuing a new one
replaces the old.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DEFAULT_TTL_S = 7 * 24 * 3600


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InviteStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        with closing(self._connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS invites ("
                "token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE, "
                "created_by TEXT NOT NULL, expires_at REAL NOT NULL)"
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)

    def issue(self, user_id: str, created_by: str, ttl_s: int = DEFAULT_TTL_S) -> str:
        token = secrets.token_urlsafe(32)
        with closing(self._connect()) as db:
            db.execute("DELETE FROM invites WHERE user_id = ?", (user_id,))
            db.execute(
                "INSERT INTO invites VALUES (?, ?, ?, ?)",
                (_digest(token), user_id, created_by, time.time() + ttl_s),
            )
            db.commit()
        return token

    def peek(self, token: str) -> str | None:
        """Return the user id for a live token without consuming it."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT user_id, expires_at FROM invites WHERE token_hash = ?",
                (_digest(token),),
            ).fetchone()
        if row is None or row[1] < time.time():
            return None
        return row[0]

    def consume(self, token: str) -> str | None:
        """Atomically burn a live token and return its user id."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT user_id, expires_at FROM invites WHERE token_hash = ?",
                (_digest(token),),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            db.execute("DELETE FROM invites WHERE token_hash = ?", (_digest(token),))
            db.commit()
        return row[0] if row[1] >= time.time() else None

    def revoke_user(self, user_id: str) -> None:
        with closing(self._connect()) as db:
            db.execute("DELETE FROM invites WHERE user_id = ?", (user_id,))
            db.commit()
