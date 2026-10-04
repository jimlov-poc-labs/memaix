# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for search.index — chunking, upsert/delete, reindex."""

from __future__ import annotations

import pytest

from memaix_gateway.acl import Acl
from memaix_gateway.search.embedder import FakeEmbedder
from memaix_gateway.search.index import chunk_text, index_delete, index_upsert, reindex_project
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.tools import backlog as t_backlog
from memaix_gateway.tools import memory as t_memory


def test_chunk_text_short_text_single_chunk():
    assert chunk_text("hello world", size=800) == ["hello world"]


def test_chunk_text_empty_returns_nothing():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_chunk_text_splits_long_text():
    text = "line\n" * 500  # 2500 chars
    chunks = chunk_text(text, size=200, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 220 for c in chunks)  # some slack for the newline-aligned break


def test_chunk_text_makes_progress_even_without_newlines():
    text = "x" * 5000  # no newlines at all
    chunks = chunk_text(text, size=200, overlap=190)
    assert len(chunks) > 1
    assert len(chunks) < 5000  # must terminate, not loop forever


@pytest.fixture()
def store(tmp_path):
    return EmbeddingStore.for_path(tmp_path / "index.db")


@pytest.fixture()
def embedder():
    return FakeEmbedder(dim=32)


def test_index_upsert_creates_searchable_chunks(store, embedder):
    n = index_upsert(store, embedder, "proj", "memory", "note.md", "note.md", "invoice is overdue")
    assert n == 1
    hits = store.fts_search(["proj"], ["memory"], "invoice", 10)
    assert len(hits) == 1


def test_index_upsert_empty_text_deletes_existing(store, embedder):
    index_upsert(store, embedder, "proj", "memory", "note.md", "note.md", "content")
    n = index_upsert(store, embedder, "proj", "memory", "note.md", "note.md", "")
    assert n == 0
    assert store.fts_search(["proj"], ["memory"], "content", 10) == []


def test_index_delete(store, embedder):
    index_upsert(store, embedder, "proj", "memory", "note.md", "note.md", "content")
    index_delete(store, "proj", "memory", "note.md")
    assert store.candidates(["proj"], ["memory"], 10) == []


def test_index_upsert_without_embedder_still_lexically_searchable(store):
    n = index_upsert(store, None, "proj", "memory", "note.md", "note.md", "invoice overdue")
    assert n == 1
    assert store.candidates(["proj"], ["memory"], 10) == []  # no vectors
    assert len(store.fts_search(["proj"], ["memory"], "invoice", 10)) == 1


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "memory").mkdir(parents=True)
    (v / "backlog").mkdir(parents=True)
    (v / "memory" / "standup.md").write_text("Yesterday we discussed the invoice delay.")
    (v / "docs.txt").write_text("Project charter and scope document.")
    return v


@pytest.fixture()
def acl(vault):
    return Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": str(vault)}})


def test_reindex_project_indexes_memory_backlog_and_files(store, embedder, acl, vault):
    t_backlog.backlog_add(acl, "alice", "proj", "Fix the invoice bug", "long description here")
    result = reindex_project(store, embedder, acl, "proj")
    assert result["sources"] >= 3  # standup.md + docs.txt + 1 backlog item
    assert result["chunks"] >= 3

    hits = store.fts_search(["proj"], ["memory"], "invoice", 10)
    assert len(hits) == 1
    hits = store.fts_search(["proj"], ["backlog"], "invoice", 10)
    assert len(hits) == 1
    hits = store.fts_search(["proj"], ["file"], "charter", 10)
    assert len(hits) == 1


def test_reindex_project_skips_internal_files(store, embedder, acl, vault):
    system_dir = vault / "_system"
    system_dir.mkdir(exist_ok=True)
    (system_dir / "onboarding.json").write_text('{"secret": "should-not-be-indexed"}')
    reindex_project(store, embedder, acl, "proj")
    hits = store.fts_search(["proj"], ["file"], "should", 10)
    assert hits == []


def test_reindex_project_no_vault_raises(tmp_path, acl):
    acl2 = Acl(users={"alice": {"grants": {"x": "owner"}}}, projects={"x": {}})
    with pytest.raises(ValueError):
        reindex_project(EmbeddingStore.for_path(tmp_path / "never.db"), None, acl2, "x")


# ---------------------------------------------------------------------------
# Karakteriseringstester (Sonar S3776-saneringen av reindex_project).
# ---------------------------------------------------------------------------

def _rows(store):
    conn = __import__("sqlite3").connect(str(store._path))
    try:
        return sorted(conn.execute(
            "SELECT source_type, ref, title, COUNT(*) FROM chunks GROUP BY source_type, ref, title"
        ).fetchall())
    finally:
        conn.close()


def _acl_for(vault):
    return Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": str(vault)}})


def test_reindex_error_message_and_empty_vault_value(tmp_path, store):
    acl = Acl(users={}, projects={"x": {"vault": ""}})
    with pytest.raises(ValueError) as exc:
        reindex_project(store, None, acl, "x")
    assert str(exc.value) == "project 'x' has no vault configured"
    with pytest.raises(ValueError):
        reindex_project(store, None, acl, "unknown-project")


