# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite-backed memory store with git history for memaix vaults.

Active state lives in {vault}/.memaix.db (notes + FTS5).
History lives in a git repo at the vault root.
Git commits are run synchronously inside write_lock so the returned
snapshot-id is always a real commit hash usable with revert().

TODO(perf): batch async git commits via a queue.Queue + background thread
      once benchmarks show write latency is a problem.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import gitvault, vault_fs

# FTS5 metacharacters that indicate an already-structured query: quotes,
# parentheses, prefix-match wildcard, column filter colon, caret (initial
# token), or the reserved operators written in all-caps as standalone words.
_FTS5_META_RE = re.compile(r'["()*^]|(?<!\w)(AND|OR|NOT|NEAR)(?!\w)')


def _fts_or_query(query: str) -> str:
    """Rewrite a plain space-separated query as FTS5 OR so multi-word searches
    return notes that contain *any* of the terms rather than requiring all.

    A query that already uses FTS5 syntax (quotes, AND/OR/NOT/NEAR, *, ^) is
    returned unchanged — the caller expressed intent we shouldn't second-guess.
    """
    if _FTS5_META_RE.search(query):
        return query
    tokens = query.split()
    if len(tokens) <= 1:
        return query
    return " OR ".join(tokens)


class MemoryStore:
    """Per-vault note store.  One instance per resolved vault path (singleton)."""

    _instances: dict[Path, "MemoryStore"] = {}
    _class_lock = threading.Lock()

    def __init__(self, vault: Path) -> None:
        self.vault = vault.resolve()
        self.db_path = self.vault / ".memaix.db"
        self.memory_dir = self.vault / "memory"
        self.write_lock = threading.RLock()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._conn = self._open_db()
        self._ensure_git()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def for_vault(cls, vault: Path) -> "MemoryStore":
        key = vault.resolve()
        with cls._class_lock:
            if key not in cls._instances:
                cls._instances[key] = cls(key)
            return cls._instances[key]

    @classmethod
    def _clear_instances(cls) -> None:
        """For testing only — reset singleton registry."""
        with cls._class_lock:
            cls._instances.clear()

    # ------------------------------------------------------------------
    # DB setup
    # ------------------------------------------------------------------

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY,
                path       TEXT UNIQUE NOT NULL,
                content    TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                path, content, tokenize='porter unicode61'
            );
            """
        )
        conn.commit()
        return conn

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        return gitvault.run(self.vault, args, check=check)

    def _ensure_git(self) -> None:
        """Guarantee a working repository, or refuse to serve this vault.

        The old version returned early whenever `.git` existed, which is how
        production stayed broken in silence: `git init` had created the
        directory as root, every command afterwards died on ownership, and
        the early return meant nobody ever asked again. The presence of a
        directory is not the same thing as a repository that answers.
        """
        if not gitvault.is_repo(self.vault):
            if (self.vault / ".git").exists():
                # Present but unusable. Re-run the probe with check=True so
                # the caller gets git's own diagnosis. Running `init` on top
                # would succeed, change nothing, and restore the illusion.
                gitvault.run(self.vault, ["rev-parse", "--git-dir"])
            gitvault.init(self.vault)

        self._ensure_gitignore()

        # Keyed on "has no commits" rather than "was just created", because
        # the six vaults this fix is aimed at are already initialised and
        # still empty. They need their first commit on the next startup, not
        # a branch that only new vaults can reach.
        if not gitvault.has_commits(self.vault):
            gitvault.commit(self.vault, [".gitignore"], "chore: init memaix vault")

    # `PRAGMA journal_mode=WAL` in _open_db means the store is three files,
    # not one. The sidecars change on reads as well as writes, so a vault
    # that tracks them records a diff every time anyone looks at it -- and
    # committing a WAL alongside its database is worse than useless: the
    # pair is only consistent at the instant of the snapshot, so restoring
    # them from different commits yields a corrupt database rather than an
    # old one. Only `.memaix.db` was ever listed here, which nobody noticed
    # because no vault in production ever reached the point of committing.
    _IGNORED = (".memaix.db", ".memaix.db-wal", ".memaix.db-shm")

    def _ensure_gitignore(self) -> None:
        """Make sure the vault ignores its own database, all three files of it.

        Appends what is missing rather than rewriting the file: a vault's
        .gitignore belongs to whoever owns the vault, and may hold entries
        we know nothing about.
        """
        gi = self.vault / ".gitignore"
        if gi.is_symlink():
            return  # never append through a link
        present = gi.read_text().splitlines() if gi.exists() else []
        missing = [pattern for pattern in self._IGNORED if pattern not in present]
        if not missing:
            return
        lead = "" if not present or present[-1] == "" else "\n"
        with gi.open("a") as fh:
            fh.write(lead + "\n".join(missing) + "\n")

    def _current_hash(self) -> str:
        return gitvault.head(self.vault)

    def _git_commit(self, paths: list[str], message: str) -> str:
        return gitvault.commit(self.vault, paths, message)

    # flush() is a no-op here (operations are synchronous).
    # Kept for API compatibility with any future async variant.
    def flush(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def read(self, path: str) -> str | None:
        row = self._conn.execute(
            "SELECT content FROM notes WHERE path = ?", (path,)
        ).fetchone()
        return row["content"] if row else None

    def list_all(self) -> list[str]:
        rows = self._conn.execute("SELECT path FROM notes ORDER BY path").fetchall()
        return [r["path"] for r in rows]

    def get_updated_at(self, path: str) -> str | None:
        row = self._conn.execute("SELECT updated_at FROM notes WHERE path = ?", (path,)).fetchone()
        return row["updated_at"] if row else None

    def search(self, query: str) -> list[dict]:
        """FTS5 full-text search.  Returns [{path, snippet}]."""
        try:
            rows = self._conn.execute(
                "SELECT path, snippet(notes_fts, 1, '', '', '...', 15) AS snip "
                "FROM notes_fts WHERE notes_fts MATCH ?",
                (_fts_or_query(query),),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"path": r["path"], "snippet": r["snip"]} for r in rows]

    def history(self, path: str | None = None, limit: int = 20) -> list[dict]:
        """Git log.  Returns [{hash, author, date, message}] newest-first."""
        fmt = "--format=%H\x1f%an\x1f%ai\x1f%s"
        # An empty repository is the one honest reason for an empty history,
        # and `git log` reports it the same way it reports a repository it
        # refuses to read: exit 128. The old code treated both as "no
        # history", which is why production answered [] for months while
        # every command underneath was failing. Establish that the repo
        # answers before letting emptiness mean emptiness -- asked the other
        # way round, a broken vault reports "no commits" and the silence
        # comes straight back.
        gitvault.require_repo(self.vault)
        if not gitvault.has_commits(self.vault):
            return []
        if path:
            rel = str(Path("memory") / path)
            r = self._run(["log", f"-{limit}", fmt, "--", rel])
        else:
            r = self._run(["log", f"-{limit}", fmt])
        out: list[dict] = []
        for line in r.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\x1f", 3)
            if len(parts) < 4:
                continue
            out.append(
                {
                    "hash": parts[0],
                    "author": parts[1],
                    "date": parts[2],
                    "message": parts[3],
                }
            )
        return out

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def write(self, path: str, content: str, author: str) -> str:
        """Write note to disk + SQLite + git.  Returns git commit hash."""
        now = datetime.now(timezone.utc).isoformat()
        with self.write_lock:
            vault_fs.write_text(self.vault, self.memory_dir / path, content)
            self._conn.execute(
                "INSERT INTO notes (path, content, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET"
                " content=excluded.content, updated_at=excluded.updated_at",
                (path, content, now),
            )
            # Keep standalone FTS in sync
            self._conn.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
            self._conn.execute(
                "INSERT INTO notes_fts(path, content) VALUES (?, ?)", (path, content)
            )
            self._conn.commit()
            rel = str(Path("memory") / path)
            return self._git_commit([rel], f"memaix: write {path} by {author}")

    def append(self, path: str, text: str, author: str) -> str:
        """Append text to note (creating if absent).  Returns git commit hash."""
        existing = self.read(path) or ""
        sep = "\n" if existing and not existing.endswith("\n") else ""
        return self.write(path, existing + sep + text, author)

    def revert(self, commit: str) -> str:
        """Create a new commit that undoes *commit*.  Returns new commit hash."""
        # Only accept a plain git object hash. This blocks argument injection
        # (a value like "-x" being parsed as a git flag) and stray refspecs.
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
            raise ValueError(f"invalid commit hash: {commit!r}")
        with self.write_lock:
            self._run(["revert", "--no-edit", commit])
            reverted = self._current_hash()
            if not reverted:
                raise gitvault.GitError(f"git revert left no commit in {self.vault}")
            return reverted
