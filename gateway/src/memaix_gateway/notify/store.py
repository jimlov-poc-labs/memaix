# SPDX-License-Identifier: AGPL-3.0-or-later
"""NotifyStore — per-user brief preferences, schedule and delivery idempotency.

See docs/FEATURE-PROACTIVE-BRIEF.md §3.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class NotifyStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_path(cls, db_path: Path) -> "NotifyStore":
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
                CREATE TABLE IF NOT EXISTS notify_prefs (
                    memaix_user  TEXT PRIMARY KEY,
                    enabled      INTEGER NOT NULL DEFAULT 0,
                    timezone     TEXT NOT NULL DEFAULT 'UTC',
                    brief_time   TEXT NOT NULL DEFAULT '07:00',
                    quiet_start  TEXT,
                    quiet_end    TEXT,
                    channels     TEXT NOT NULL DEFAULT '[]',
                    projects     TEXT NOT NULL DEFAULT '[]',
                    updated_at   TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS brief_schedule (
                    memaix_user  TEXT NOT NULL,
                    slot         TEXT NOT NULL,
                    next_run     INTEGER NOT NULL,
                    last_run     INTEGER,
                    PRIMARY KEY (memaix_user, slot)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS brief_sent (
                    idem_key     TEXT PRIMARY KEY,
                    sent_at      TEXT NOT NULL
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Prefs
    # ------------------------------------------------------------------

    def get_prefs(self, user: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notify_prefs WHERE memaix_user=?", (user,)
            ).fetchone()
        return self._prefs_row_to_dict(row) if row else None

    def all_channels(self) -> list[list[dict]]:
        """Every user's brief channel list (for the unlock-channel check)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT channels FROM notify_prefs").fetchall()
        out: list[list[dict]] = []
        for row in rows:
            try:
                out.append(json.loads(row[0]) or [])
            except (TypeError, ValueError):
                continue
        return out

    def _prefs_row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["channels"] = json.loads(d["channels"])
        d["projects"] = json.loads(d["projects"])
        return d

    def set_prefs(self, user: str, *, now_iso: str, **fields) -> dict:
        """Upsert preferences. Unset fields keep their existing/default value."""
        existing = self.get_prefs(user) or {
            "enabled": False, "timezone": "UTC", "brief_time": "07:00",
            "quiet_start": None, "quiet_end": None, "channels": [], "projects": [],
        }
        merged = {**existing, **{k: v for k, v in fields.items() if v is not None}}
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notify_prefs
                    (memaix_user, enabled, timezone, brief_time, quiet_start,
                     quiet_end, channels, projects, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memaix_user) DO UPDATE SET
                    enabled=excluded.enabled, timezone=excluded.timezone,
                    brief_time=excluded.brief_time, quiet_start=excluded.quiet_start,
                    quiet_end=excluded.quiet_end, channels=excluded.channels,
                    projects=excluded.projects, updated_at=excluded.updated_at
                """,
                (
                    user, 1 if merged["enabled"] else 0, merged["timezone"],
                    merged["brief_time"], merged.get("quiet_start"), merged.get("quiet_end"),
                    json.dumps(merged["channels"]), json.dumps(merged["projects"]), now_iso,
                ),
            )
            conn.commit()
        prefs = self.get_prefs(user)
        assert prefs is not None  # just upserted
        return prefs

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------

    def upsert_schedule(self, user: str, slot: str, next_run: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO brief_schedule (memaix_user, slot, next_run)
                VALUES (?, ?, ?)
                ON CONFLICT(memaix_user, slot) DO UPDATE SET next_run=excluded.next_run
                """,
                (user, slot, next_run),
            )
            conn.commit()

    def get_schedule(self, user: str, slot: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM brief_schedule WHERE memaix_user=? AND slot=?", (user, slot)
            ).fetchone()
        return dict(row) if row else None

    def due(self, now: int) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM brief_schedule WHERE next_run <= ?", (now,)
            ).fetchall()
        return [dict(r) for r in rows]

    def claim(self, user: str, slot: str, old_next: int, new_next: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE brief_schedule SET next_run=? "
                "WHERE memaix_user=? AND slot=? AND next_run=?",
                (new_next, user, slot, old_next),
            )
            conn.commit()
            return cur.rowcount == 1

    def mark_run(self, user: str, slot: str, last_run: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE brief_schedule SET last_run=? WHERE memaix_user=? AND slot=?",
                (last_run, user, slot),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def already_sent(self, idem_key: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM brief_sent WHERE idem_key=?", (idem_key,)
            ).fetchone()
        return row is not None

    def record_sent(self, idem_key: str, sent_at: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO brief_sent (idem_key, sent_at) VALUES (?, ?)",
                (idem_key, sent_at),
            )
            conn.commit()
