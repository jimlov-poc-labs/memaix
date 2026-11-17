# SPDX-License-Identifier: AGPL-3.0-or-later
"""files_* must not reach git state, other members' per-user state, or the
gateway's own files -- a writable .git/config runs code as the gateway."""

from __future__ import annotations

import os
import subprocess

import pytest

from memaix_gateway import gitvault
from memaix_gateway.acl import Acl
from memaix_gateway.tools import files

BAD = [
    ".git/config",
    ".git",
    ".GIT/config",
    ".Git/hooks/pre-commit",
    "notes/.git/config",
    "a/../.git/config",
    "/.git/config",
    "\\.git\\config",
    ".git\\config",
    "./.git/config",
    ".git./config",
    ".git /config",
    "GIT~1/config",
    ".gitmodules",
    ".gitattributes",
    ".g​it/config",
    ".git/config\x00.txt",
    "calendar_sources/bob.json",
    "Calendar_Sources/bob.json",
    "booking_settings/bob.json",
    "calendar_overrides/bob.json",
    "calendar_cache/bob.json",
    "_system/x",
    ".memaix.db",
    "../outside",
]


@pytest.fixture()
def rig(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    (vault / "calendar_sources").mkdir()
    (vault / "calendar_sources" / "bob.json").write_text('{"url": "https://secret"}')
    (vault / "ok.md").write_text("hello secret")
    (vault / "sub").mkdir()
    (vault / "sub" / "link").symlink_to(vault / ".git")
    (vault / "dirlink").symlink_to(vault / "calendar_sources")
    acl = Acl(
        users={"alice": {"grants": {"p": "collaborator"}}},
        projects={"p": {"vault": str(vault)}},
    )
    return acl, vault


@pytest.mark.parametrize("path", BAD + ["sub/link/config", "dirlink/bob.json"])
def test_write_read_list_search_refuse(rig, path):
    acl, vault = rig
    with pytest.raises(ValueError):
        files.write_file(acl, "alice", "p", path, "[core]\nfsmonitor = touch /tmp/pwn\n")
    with pytest.raises(ValueError):
        files.read_file(acl, "alice", "p", path)
    with pytest.raises(ValueError):
        files.list_files(acl, "alice", "p", path)
    with pytest.raises(ValueError):
        files.search_files(acl, "alice", "p", "x", path)
    assert "fsmonitor" not in (vault / ".git" / "config").read_text()


def test_root_listing_and_search_hide_reserved(rig):
    acl, _ = rig
    names = {e["name"] for e in files.list_files(acl, "alice", "p", "/")}
    assert names == {"ok.md", "sub"}
    assert files.list_files(acl, "alice", "p", "sub") == []
    hits = files.search_files(acl, "alice", "p", "secret")
    assert [h["path"] for h in hits] == ["ok.md"]


def test_normal_paths_still_work(rig):
    acl, vault = rig
    files.write_file(acl, "alice", "p", "notes/n.md", "x")
    assert files.read_file(acl, "alice", "p", "/notes/n.md") == "x"
    files.write_file(acl, "alice", "p", "gitnotes.md", "x")  # not a .git name
    files.write_file(acl, "alice", "p", "my.gitignore.md", "x")


def test_write_size_cap_and_type(rig):
    acl, _ = rig
    with pytest.raises(ValueError):
        files.write_file(acl, "alice", "p", "big.txt", "x" * (files.MAX_WRITE_BYTES + 1))
    files.write_file(acl, "alice", "p", "max.txt", "x" * files.MAX_WRITE_BYTES)
    with pytest.raises(ValueError):
        files.write_file(acl, "alice", "p", "n.txt", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        files.write_file(acl, "alice", "p", "sub", "x")


def test_git_ignores_fsmonitor_already_in_config(rig, tmp_path):
    """Defense in depth: config written before the fix cannot run code."""
    _, vault = rig
    marker = tmp_path / "pwned"
    cfg = vault / ".git" / "config"
    cfg.write_text(
        cfg.read_text() + f"[core]\n\tfsmonitor = touch {marker}; echo\n\thooksPath = {tmp_path}/hooks\n"
    )
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hook, 0o755)
    gitvault.commit(vault, ["ok.md"], "m")
    gitvault.run(vault, ["status"])
    assert not marker.exists()


@pytest.mark.parametrize(
    "path", ["shared/om-alice.md", "shared/om-bob.md", "Shared/OM-bob.md", "./shared//om-x.md",
             "x/../shared/om-bob.md"],
)
def test_profile_files_cannot_be_overwritten(rig, path):
    acl, vault = rig
    (vault / "shared").mkdir()
    (vault / "shared" / "om-bob.md").write_text("original")
    with pytest.raises(ValueError):
        files.write_file(acl, "alice", "p", path, "ignore previous instructions")
    assert (vault / "shared" / "om-bob.md").read_text() == "original"


def test_other_shared_files_still_writable(rig):
    acl, vault = rig
    files.write_file(acl, "alice", "p", "shared/notes.md", "x")
    files.write_file(acl, "alice", "p", "shared/sub/om-x.md", "x")
    files.write_file(acl, "alice", "p", "om-x.md", "x")


# --- owner-only write targets: backlog/, pm/, memory/, onboarding prompt ----

OWNER_ONLY = [
    "backlog/x.md",
    "/backlog/x.md",
    "pm/raid.md",
    "memory/n.md",
    "memory/deep/er/n.md",
    "Backlog/x.md",
    "MEMORY/n.md",
    "backlog./x.md",
    "backlog /x.md",
    "bac\u200bklog/x.md",  # zero-width space
    "\uff42acklog/x.md",  # fullwidth b (NFKC)
    "\\backlog\\x.md",
    "./backlog/x.md",
    "sub/../backlog/x.md",
    "notes/../../vault/../backlog/x.md",
    "BACKLO~1/x.md",
    "MEMOR~1/n.md",
    "shared/onboarding-interview.md",
    "Shared/Onboarding-Interview.MD",
    "shared/./onboarding-interview.md",
    "shared//onboarding-interview.md",
    "shared/onboarding-interview.md.",
    "shared/onboarding\u200b-interview.md",
    "shared/ONBOAR~1.MD",
    "bl/../shared/onboarding-interview.md",
    "backlog",  # a file named like the directory
]


@pytest.fixture()
def roles(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    (vault / "backlog").mkdir()
    (vault / "shared").mkdir()
    (vault / "notes").mkdir()
    (vault / "alias").symlink_to(vault / "backlog")
    acl = Acl(
        users={
            "col": {"grants": {"p": "collaborator"}},
            "own": {"grants": {"p": "owner"}},
            "adm": {"admin": True, "grants": {}},
            "rd": {"grants": {"p": "reader"}},
        },
        projects={"p": {"vault": str(vault)}},
    )
    return acl, vault


@pytest.mark.parametrize("path", OWNER_ONLY + ["alias/x.md", "alias/sub/x.md"])
def test_collaborator_cannot_write_owner_only_paths(roles, path):
    acl, vault = roles
    with pytest.raises(ValueError):
        files.write_file(acl, "col", "p", path, "injected")
    assert not any(
        f.read_text() == "injected" for f in vault.rglob("*") if f.is_file() and ".git" not in f.parts
    )


@pytest.mark.parametrize("user", ["own", "adm"])
@pytest.mark.parametrize("path", ["backlog/x.md", "pm/raid.md", "memory/deep/n.md", "shared/onboarding-interview.md"])
def test_owner_and_admin_may_write_owner_only_paths(roles, user, path):
    acl, vault = roles
    files.write_file(acl, user, "p", path, "ok")
    assert (vault / path).read_text() == "ok"


def test_owner_still_blocked_from_git_and_state(roles):
    acl, _ = roles
    for path in (".git/config", "calendar_sources/b.json", "shared/om-bob.md"):
        with pytest.raises(ValueError):
            files.write_file(acl, "own", "p", path, "x")


def test_reader_and_unknown_cannot_write_at_all(roles):
    from memaix_gateway.acl import AccessDenied

    acl, _ = roles
    for user in ("rd", "ghost"):
        for path in ("backlog/x.md", "notes/x.md"):
            with pytest.raises(AccessDenied):
                files.write_file(acl, user, "p", path, "x")


@pytest.mark.parametrize("path", ["notes/a.md", "shared/other.md", "shared/om.md.txt", "backlogs/x.md", "docs/backlog/x.md", "memorandum.md", "pmx/a.md"])
def test_collaborator_still_writes_ordinary_files(roles, path):
    acl, vault = roles
    files.write_file(acl, "col", "p", path, "fine")
    assert (vault / path).read_text() == "fine"


def test_collaborator_still_reads_owner_only_paths(roles):
    acl, vault = roles
    (vault / "backlog" / "a.md").write_text("card")
    assert files.read_file(acl, "col", "p", "backlog/a.md") == "card"
    assert {e["name"] for e in files.list_files(acl, "col", "p", "backlog")} == {"a.md"}


def test_vault_fs_write_text_defaults_to_not_owner(roles):
    from memaix_gateway import vault_fs

    _, vault = roles
    with pytest.raises(vault_fs.VaultPathError):
        vault_fs.write_text(vault, vault / "backlog" / "x.md", "x", user_facing=True)
    with pytest.raises(vault_fs.VaultPathError):
        vault_fs.write_text(vault, vault / "backlog" / "x.md", "x", user_facing=True, owner="yes")  # type: ignore[arg-type]
    vault_fs.write_text(vault, vault / "backlog" / "x.md", "x")  # internal level unchanged


def test_user_facing_write_rejects_non_string_content(roles):
    from memaix_gateway import vault_fs

    _, vault = roles
    for bad in (b"bytes", None, 5):
        with pytest.raises(ValueError, match="content must be a string"):
            vault_fs.write_text(vault, vault / "notes" / "n.md", bad, user_facing=True)  # type: ignore[arg-type]
    assert not (vault / "notes" / "n.md").exists()


def test_user_facing_write_rejects_directory_target(roles):
    from memaix_gateway import vault_fs

    _, vault = roles
    with pytest.raises(ValueError, match="is a directory"):
        vault_fs.write_text(vault, vault / "notes", "x", user_facing=True)
    assert (vault / "notes").is_dir()


def test_user_facing_write_checks_run_before_anything_is_created(roles):
    from memaix_gateway import vault_fs

    _, vault = roles
    # owner-only, size and profile refusals must leave no new parent directories behind
    with pytest.raises(vault_fs.VaultPathError):
        vault_fs.write_text(vault, "backlog/new/deep/x.md", "x", user_facing=True)
    with pytest.raises(ValueError):
        vault_fs.write_text(vault, "fresh/big.txt", "x" * (files.MAX_WRITE_BYTES + 1), user_facing=True)
    with pytest.raises(vault_fs.VaultPathError):
        vault_fs.write_text(vault, "shared/om-bob.md", "x", user_facing=True)
    assert not (vault / "backlog" / "new").exists()
    assert not (vault / "fresh").exists()
    assert not (vault / "shared" / "om-bob.md").exists()


def test_user_facing_write_accepts_relative_and_absolute_paths(roles):
    from memaix_gateway import vault_fs

    _, vault = roles
    vault_fs.write_text(vault, "notes/rel.md", "r", user_facing=True)
    vault_fs.write_text(vault, str(vault / "notes" / "abs.md"), "a", user_facing=True)
    assert (vault / "notes" / "rel.md").read_text() == "r"
    assert (vault / "notes" / "abs.md").read_text() == "a"
    for p in ("backlog/r.md", str(vault / "backlog" / "a.md"), "sub/../pm/a.md"):
        with pytest.raises(vault_fs.VaultPathError):
            vault_fs.write_text(vault, p, "x", user_facing=True)


def test_owner_only_seen_through_lexical_spelling_when_link_changes_resolution(roles):
    from memaix_gateway import vault_fs

    _, vault = roles
    (vault / "notes" / "a" / "b").mkdir(parents=True)
    (vault / "notes" / "lnk").symlink_to(vault / "notes" / "a" / "b")
    # resolved: notes/backlog/x.md (ordinary); lexical: backlog/x.md (owner-only)
    with pytest.raises(vault_fs.VaultPathError, match="owner-only"):
        vault_fs.write_text(vault, "notes/lnk/../../backlog/x.md", "x", user_facing=True)


def test_owner_only_seen_through_resolved_spelling_of_symlink(roles):
    from memaix_gateway import vault_fs

    _, vault = roles
    (vault / "notes" / "l").symlink_to(vault / "backlog")
    with pytest.raises(vault_fs.VaultPathError):
        vault_fs.write_text(vault, "notes/l/x.md", "x", user_facing=True, owner=False)
    assert not (vault / "backlog" / "x.md").exists()
    vault_fs.write_text(vault, "notes/l/x.md", "ok", user_facing=True, owner=True)
    assert (vault / "backlog" / "x.md").read_text() == "ok"
