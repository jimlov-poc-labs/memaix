# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indexing — chunk vault content and keep the EmbeddingStore in sync.

See docs/FEATURE-SEMANTIC-SEARCH.md §6.
"""

from __future__ import annotations

from pathlib import Path

from .. import frontmatter as fm

# Directories/files that must never be indexed (internal state, secrets, VCS).
_SKIP_DIR_PARTS = {"_system", ".git", "pm"}
_SKIP_FILE_NAMES = {".memaix.db", ".gitignore"}
_MEMORY = "memory"
_BACKLOG = "backlog"


def chunk_text(text: str, size: int = 800, overlap: int = 120) -> list[str]:
    """Split *text* into ~size-char chunks, preferring to break at a newline
    near the end of each window. Returns [] for empty/whitespace-only text."""
    text = text or ""
    if not text.strip():
        return []
    if len(text) <= size:
        return [text.strip()]

    chunks: list[str] = []
    n = len(text)
    start = 0
    while start < n:
        end = min(start + size, n)
        if end < n:
            back_limit = max(start, end - int(size * 0.2))
            nl = text.rfind("\n", back_limit, end)
            if nl != -1 and nl > start:
                end = nl
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)  # always make forward progress
    return chunks


def index_upsert(
    store, embedder, project: str, source_type: str, ref: str, title: str, text: str,
    *, chunk_chars: int = 800, chunk_overlap: int = 120,
) -> int:
    """(Re)index one source document. Returns the number of chunks written."""
    pieces = chunk_text(text, chunk_chars, chunk_overlap)
    if not pieces:
        store.delete(project, source_type, ref)
        return 0
    vectors = embedder.embed(pieces) if embedder is not None else [None] * len(pieces)
    chunks = [
        {"chunk_ix": i, "title": title, "text": piece, "vector": vec}
        for i, (piece, vec) in enumerate(zip(pieces, vectors))
    ]
    store.replace_chunks(project, source_type, ref, chunks)
    return len(chunks)


def index_delete(store, project: str, source_type: str, ref: str) -> None:
    store.delete(project, source_type, ref)


def _is_skippable(vault: Path, path: Path) -> bool:
    if path.name in _SKIP_FILE_NAMES or path.name.startswith("."):
        return True
    parts = path.relative_to(vault).parts
    return any(p in _SKIP_DIR_PARTS for p in parts)


def _read_text(path: Path) -> str | None:
    """File text, or None if it cannot be read as text."""
    try:
        return path.read_text()
    except (UnicodeDecodeError, OSError):
        return None


def _index_memory_dir(store, embedder, project: str, vault: Path) -> tuple[int, int]:
    memory_dir = vault / _MEMORY
    chunks = sources = 0
    if not memory_dir.is_dir():
        return chunks, sources
    for p in sorted(memory_dir.rglob("*")):
        if not p.is_file() or _is_skippable(vault, p):
            continue
        text = _read_text(p)
        if text is None:
            continue
        rel = str(p.relative_to(memory_dir))
        chunks += index_upsert(store, embedder, project, _MEMORY, rel, rel, text)
        sources += 1
    return chunks, sources


def _index_backlog_dir(store, embedder, project: str, vault: Path) -> tuple[int, int]:
    backlog_dir = vault / _BACKLOG
    chunks = sources = 0
    if not backlog_dir.is_dir():
        return chunks, sources
    for p in sorted(backlog_dir.glob("*.md")):
        text = _read_text(p)
        if text is None:
            continue
        meta, body = fm.split(text)
        title = meta.get("title", p.stem)
        ref = meta.get("id", p.stem)
        chunks += index_upsert(store, embedder, project, _BACKLOG, ref, title, f"{title}\n{body}")
        sources += 1
    return chunks, sources


def _index_other_files(store, embedder, project: str, vault: Path) -> tuple[int, int]:
    chunks = sources = 0
    if not vault.is_dir():
        return chunks, sources
    for p in sorted(vault.rglob("*")):
        if not p.is_file() or _is_skippable(vault, p):
            continue
        parts = p.relative_to(vault).parts
        if parts and parts[0] in (_MEMORY, _BACKLOG):
            continue  # already covered by the memory/backlog passes
        text = _read_text(p)
        if text is None:
            continue
        rel = str(p.relative_to(vault))
        chunks += index_upsert(store, embedder, project, "file", rel, rel, text)
        sources += 1
    return chunks, sources


def reindex_project(store, embedder, acl, project: str) -> dict:
    """Walk a project's vault (memory/, backlog/*.md, other text files) and
    (re)index everything. Returns {"chunks": total, "sources": count}."""
    vault_str = acl.resource(project, "vault")
    if not vault_str:
        raise ValueError(f"project {project!r} has no vault configured")
    vault = Path(vault_str)
    total_chunks = 0
    sources = 0
    for index_pass in (_index_memory_dir, _index_backlog_dir, _index_other_files):
        chunks, n = index_pass(store, embedder, project, vault)
        total_chunks += chunks
        sources += n
    return {"chunks": total_chunks, "sources": sources}
