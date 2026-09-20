# SPDX-License-Identifier: AGPL-3.0-or-later
"""ActionQueue — SQLite-backed queue for outgoing actions awaiting approval.

See docs/FEATURE-APPROVAL-OUTBOX.md. Mirrors the SQLite conventions used
elsewhere in the gateway (WAL mode, a single threading.Lock per instance, a
fresh connection per operation) — see backends/token_store.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..paths import data_dir as _data_dir

TERMINAL_STATUSES = frozenset({"rejected", "executed", "failed", "expired"})


class ActionQueue:
    """One instance per deployment — shared across all projects."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_path(cls, db_path: Path) -> "ActionQueue":
        return cls(db_path)

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id           TEXT PRIMARY KEY,
                    memaix_user  TEXT NOT NULL,
                    project      TEXT NOT NULL,
                    tool         TEXT NOT NULL,
                    args_json    TEXT NOT NULL,
                    preview      TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    created_at   TEXT NOT NULL,
                    expires_at   TEXT NOT NULL,
                    decided_by   TEXT,
                    decided_at   TEXT,
                    result_json  TEXT,
                    reason       TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_scope "
                "ON pending_actions(project, status)"
            )
            conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self, user: str, project: str, tool: str, args: dict, preview: str, ttl_h: int = 72
    ) -> str:
        action_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=ttl_h)).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_actions
                    (id, memaix_user, project, tool, args_json, preview,
                     status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (action_id, user, project, tool, json.dumps(args), preview, self._now(), expires_at),
            )
            conn.commit()
        return action_id

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["args"] = json.loads(d.pop("args_json"))
        result_json = d.pop("result_json")
        d["result"] = json.loads(result_json) if result_json else None
        return d

    def get(self, action_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE id=?", (action_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, projects: list[str], status: str | None = None) -> list[dict]:
        if not projects:
            return []
        with self._lock, self._connect() as conn:
            placeholders = ",".join("?" for _ in projects)
            sql = f"SELECT * FROM pending_actions WHERE project IN ({placeholders})"  # nosec B608 -- placeholders is a "?,?,..." count string, values are bound below
            params: list = list(projects)
            if status:
                sql += " AND status=?"
                params.append(status)
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def claim_for_decision(
        self, action_id: str, decision: str, decided_by: str, reason: str = ""
    ) -> dict | None:
        """Atomically move a 'pending' action to *decision*. Returns the updated
        row, or None if the action doesn't exist or was already decided
        (idempotency: a second approve/reject on the same id is a no-op)."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE pending_actions
                SET status=?, decided_by=?, decided_at=?, reason=?
                WHERE id=? AND status='pending'
                """,
                (decision, decided_by, self._now(), reason, action_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE id=?", (action_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def record_result(self, action_id: str, status: str, result: dict) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE pending_actions SET status=?, result_json=? WHERE id=?",
                (status, json.dumps(result), action_id),
            )
            conn.commit()

    def expire_due(self, now_iso: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE pending_actions SET status='expired' "
                "WHERE status='pending' AND expires_at < ?",
                (now_iso,),
            )
            conn.commit()
            return cur.rowcount


_default_instance: "ActionQueue | None" = None


def default_queue() -> "ActionQueue":
    """Process-wide singleton, used when a tool isn't given an explicit queue.

    Only ever touched when an action actually needs to be queued (mode
    'review') — tools running with the default 'auto' mode never construct
    this, so it doesn't affect existing callers/tests.
    """
    global _default_instance
    if _default_instance is None:
        db_path = Path(os.environ.get("MEMAIX_OUTBOX_DB", str(_data_dir() / "memaix-outbox.db")))
        _default_instance = ActionQueue.for_path(db_path)
    return _default_instance


def _reset_default_queue() -> None:
    """For testing only — reset the singleton registry."""
    global _default_instance
    _default_instance = None
