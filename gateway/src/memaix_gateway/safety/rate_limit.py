# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sliding-window rate limiter.

Two interchangeable backends with the same interface:
  RateLimiter        — in-memory deque, process-local (default; fast).
  SQLiteRateLimiter  — shared SQLite table, survives restart and works across
                       multiple workers/processes.

Select via env (see make_rate_limiter): MEMAIX_RATELIMIT_BACKEND=memory|sqlite,
MEMAIX_RATELIMIT_DB=<path>.  For very high multi-process throughput swap in a
Redis backend implementing the same three public methods.

Defaults (SAFETY.md §3):
  user    — 60 calls / 60 s
  project — 120 calls / 60 s
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from ..paths import data_dir as _data_dir


class RateLimiter:
    """Thread-safe sliding-window rate limiter."""

    def __init__(self) -> None:
        self._windows: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_s: int) -> bool:
        """Return True if the call is within rate, False if the limit is exceeded.

        Calling this method always counts as one call attempt (timestamps are
        appended on the first successful check, not on denial).
        """
        now = time.monotonic()
        cutoff = now - window_s
        with self._lock:
            if key not in self._windows:
                self._windows[key] = deque()
            dq = self._windows[key]
            # Evict expired timestamps
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

    def check_user(self, user_id: str) -> bool:
        """60 calls per 60 s per user."""
        return self.check(f"user:{user_id}", limit=60, window_s=60)

    def check_project(self, project: str) -> bool:
        """120 calls per 60 s per project."""
        return self.check(f"project:{project}", limit=120, window_s=60)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def _inject_timestamps(self, key: str, timestamps: list[float]) -> None:
        """For testing: pre-populate a window with known timestamps."""
        with self._lock:
            self._windows[key] = deque(timestamps)

    def _get_timestamps(self, key: str) -> list[float]:
        """For testing: inspect the current window."""
        with self._lock:
            return list(self._windows.get(key, []))

    def _reset(self) -> None:
        """For testing: forget every window.

        The module-level `rate_limiter` is a singleton, so without this one
        test's requests count against the next one's budget — a test that
        adds a few HTTP calls then fails an unrelated test further down the
        file with a 429, which reads as a real regression and isn't one.
        """
        with self._lock:
            self._windows.clear()


class SQLiteRateLimiter:
    """Sliding-window limiter backed by a shared SQLite table.

    Same interface as RateLimiter, but state is persisted and visible to every
    process pointing at the same DB file — so limits hold across a multi-worker
    deployment and survive a restart.  Uses wall-clock time (time.time) because
    monotonic clocks are not comparable across processes.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rate_events (key TEXT NOT NULL, ts REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rate_events_key ON rate_events(key, ts)"
            )
            conn.commit()

    def check(self, key: str, limit: int, window_s: int) -> bool:
        now = time.time()
        cutoff = now - window_s
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM rate_events WHERE key=? AND ts<=?", (key, cutoff))
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM rate_events WHERE key=?", (key,)
            ).fetchone()
            if count >= limit:
                conn.commit()
                return False
            conn.execute("INSERT INTO rate_events (key, ts) VALUES (?, ?)", (key, now))
            conn.commit()
            return True

    def check_user(self, user_id: str) -> bool:
        return self.check(f"user:{user_id}", limit=60, window_s=60)

    def check_project(self, project: str) -> bool:
        return self.check(f"project:{project}", limit=120, window_s=60)

    # --- test helpers (interface parity with RateLimiter) ---

    def _inject_timestamps(self, key: str, timestamps: list[float]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM rate_events WHERE key=?", (key,))
            conn.executemany(
                "INSERT INTO rate_events (key, ts) VALUES (?, ?)",
                [(key, t) for t in timestamps],
            )
            conn.commit()

    def _get_timestamps(self, key: str) -> list[float]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT ts FROM rate_events WHERE key=? ORDER BY ts", (key,)
            ).fetchall()
        return [r[0] for r in rows]


def make_rate_limiter():
    """Build the rate limiter from env (default: in-memory)."""
    backend = os.environ.get("MEMAIX_RATELIMIT_BACKEND", "memory").strip().lower()
    if backend == "sqlite":
        db = Path(os.environ.get("MEMAIX_RATELIMIT_DB", str(_data_dir() / "memaix-ratelimit.db")))
        return SQLiteRateLimiter(db)
    return RateLimiter()


# Module-level default instance used by server.py
rate_limiter = make_rate_limiter()
