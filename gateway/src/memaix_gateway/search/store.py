# SPDX-License-Identifier: AGPL-3.0-or-later
"""EmbeddingStore — SQLite-backed chunk index for semantic + lexical search.

See docs/FEATURE-SEMANTIC-SEARCH.md §4. Vectors are stored as raw float32
bytes; cosine similarity is computed in Python over the ACL-filtered
candidate set (fine for single-tenant vault sizes — see the doc's note on
upgrading to sqlite-vec if that ever stops being true).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import numpy as np


class EmbeddingStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_path(cls, db_path: Path) -> "EmbeddingStore":
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
                CREATE TABLE IF NOT EXISTS chunks (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    project      TEXT NOT NULL,
                    source_type  TEXT NOT NULL,
                    ref          TEXT NOT NULL,
                    chunk_ix     INTEGER NOT NULL,
                    title        TEXT NOT NULL DEFAULT '',
                    text         TEXT NOT NULL,
                    dim          INTEGER NOT NULL,
                    vector       BLOB,
                    updated_at   TEXT NOT NULL,
                    UNIQUE(project, source_type, ref, chunk_ix)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_scope "
                "ON chunks(project, source_type, ref)"
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, title, project UNINDEXED, source_type UNINDEXED, ref UNINDEXED,
                    tokenize='porter unicode61'
                )
                """
            )
            conn.commit()

    def _now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def replace_chunks(self, project: str, source_type: str, ref: str, chunks: list[dict]) -> None:
        """Replace all chunks for (project, source_type, ref) with *chunks*.

        Each chunk: {"chunk_ix": int, "title": str, "text": str,
        "vector": list[float] | None}.
        """
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE project=? AND source_type=? AND ref=?",
                (project, source_type, ref),
            )
            conn.execute(
                "DELETE FROM chunks_fts WHERE project=? AND source_type=? AND ref=?",
                (project, source_type, ref),
            )
            for c in chunks:
                vector = c.get("vector")
                blob = np.asarray(vector, dtype=np.float32).tobytes() if vector else None
                dim = len(vector) if vector else 0
                conn.execute(
                    """
                    INSERT INTO chunks
                        (project, source_type, ref, chunk_ix, title, text, dim, vector, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (project, source_type, ref, c["chunk_ix"], c.get("title", ""),
                     c["text"], dim, blob, now),
                )
                conn.execute(
                    "INSERT INTO chunks_fts (text, title, project, source_type, ref) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (c["text"], c.get("title", ""), project, source_type, ref),
                )
            conn.commit()

    def delete(self, project: str, source_type: str, ref: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE project=? AND source_type=? AND ref=?",
                (project, source_type, ref),
            )
            conn.execute(
                "DELETE FROM chunks_fts WHERE project=? AND source_type=? AND ref=?",
                (project, source_type, ref),
            )
            conn.commit()

    def candidates(self, projects: list[str], source_types: list[str], limit: int) -> list[dict]:
        """Return chunks with a non-null vector, for cosine ranking."""
        if not projects or not source_types:
            return []
        with self._lock, self._connect() as conn:
            pph = ",".join("?" for _ in projects)
            sph = ",".join("?" for _ in source_types)
            rows = conn.execute(
                f"SELECT * FROM chunks WHERE project IN ({pph}) AND source_type IN ({sph}) "  # nosec B608 -- pph/sph are "?,?,..." count strings, values are bound
                "AND vector IS NOT NULL LIMIT ?",
                (*projects, *source_types, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["vector"] = np.frombuffer(d["vector"], dtype=np.float32)
            out.append(d)
        return out

    def fts_search(self, projects: list[str], source_types: list[str], query: str, limit: int) -> list[dict]:
        if not projects or not source_types or not query.strip():
            return []
        with self._lock, self._connect() as conn:
            pph = ",".join("?" for _ in projects)
            sph = ",".join("?" for _ in source_types)
            try:
                rows = conn.execute(
                    f"SELECT project, source_type, ref, title, text "
                    f"FROM chunks_fts WHERE chunks_fts MATCH ? "
                    f"AND project IN ({pph}) AND source_type IN ({sph}) LIMIT ?",  # nosec B608 -- pph/sph are "?,?,..." count strings, values are bound
                    (query, *projects, *source_types, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    def get_ref_updated_at(self, project: str, source_type: str, ref: str) -> str | None:
        """Return MAX(updated_at) for a (project, source_type, ref) triple, or None."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) as updated_at FROM chunks "
                "WHERE project=? AND source_type=? AND ref=?",
                (project, source_type, ref),
            ).fetchone()
        return row["updated_at"] if row and row["updated_at"] else None

    def count_by_project(self, projects: list[str]) -> dict[str, int]:
        if not projects:
            return {}
        with self._lock, self._connect() as conn:
            pph = ",".join("?" for _ in projects)
            rows = conn.execute(
                f"SELECT project, COUNT(*) as n FROM chunks WHERE project IN ({pph}) "  # nosec B608 -- pph is a "?,?,..." count string, values are bound
                "GROUP BY project",
                projects,
            ).fetchall()
        counts = dict.fromkeys(projects, 0)
        for r in rows:
            counts[r["project"]] = r["n"]
        return counts
