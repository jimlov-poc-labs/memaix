# SPDX-License-Identifier: AGPL-3.0-or-later
"""vault_fs is the one guard: exact names, two levels, query-time search filter."""

from __future__ import annotations

import subprocess

import pytest

from memaix_gateway import paths, vault_fs
from memaix_gateway.acl import Acl
from memaix_gateway.search.embedder import FakeEmbedder
from memaix_gateway.search.index import index_upsert
from memaix_gateway.search.query import search_all
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.tools import files
from memaix_gateway.tools.memory import memory_write

RESERVED = [
    ".git", ".gitignore", ".gitmodules", ".gitattributes", ".memaix.db", ".memaix-link",
    ".GITMODULES", "GIT~1", "GITIGN~1", "gitmod~2", "GITATT~1", "MEMAIX~1",
    ".git.", ".git ", ".g​it", ".g­it", ".g️it", "．git", ".git::$INDEX_ALLOCATION",
]
ORDINARY = [".github", ".gitkeep", ".gitlab-ci.yml", "gitnotes.md", "my.gitignore.md", "digit", "legit.md", "notes.md"]


@pytest.mark.parametrize("name", RESERVED)
def test_reserved_names(name):
    assert vault_fs.is_reserved(name)


@pytest.mark.parametrize("name", ORDINARY)
def test_ordinary_names_are_not_reserved(name):
    assert not vault_fs.is_reserved(name)


@pytest.fixture()
def rig(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    acl = Acl(
        users={"alice": {"grants": {"p": "collaborator"}}},
        projects={"p": {"vault": str(vault)}},
    )
    return acl, vault


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".gitkeep", "d/.gitkeep", ".gitlab-ci.yml"])
def test_files_write_allows_legit_dot_git_lookalikes(rig, path):
    acl, vault = rig
    files.write_file(acl, "alice", "p", path, "x")
    assert files.read_file(acl, "alice", "p", path) == "x"


def test_size_cap_counts_bytes_not_characters(rig):
    acl, vault = rig
    too_big = "é" * (files.MAX_WRITE_BYTES // 2 + 1)  # 2 bytes each, under the cap in characters
    assert len(too_big) < files.MAX_WRITE_BYTES
    with pytest.raises(ValueError):
        files.write_file(acl, "alice", "p", "big.txt", too_big)
    assert not (vault / "big.txt").exists()


def test_internal_level_still_writes_system_state_and_profiles(tmp_path):
    """Onboarding writes these through vault_fs; the tool-level rules must not leak into it."""
    v = tmp_path / "v"
    v.mkdir()
    vault_fs.write_text(v, v / "_system" / "onboarding.json", "{}")
    vault_fs.write_text(v, v / "shared" / "om-bob.md", "profile")
    vault_fs.write_text(v, v / "calendar_sources" / "bob.json", "{}")
    assert vault_fs.read_text(v, v / "shared" / "om-bob.md") == "profile"
    for p in ("_system/onboarding.json", "shared/om-bob.md", "calendar_sources/bob.json"):
        assert vault_fs.is_blocked(v, v / p, user_facing=True) == (p != "shared/om-bob.md")
    # profile is readable by tools, only the write is refused
    assert vault_fs.read_text(v, v / "shared" / "om-bob.md", user_facing=True) == "profile"
    with pytest.raises(vault_fs.VaultPathError):
        vault_fs.write_text(v, v / "shared" / "om-bob.md", "x", user_facing=True)


def test_default_level_refuses_git_metadata_files(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    for name in (".gitmodules", ".gitattributes", "GIT~1/config", ".git./config"):
        with pytest.raises(vault_fs.VaultPathError):
            vault_fs.write_text(v, v / name, "x")


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("calendar_sources/bob.json", True), ("./Working_Hours/x", True), ("a/../calendar_sources/x", True),
        ("../x", True), ("sub/.git/config", True), ("sub/calendar_sources/x", False), ("ok.md", False),
        (".github/x.md", False), ("shared/om-bob.md", False),
    ],
)
def test_is_reserved_rel(rel, expected):
    assert vault_fs.is_reserved_rel(rel) is expected


def test_is_reserved_rel_fails_closed_on_junk():
    assert vault_fs.is_reserved_rel(None) is True  # type: ignore[arg-type]
    assert vault_fs.is_reserved_rel("a\0b") is True


def test_forbid_git_uses_exact_names():
    paths.validate_relative_path("notes/.github/x.md", forbid_git=True)
    for bad in (".git/config", "a/.gitmodules", "GIT~1/x", ".g​it/x"):
        with pytest.raises(ValueError):
            paths.validate_relative_path(bad, forbid_git=True)


def test_memory_write_allows_lookalike_names(rig):
    acl, vault = rig
    memory_write(acl, "alice", "p", ".github/n.md", "x")


# ---- query-time filter: no reindex needed ---------------------------------

@pytest.mark.parametrize("embedder", [None, FakeEmbedder(dim=32)])
def test_search_hides_reserved_rows_without_reindex(tmp_path, embedder):
    acl = Acl(users={"m": {"grants": {"p": "collaborator"}}}, projects={"p": {"vault": str(tmp_path)}})
    st = EmbeddingStore.for_path(tmp_path / "i.db")
    index_upsert(st, embedder, "p", "file", "calendar_sources/bob.json", "t", "LEAKEDTOKEN one")
    index_upsert(st, embedder, "p", "file", ".git/config", "t", "LEAKEDTOKEN two")
    index_upsert(st, embedder, "p", "memory", ".gitmodules", "t", "LEAKEDTOKEN three")
    index_upsert(st, embedder, "p", "file", "notes/ok.md", "t", "LEAKEDTOKEN fine")
    res = search_all(acl, "m", None, st, embedder, "LEAKEDTOKEN")
    assert [r["ref"] for r in res["results"]] == ["notes/ok.md"]
