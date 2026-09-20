# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encrypted per-user OAuth token store backed by SQLite.

Master key is supplied externally (env: TOKEN_MASTER_KEY).
Fernet provides AES-128-CBC + HMAC-SHA256 authenticated encryption.
Thread-safe via a single Lock per instance.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


class TokenStore:
    """One instance per deployment — shared across all projects."""

    def __init__(self, db_path: Path, fernet: Fernet) -> None:
        self._path = db_path
        self._fernet = fernet
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_path(cls, db_path: Path, master_key: bytes) -> "TokenStore":
        fernet = Fernet(master_key)
        return cls(db_path, fernet)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_tokens (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        memaix_user   TEXT NOT NULL,
                        provider      TEXT NOT NULL,
                        account_email TEXT NOT NULL,
                        encrypted_data BLOB NOT NULL,
                        status        TEXT NOT NULL DEFAULT 'active',
                        updated_at    TEXT NOT NULL,
                        UNIQUE(memaix_user, provider, account_email)
                    )
                    """
                )
                # Which projects may use a linked account, per capability.
                # A row's presence IS the grant — absence means "not allowed"
                # (opt-in: a freshly linked account is visible to no project
                # until scoped).  project='*' grants every project the user
                # has access to, including ones created later; the ACL still
                # gates whether they can reach the project at all, this only
                # decides whether the account joins that project's sources.
                # capability is always concrete ('mail'/'calendar'), never a
                # wildcard, so granting one capability can't silently widen
                # another.
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS account_scopes (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        memaix_user   TEXT NOT NULL,
                        provider      TEXT NOT NULL,
                        account_email TEXT NOT NULL,
                        capability    TEXT NOT NULL,
                        project       TEXT NOT NULL,
                        updated_at    TEXT NOT NULL,
                        UNIQUE(memaix_user, provider, account_email, capability, project)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_account_scopes_lookup "
                    "ON account_scopes (memaix_user, provider, account_email, capability)"
                )
                # One-shot migration markers (see backfill_scopes_once).
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store(self, user: str, provider: str, account: str, token_data: dict) -> None:
        """Encrypt and store (or overwrite) token_data. Status is set to 'active'."""
        blob = self._fernet.encrypt(json.dumps(token_data).encode())
        now = self._now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_tokens
                        (memaix_user, provider, account_email, encrypted_data, status, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?)
                    ON CONFLICT(memaix_user, provider, account_email)
                    DO UPDATE SET
                        encrypted_data = excluded.encrypted_data,
                        status         = 'active',
                        updated_at     = excluded.updated_at
                    """,
                    (user, provider, account, blob, now),
                )
                conn.commit()

    def load_one(self, user: str, provider: str, account: str) -> dict | None:
        """Decrypt and return token_data, or None if not found."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT encrypted_data FROM user_tokens
                    WHERE memaix_user=? AND provider=? AND account_email=?
                    """,
                    (user, provider, account),
                ).fetchone()
        if row is None:
            return None
        return json.loads(self._fernet.decrypt(bytes(row["encrypted_data"])))

    def list_accounts(self, user: str) -> list[dict]:
        """Return [{provider, account, status, scopes}] for all linked accounts."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT provider, account_email, encrypted_data, status
                    FROM user_tokens
                    WHERE memaix_user=?
                    ORDER BY provider, account_email
                    """,
                    (user,),
                ).fetchall()
        result = []
        for row in rows:
            token_data = json.loads(self._fernet.decrypt(bytes(row["encrypted_data"])))
            result.append(
                {
                    "provider": row["provider"],
                    "account": row["account_email"],
                    "status": row["status"],
                    "scopes": token_data.get("scope", "").split()
                    if token_data.get("scope")
                    else [],
                }
            )
        return result

    def delete(self, user: str, provider: str, account: str) -> bool:
        """Delete token and any project scopes granted to it.

        Scopes are dropped in the same transaction so an account that is
        unlinked and later re-linked comes back with no grants, rather than
        silently inheriting the grants of its predecessor.
        """
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    DELETE FROM user_tokens
                    WHERE memaix_user=? AND provider=? AND account_email=?
                    """,
                    (user, provider, account),
                )
                conn.execute(
                    """
                    DELETE FROM account_scopes
                    WHERE memaix_user=? AND provider=? AND account_email=?
                    """,
                    (user, provider, account),
                )
                conn.commit()
                return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Project scoping
    # ------------------------------------------------------------------

    def set_scopes(
        self,
        user: str,
        provider: str,
        account: str,
        capability: str,
        projects: list[str],
    ) -> list[str]:
        """Replace this account's project grants for one capability.

        Replace, not merge: the caller sends the complete desired set, so
        passing [] revokes the capability entirely.  That makes the settings
        UI a plain checkbox matrix — it POSTs what's ticked, without having
        to diff against what was there before.

        '*' anywhere in `projects` collapses to the single wildcard row; a
        mixed ['*', 'jimlov'] would otherwise leave a redundant row behind
        that survives the wildcard being unticked later.

        Returns the stored project list.
        """
        if not capability:
            raise ValueError("capability is required")
        wanted = ["*"] if "*" in projects else sorted(set(projects))
        now = self._now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM account_scopes
                    WHERE memaix_user=? AND provider=? AND account_email=? AND capability=?
                    """,
                    (user, provider, account, capability),
                )
                conn.executemany(
                    """
                    INSERT INTO account_scopes
                        (memaix_user, provider, account_email, capability, project, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [(user, provider, account, capability, p, now) for p in wanted],
                )
                conn.commit()
        return wanted

    def list_scopes(
        self, user: str, provider: str | None = None, account: str | None = None
    ) -> list[dict]:
        """Return [{provider, account, capability, projects}] for this user.

        Rows are folded into one entry per (provider, account, capability)
        because that's the unit the UI and the MCP tools speak in — the
        row-per-project shape is a storage detail.
        """
        sql = (
            "SELECT provider, account_email, capability, project FROM account_scopes "
            "WHERE memaix_user=?"
        )
        params: list[str] = [user]
        if provider is not None:
            sql += " AND provider=?"
            params.append(provider)
        if account is not None:
            sql += " AND account_email=?"
            params.append(account)
        sql += " ORDER BY provider, account_email, capability, project"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        folded: dict[tuple[str, str, str], dict] = {}
        for row in rows:
            key = (row["provider"], row["account_email"], row["capability"])
            entry = folded.get(key)
            if entry is None:
                entry = {
                    "provider": row["provider"],
                    "account": row["account_email"],
                    "capability": row["capability"],
                    "projects": [],
                }
                folded[key] = entry
            entry["projects"].append(row["project"])
        return list(folded.values())

    def is_allowed(
        self, user: str, provider: str, account: str, capability: str, project: str
    ) -> bool:
        """True if this account may serve `capability` in `project`."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT 1 FROM account_scopes
                    WHERE memaix_user=? AND provider=? AND account_email=?
                      AND capability=? AND project IN (?, '*')
                    LIMIT 1
                    """,
                    (user, provider, account, capability, project),
                ).fetchone()
        return row is not None

    def backfill_scopes_once(self, capabilities: list[str]) -> int:
        """Grant every pre-existing linked account '*' for each capability.

        Runs at most once per database, guarded by a schema_meta marker.
        Without it, deploying the scope gate would silence every already-
        linked mailbox and calendar the moment the migration ran — opt-in is
        only safe for accounts linked *after* the feature exists.  The marker
        is what stops a user who deliberately revoked all their scopes from
        having them handed back on the next restart.

        Returns the number of scope rows inserted (0 if already backfilled).
        """
        now = self._now()
        with self._lock:
            with self._connect() as conn:
                done = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='account_scopes_backfilled'"
                ).fetchone()
                if done is not None:
                    return 0
                accounts = conn.execute(
                    "SELECT memaix_user, provider, account_email FROM user_tokens"
                ).fetchall()
                rows = [
                    (a["memaix_user"], a["provider"], a["account_email"], cap, "*", now)
                    for a in accounts
                    for cap in capabilities
                ]
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO account_scopes
                        (memaix_user, provider, account_email, capability, project, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('account_scopes_backfilled', '1')"
                )
                conn.commit()
        return len(rows)

    def mark_needs_relink(self, user: str, provider: str, account: str) -> None:
        """Set status='needs_relink' for this account."""
        now = self._now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE user_tokens SET status='needs_relink', updated_at=?
                    WHERE memaix_user=? AND provider=? AND account_email=?
                    """,
                    (now, user, provider, account),
                )
                conn.commit()
