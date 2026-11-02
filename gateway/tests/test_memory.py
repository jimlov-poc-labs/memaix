# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for MemoryStore + memory_* tools.

Uses real SQLite and real git (git must be available in PATH).
Each test gets an isolated tmp_path so singletons don't collide.
"""

from __future__ import annotations

import pytest

from memaix_gateway.acl import Acl, AccessDenied
from memaix_gateway.backends.memory_store import MemoryStore
from memaix_gateway.tools.memory import (
    memory_append,
    memory_history,
    memory_read,
    memory_revert,
    memory_search,
    memory_write,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture()
def store(vault):
    # Clear singleton registry so each test gets a fresh instance
    MemoryStore._clear_instances()
    return MemoryStore.for_vault(vault)


@pytest.fixture()
def acl(vault):
    return Acl(
        users={
            "alice": {"grants": {"proj": "owner", "novault": "owner"}},
            "carol": {"grants": {"proj": "collaborator"}},
            "bob": {"grants": {"proj": "reader"}},
        },
        projects={
            "proj": {"vault": str(vault)},
            "novault": {},  # project exists but has no vault configured
        },
    )


# ------------------------------------------------------------------
# MemoryStore unit tests
# ------------------------------------------------------------------


def test_write_read_roundtrip(store):
    commit = store.write("ideas/note.md", "hello memaix", "alice")
    assert commit  # non-empty hash
    assert store.read("ideas/note.md") == "hello memaix"


def test_read_missing_returns_none(store):
    assert store.read("does-not-exist.md") is None


def test_append_adds_text(store):
    store.write("log.md", "line one", "alice")
    store.append("log.md", "line two", "alice")
    content = store.read("log.md")
    assert "line one" in content
    assert "line two" in content


def test_append_creates_missing_note(store):
    store.append("fresh.md", "created via append", "alice")
    assert store.read("fresh.md") == "created via append"


def test_search_finds_content(store):
    store.write("ideas/note.md", "hello memaix world", "alice")
    results = store.search("hello")
    paths = [r["path"] for r in results]
    assert "ideas/note.md" in paths


def test_search_does_not_find_overwritten_content(store):
    store.write("temp.md", "findme unique_term_xyz", "alice")
    results = store.search("unique_term_xyz")
    assert any(r["path"] == "temp.md" for r in results)

    # Overwrite with different content — old term should vanish from FTS
    store.write("temp.md", "replacement content only", "alice")
    results2 = store.search("unique_term_xyz")
    assert not any(r["path"] == "temp.md" for r in results2)


def test_list_all(store):
    store.write("a.md", "aaa", "alice")
    store.write("b.md", "bbb", "alice")
    all_paths = store.list_all()
    assert "a.md" in all_paths
    assert "b.md" in all_paths


def test_history_returns_commits_after_write(store):
    store.write("hist.md", "v1", "alice")
    store.write("hist.md", "v2", "alice")
    hist = store.history("hist.md")
    assert len(hist) >= 2
    assert all("hash" in h and "message" in h for h in hist)


def test_revert_rolls_back(store):
    store.write("rv.md", "original", "alice")
    store.write("rv.md", "modified", "alice")
    assert store.read("rv.md") == "modified"

    hist = store.history("rv.md")
    assert len(hist) >= 2
    # Revert the most recent commit (less likely to conflict than reverting old ones)
    latest_commit = hist[0]["hash"]

    new_commit = store.revert(latest_commit)
    assert new_commit  # non-empty
    # After revert, a new commit exists on top
    hist_after = store.history("rv.md")
    assert len(hist_after) > len(hist)


@pytest.mark.parametrize("bad", ["-rf", "--all", "HEAD~1; rm -rf /", "not-a-hash", "", "../etc"])
def test_revert_rejects_non_hash(store, bad):
    """revert must only accept a plain git object hash (blocks arg injection)."""
    with pytest.raises(ValueError):
        store.revert(bad)


# ------------------------------------------------------------------
# memory_* tool ACL tests
# ------------------------------------------------------------------


def test_memory_write_allowed_for_collaborator(acl, store):
    result = memory_write(acl, "carol", "proj", "note.md", "written by carol")
    assert result["path"] == "note.md"
    assert result["commit"]


def test_memory_read_allowed_for_reader(acl, store):
    memory_write(acl, "carol", "proj", "shared.md", "shared content")
    result = memory_read(acl, "bob", "proj", "shared.md")
    assert result["content"] == "shared content"
    assert result["status"] == "hypotes"  # saknad frontmatter = hypotes (trappan)


def test_memory_search_allowed_for_reader(acl, store):
    memory_write(acl, "carol", "proj", "x.md", "searchable text")
    results = memory_search(acl, "bob", "proj", "searchable")
    assert isinstance(results, list)


def test_memory_write_denied_for_reader(acl, store):
    with pytest.raises(AccessDenied):
        memory_write(acl, "bob", "proj", "note.md", "blocked")


def test_memory_read_denied_for_unknown_user(acl, store):
    with pytest.raises(AccessDenied):
        memory_read(acl, "ghost", "proj", "note.md")


def test_memory_append_denied_for_reader(acl, store):
    with pytest.raises(AccessDenied):
        memory_append(acl, "bob", "proj", "note.md", "extra")


def test_memory_history_allowed_for_reader(acl, store):
    memory_write(acl, "carol", "proj", "h.md", "v1")
    result = memory_history(acl, "bob", "proj", "h.md")
    assert isinstance(result, list)


def test_memory_write_no_vault_raises_value_error(acl, store):
    with pytest.raises((ValueError, Exception)):
        memory_write(acl, "alice", "novault", "note.md", "content")


def test_memory_read_invalid_path_dot_dot(acl, store):
    with pytest.raises(ValueError):
        memory_read(acl, "alice", "proj", "../project-b.md")


def test_memory_read_absolute_path_rejected(acl, store):
    with pytest.raises(ValueError):
        memory_read(acl, "alice", "proj", "/etc/passwd")


# ------------------------------------------------------------------
# Minnestrappan (SELF-IMPROVING-SYSTEM.md Fas B)
# ------------------------------------------------------------------

from memaix_gateway.tools.memory import (  # noqa: E402
    memory_set_status,
    note_status,
    set_note_status,
)


def test_note_status_parsing():
    assert note_status("---\nstatus: verifierad\n---\ntext") == "verifierad"
    assert note_status("---\nstatus: hypotes\n---\ntext") == "hypotes"
    assert note_status("bara text utan frontmatter") == "hypotes"
    assert note_status("---\nstatus: påhittad\n---\nx") == "hypotes", "ogiltig → hypotes"
    assert note_status("") == "hypotes"


def test_set_note_status_creates_and_updates_frontmatter():
    assert set_note_status("ren text", "hypotes").startswith("---\nstatus: hypotes\n---\n")
    promoted = set_note_status("---\nstatus: hypotes\nowner: jimmy\n---\nkropp", "verifierad")
    assert "status: verifierad" in promoted and "owner: jimmy" in promoted and "kropp" in promoted
    with pytest.raises(ValueError):
        set_note_status("x", "sannolik")


def test_write_defaults_to_hypotes_without_stamping(acl, store):
    # Ingen tvångsstämpling: innehållet lagras ORÖRT (synk-trohet) men
    # presenteras som hypotes — saknad status är aldrig fakta.
    result = memory_write(acl, "carol", "proj", "trappa/a.md", "obekräftat påstående")
    assert result["status"] == "hypotes"
    read = memory_read(acl, "carol", "proj", "trappa/a.md")
    assert read["content"] == "obekräftat påstående"
    assert read["status"] == "hypotes"


def test_explicit_status_wins_over_content(acl, store):
    content = "---\nstatus: hypotes\n---\nbekräftad mot källa"
    result = memory_write(acl, "carol", "proj", "trappa/b.md", content, status="verifierad")
    assert result["status"] == "verifierad"
    assert memory_read(acl, "carol", "proj", "trappa/b.md")["status"] == "verifierad"


def test_promote_preserves_content_and_requires_collaborator(acl, store):
    memory_write(acl, "carol", "proj", "trappa/c.md", "fakta att befordra")
    result = memory_set_status(acl, "carol", "proj", "trappa/c.md", "verifierad")
    assert result["status"] == "verifierad"
    read = memory_read(acl, "carol", "proj", "trappa/c.md")
    assert "fakta att befordra" in read["content"] and read["status"] == "verifierad"
    with pytest.raises(AccessDenied):
        memory_set_status(acl, "bob", "proj", "trappa/c.md", "verifierad")  # reader


def test_search_and_list_carry_status(acl, store):
    memory_write(acl, "carol", "proj", "trappa/hyp.md", "solskensfaktoid unikord")
    memory_write(acl, "carol", "proj", "trappa/ver.md", "solskensfaktoid unikord",
                 status="verifierad")
    hits = {h["path"]: h["status"] for h in
            __import__("memaix_gateway.tools.memory", fromlist=["memory_search"])
            .memory_search(acl, "bob", "proj", "solskensfaktoid")}
    assert hits["trappa/hyp.md"] == "hypotes", "hypotes får aldrig se ut som faktum i sök"
    assert hits["trappa/ver.md"] == "verifierad"
    listed = {n["path"]: n["status"]
              for n in __import__("memaix_gateway.tools.memory", fromlist=["memory_list"])
              .memory_list(acl, "bob", "proj")}
    assert listed["trappa/hyp.md"] == "hypotes"


def test_append_new_note_is_born_hypotes(acl, store):
    from memaix_gateway.tools.memory import memory_append as _append

    result = _append(acl, "carol", "proj", "trappa/ny.md", "första raden")
    assert result["status"] == "hypotes"
    from memaix_gateway.tools.memory import memory_read as _read
    assert _read(acl, "carol", "proj", "trappa/ny.md")["content"].strip() == "första raden"
    # append till befintlig behåller status
    memory_set_status(acl, "carol", "proj", "trappa/ny.md", "verifierad")
    result = _append(acl, "carol", "proj", "trappa/ny.md", "andra raden")
    assert result["status"] == "verifierad"


def test_whoami_carries_memory_rules(acl):
    from memaix_gateway.tools.whoami import whoami

    result = whoami(acl, "carol")
    assert "memory_rules" in result
    assert "hypotes" in result["memory_rules"] and "verifierad" in result["memory_rules"]


# ------------------------------------------------------------------
# Provenance (02db3094)
# ------------------------------------------------------------------


def test_memory_write_returns_provenance_field(acl, store):
    result = memory_write(acl, "carol", "proj", "prov/a.md", "ingen filreferens här")
    assert "provenance" in result
    assert result["provenance"]["grade"] in ("verified", "unverifiable")


def test_provenance_verified_when_no_path_references(acl, store):
    result = memory_write(acl, "carol", "proj", "prov/b.md", "bara text, inga sökvägar")
    assert result["provenance"]["grade"] == "verified"
    assert result["provenance"]["dead_refs"] == []


def test_provenance_unverifiable_when_path_missing(acl, store, tmp_path):
    content = f"Vi använder /tmp/finns-inte-alls/fil.py som config"
    result = memory_write(acl, "carol", "proj", "prov/c.md", content)
    assert result["provenance"]["grade"] == "unverifiable"
    assert any("finns-inte-alls" in r for r in result["provenance"]["dead_refs"])


def test_provenance_verified_when_path_exists(acl, store, tmp_path):
    real_file = tmp_path / "real.py"
    real_file.write_text("exists")
    content = f"Se implementationen i {real_file}"
    result = memory_write(acl, "carol", "proj", "prov/d.md", content)
    assert result["provenance"]["grade"] == "verified"


def test_provenance_does_not_override_explicit_verifierad(acl, store):
    content = "sökväg /tmp/finns-inte/x.py nämns"
    result = memory_write(acl, "carol", "proj", "prov/e.md", content, status="verifierad")
    assert result["status"] == "verifierad"
    assert result["provenance"]["grade"] == "unverifiable"


@pytest.mark.parametrize(
    "path",
    [".git/config", "sub/.git/HEAD", ".gitattributes", ".gitmodules", "a/.GIT/x", ".git",
     "sub\\.git\\config", "GIT~1/config", ".git./config"],
)
def test_memory_write_refuses_git_state(acl, vault, path):
    with pytest.raises(ValueError):
        memory_write(acl, "alice", "proj", path, "[core]\nfsmonitor = x\n")
    assert not (vault / "memory" / path.replace("\\", "/")).exists()


def test_memory_write_normal_names_still_work(acl):
    memory_write(acl, "alice", "proj", "gitnotes/my.gitignore.md", "x")