def test_reindex_vault_path_that_does_not_exist_returns_zero(tmp_path, store):
    result = reindex_project(store, None, _acl_for(tmp_path / "nope"), "proj")
    assert result == {"chunks": 0, "sources": 0}
    assert _rows(store) == []


def test_reindex_memory_refs_nested_hidden_pm_and_unreadable(tmp_path, store):
    v = tmp_path / "v"
    (v / "memory" / "sub").mkdir(parents=True)
    (v / "memory" / "pm").mkdir()
    (v / "memory" / "_system").mkdir()
    (v / "memory" / "a.md").write_text("alpha")
    (v / "memory" / "sub" / "b.md").write_text("beta")
    (v / "memory" / ".hidden.md").write_text("hidden")
    (v / "memory" / "pm" / "plan.md").write_text("pm data")
    (v / "memory" / "_system" / "s.md").write_text("system data")
    (v / "memory" / "bin.md").write_bytes(b"\xff\xfe\xfa\x00")
    locked = v / "memory" / "locked.md"
    locked.write_text("locked")
    locked.chmod(0)
    empty = v / "memory" / "empty.md"
    empty.write_text("   ")
    try:
        result = reindex_project(store, None, _acl_for(v), "proj")
    finally:
        locked.chmod(0o644)
    # tom fil räknas som källa men ger 0 chunks; ej läsbara/skippable hoppas över
    assert result == {"chunks": 2, "sources": 3}
    assert _rows(store) == [
        ("memory", "a.md", "a.md", 1),
        ("memory", "sub/b.md", "sub/b.md", 1),
    ]


def test_reindex_backlog_frontmatter_and_fallbacks(tmp_path, store):
    v = tmp_path / "v"
    (v / "backlog").mkdir(parents=True)
    (v / "backlog" / "card1.md").write_text("---\nid: BL-1\ntitle: Titel ett\n---\nbody ett\n")
    (v / "backlog" / "plain.md").write_text("bara text")
    (v / "backlog" / "notes.txt").write_text("ignoreras")
    (v / "backlog" / "bin.md").write_bytes(b"\xff\xfe\xfa\x00")
    result = reindex_project(store, None, _acl_for(v), "proj")
    assert result == {"chunks": 2, "sources": 2}
    assert _rows(store) == [
        ("backlog", "BL-1", "Titel ett", 1),
        ("backlog", "plain", "plain", 1),
    ]
    hits = store.fts_search(["proj"], ["backlog"], "ett", 10)
    assert {h["ref"] for h in hits} == {"BL-1"}
    assert "Titel ett\nbody ett" in hits[0]["text"]


def test_reindex_files_skip_rules_and_unreadable(tmp_path, store):
    v = tmp_path / "v"
    for d in ("memory", "backlog", "_system", "pm", ".git", "docs/pm", "docs/deep"):
        (v / d).mkdir(parents=True)
    (v / "readme.txt").write_text("readme")
    (v / "docs" / "deep" / "guide.txt").write_text("guide")
    (v / "docs" / "pm" / "x.txt").write_text("skip pm")
    (v / "memory" / "m.md").write_text("mem")
    (v / "backlog" / "other.txt").write_text("not indexed anywhere")
    (v / "_system" / "s.json").write_text("{}")
    (v / "pm" / "p.md").write_text("pm")
    (v / ".git" / "config").write_text("git")
    (v / ".memaix.db").write_text("db")
    (v / ".gitignore").write_text("ignored")
    (v / ".dot").write_text("dot")
    (v / "bin.dat").write_bytes(b"\xff\xfe\xfa\x00")
    result = reindex_project(store, None, _acl_for(v), "proj")
    assert result == {"chunks": 3, "sources": 3}
    assert _rows(store) == [
        ("file", "docs/deep/guide.txt", "docs/deep/guide.txt", 1),
        ("file", "readme.txt", "readme.txt", 1),
        ("memory", "m.md", "m.md", 1),
    ]


def test_reindex_without_memory_or_backlog_dirs_still_indexes_files(tmp_path, store):
    v = tmp_path / "v"
    v.mkdir()
    (v / "only.txt").write_text("only file")
    assert reindex_project(store, None, _acl_for(v), "proj") == {"chunks": 1, "sources": 1}


def test_reindex_chunks_total_counts_multi_chunk_documents(tmp_path, store):
    v = tmp_path / "v"
    (v / "memory").mkdir(parents=True)
    (v / "memory" / "long.md").write_text("line\n" * 400)  # 2000 tecken -> flera chunks
    result = reindex_project(store, None, _acl_for(v), "proj")
    assert result["sources"] == 1
    assert result["chunks"] > 1
    assert result["chunks"] == _rows(store)[0][3]


def test_reindex_is_idempotent(tmp_path, store):
    v = tmp_path / "v"
    (v / "memory").mkdir(parents=True)
    (v / "memory" / "a.md").write_text("alpha")
    first = reindex_project(store, None, _acl_for(v), "proj")
    second = reindex_project(store, None, _acl_for(v), "proj")
    assert first == second == {"chunks": 1, "sources": 1}
    assert _rows(store) == [("memory", "a.md", "a.md", 1)]
