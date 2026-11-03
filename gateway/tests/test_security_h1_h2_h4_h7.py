# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for security findings H1, H2, H4 and H7."""

from __future__ import annotations

import ssl
import subprocess

import pytest
import yaml

from memaix_gateway import gitvault
from memaix_gateway.access_admin import AccessAdmin, AccessError
from memaix_gateway.acl import Acl
from memaix_gateway.invites import InviteStore
from memaix_gateway.outbox.policy import action_mode
from memaix_gateway.tools import files
from memaix_gateway.web.acl_writer import AclWriter

# ------------------------------------------------------------------ H1


@pytest.fixture()
def facl(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return Acl(
        users={"c": {"grants": {"p": "collaborator"}}},
        projects={"p": {"vault": str(v)}},
    ), v


BAD = [".git/config", ".git/hooks/x", "sub/.git/config", ".memaix.db", "a/../.git/config",
       ".GIT/config", ".memaix/x", "/.git/config"]


@pytest.mark.parametrize("path", BAD)
def test_write_reserved_paths_refused(facl, path):
    acl, v = facl
    with pytest.raises(ValueError):
        files.write_file(acl, "c", "p", path, "x")
    assert not (v / ".git").exists() and not (v / "sub").exists()


@pytest.mark.parametrize("path", BAD)
def test_read_reserved_paths_refused(facl, path):
    acl, v = facl
    with pytest.raises(ValueError):
        files.read_file(acl, "c", "p", path)


def test_search_and_list_reserved_refused(facl):
    acl, _ = facl
    with pytest.raises(ValueError):
        files.search_files(acl, "c", "p", "x", ".git")
    with pytest.raises(ValueError):
        files.list_files(acl, "c", "p", ".git")


def test_symlink_into_git_refused(facl):
    acl, v = facl
    (v / ".git").mkdir()
    (v / "ln").symlink_to(v / ".git")
    with pytest.raises(ValueError):
        files.write_file(acl, "c", "p", "ln/config", "x")


def test_normal_write_still_works(facl):
    acl, v = facl
    files.write_file(acl, "c", "p", "sub/notes.md", "hi")
    assert (v / "sub" / "notes.md").read_text() == "hi"
    files.write_file(acl, "c", "p", "gitignore-notes.md", "ok")


def test_gitvault_ignores_repo_fsmonitor(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    subprocess.run(["git", "init", "-q", str(v)], check=True)
    marker = tmp_path / "pwned"
    subprocess.run(["git", "-C", str(v), "config", "core.fsmonitor", f"touch {marker}; echo"],
                   check=True)
    gitvault.run(v, ["status"], check=False)
    assert not marker.exists()


# ------------------------------------------------------------------ H2

_BASE = {
    "users": {
        "alice": {"grants": {"acme": "owner"}},
        "bob": {"grants": {"beta": "owner"}},
        "erin": {"grants": {"acme": "reader"}},
    },
    "projects": {"acme": {"vault": "/a"}, "beta": {"vault": "/b"}},
}


@pytest.fixture()
def adm(tmp_path):
    path = tmp_path / "acl.yaml"
    path.write_text(yaml.safe_dump(_BASE), encoding="utf-8")
    inv = InviteStore(tmp_path / "i.db")
    admin = AccessAdmin(AclWriter(path), inv, tmp_path)
    return admin, lambda: Acl.from_config(yaml.safe_load(path.read_text(encoding="utf-8"))), inv


def test_invite_cannot_take_over_foreign_account(adm):
    admin, acl, inv = adm
    with pytest.raises(AccessError):
        admin.invite(acl(), "alice", "acme", "bob", "reader")
    assert "acme" not in acl().grants("bob")


def test_invite_new_user_works(adm):
    admin, acl, _ = adm
    assert admin.invite(acl(), "alice", "acme", "newbie", "reader")["status"] == "invited"


def test_invite_existing_user_with_only_own_grants_works(adm):
    admin, acl, _ = adm
    assert admin.invite(acl(), "alice", "acme", "erin", "collaborator")["status"] == "invited"


# ------------------------------------------------------------------ H4


def _mode(to):
    acl = Acl(users={"a": {"grants": {"p": "owner"}}},
              projects={"p": {"vault": "/v", "allowlist": ["@ok.com"]}})
    return action_mode(None, acl, "p", "email_send", {"to": to})


@pytest.mark.parametrize("to", [
    "evil@x.com;a@ok.com", "Bob <evil@x.com> @ok.com", "evil@x.com (x@ok.com",
    "evil@x.com@ok.com", "a@ok.com, evil@x.com", "garbage",
])
def test_malformed_or_foreign_recipients_need_review(to):
    assert _mode(to) == "review"


@pytest.mark.parametrize("to", ["a@ok.com", "a@ok.com, b@ok.com", "Bob <a@ok.com>"])
def test_clean_allowlisted_recipients_stay_auto(to):
    assert _mode(to) == "auto"


# ------------------------------------------------------------------ H7


class _Smtp:
    ctx = "unset"

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, *a, **k):
        _Smtp.ctx = k.get("context")

    def login(self, *a):
        pass

    def send_message(self, m):
        pass


def _assert_strict():
    c = _Smtp.ctx
    assert c is not None and c != "unset"
    assert c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname is True


def test_email_tool_starttls_verifies(monkeypatch):
    import smtplib
    from email.message import EmailMessage

    from memaix_gateway import config
    from memaix_gateway.tools import email as tool

    monkeypatch.setattr(smtplib, "SMTP", _Smtp)
    monkeypatch.setattr(config, "secret", lambda ref: "pw")
    _Smtp.ctx = "unset"
    tool._smtp_deliver(EmailMessage(), "p", {"user": "u", "password_ref": "x"}, {})
    _assert_strict()


def test_notify_channel_starttls_verifies(monkeypatch):
    import smtplib

    from memaix_gateway import config
    from memaix_gateway.notify.channels import EmailChannel

    acl = Acl(users={}, projects={"p": {"vault": "/v", "mailbox": {"host": "h", "user": "u",
                                                                   "password_ref": "x"}}})
    monkeypatch.setattr(smtplib, "SMTP", _Smtp)
    monkeypatch.setattr(config, "secret", lambda ref: "pw")
    _Smtp.ctx = "unset"
    EmailChannel(acl, {"to": "to@x.com", "project": "p"}).send("s", "m", "t")
    _assert_strict()


# ------------------------------------------------------------ review round 2


def test_search_and_list_root_skip_reserved(facl):
    acl, v = facl
    (v / ".git").mkdir()
    (v / ".git" / "config").write_text("SECRET fsmonitor")
    (v / ".memaix.db").write_text("SECRET db")
    (v / ".memaix").mkdir()
    (v / ".memaix" / "x").write_text("SECRET dir")
    (v / "ok.md").write_text("SECRET public")
    hits = files.search_files(acl, "c", "p", "secret", "/")
    assert [h["path"] for h in hits] == ["ok.md"]
    names = {e["name"] for e in files.list_files(acl, "c", "p", "/")}
    assert names == {"ok.md"}


def test_indexer_skips_memaix_dirs(tmp_path):
    from memaix_gateway.search.index import _is_skippable

    assert _is_skippable(tmp_path, tmp_path / ".memaix" / "x.md")
    assert not _is_skippable(tmp_path, tmp_path / "notes" / "x.md")


def test_calendar_attendee_injection_needs_review():
    acl = Acl(users={"a": {"grants": {"p": "owner"}}},
              projects={"p": {"vault": "/v", "allowlist": ["@ok.com"]}})
    for att in (["evil@x.com;a@ok.com"], ["evil@x.com@ok.com"], ["Bob <evil@x.com> @ok.com"]):
        assert action_mode(None, acl, "p", "calendar_create", {"attendees": att}) == "review"
    assert action_mode(None, acl, "p", "calendar_create", {"attendees": ["a@ok.com"]}) == "auto"


def test_accept_invite_rechecks_ownership(adm, tmp_path):
    admin, acl, inv = adm
    # carol: pending (no hash), only in acme -> invitable by alice
    admin.invite(acl(), "alice", "acme", "erin", "collaborator")
    tok = inv.issue("erin", "alice")
    # grants change after issue: erin gets a beta grant (alice does not own beta)
    admin._writer.update(lambda d: d["users"]["erin"]["grants"].__setitem__("beta", "reader"))
    with pytest.raises(AccessError):
        admin.accept_invite(tok, "a-long-enough-password")


def test_accept_invite_still_works_when_owned(adm):
    admin, acl, inv = adm
    out = admin.invite(acl(), "alice", "acme", "newbie", "reader")
    assert admin.accept_invite(out["token"], "a-long-enough-password") == "newbie"


def test_git_run_neutralises_hooks_and_ssh(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    subprocess.run(["git", "init", "-q", str(v)], check=True)
    hook = v / ".git" / "hooks" / "pre-commit"
    marker = tmp_path / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (v / "f").write_text("x")
    gitvault.run(v, ["add", "f"])
    gitvault.run(v, ["commit", "-q", "-m", "m"], check=False)
    assert not marker.exists()


# ------------------------------------------------------------ review round 3


def test_symlink_to_git_config_not_searched_or_indexed(facl):
    from memaix_gateway.search.index import _is_skippable

    acl, v = facl
    (v / ".git").mkdir()
    (v / ".git" / "config").write_text("SECRET fsmonitor")
    (v / "memory").mkdir()
    (v / "linkfile").symlink_to(v / ".git" / "config")
    (v / "memory" / "mlink.md").symlink_to(v / ".git" / "config")
    (v / "ok.md").write_text("SECRET public")
    assert [h["path"] for h in files.search_files(acl, "c", "p", "secret", "/")] == ["ok.md"]
    assert _is_skippable(v, v / "linkfile")
    assert _is_skippable(v, v / "memory" / "mlink.md")
    assert not _is_skippable(v, v / "ok.md")
    with pytest.raises(ValueError):
        files.read_file(acl, "c", "p", "linkfile")


def test_gitignore_is_reserved_for_writes(facl):
    acl, _ = facl
    with pytest.raises(ValueError):
        files.write_file(acl, "c", "p", ".gitignore", "")


def test_commit_never_stages_memaix_even_without_gitignore(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    subprocess.run(["git", "init", "-q", str(v)], check=True)
    (v / ".memaix.db").write_text("db")
    (v / ".memaix.db-wal").write_text("wal")
    (v / "a.md").write_text("a")
    gitvault.commit(v, [], "m", all_tracked=True)
    tracked = subprocess.run(["git", "-C", str(v), "ls-files"], capture_output=True, text=True).stdout.split()
    assert tracked == ["a.md"]


def _redeem(adm_env, issuer, user="erin", reset=False, mutate=None):
    admin, acl, inv = adm_env
    tok = inv.issue(user, issuer, reset=reset)
    if mutate:
        admin._writer.update(mutate)
    return admin.accept_invite(tok, "a-long-enough-password")


def test_reset_rechecked_after_grants_change(adm):
    admin, _, _ = adm
    with pytest.raises(AccessError):
        _redeem(adm, "alice", reset=True,
                mutate=lambda d: d["users"]["erin"]["grants"].__setitem__("beta", "reader"))


def test_reset_ok_for_owner_admin_and_self(adm):
    admin, _, _ = adm
    assert _redeem(adm, "alice", reset=True) == "erin"
    admin._writer.update(lambda d: d["users"].__setitem__("root", {"admin": True, "grants": {}}))
    assert _redeem(adm, "root", reset=True,
                   mutate=lambda d: d["users"]["erin"]["grants"].__setitem__("beta", "reader")) == "erin"
    assert _redeem(adm, "erin", reset=True) == "erin"


def test_redeem_fails_closed_for_missing_or_disabled_issuer(adm):
    with pytest.raises(AccessError):
        _redeem(adm, "ghost")
    with pytest.raises(AccessError):
        _redeem(adm, "alice", mutate=lambda d: d["users"]["alice"].__setitem__("disabled", True))


def test_git_run_passes_empty_sshcommand(monkeypatch, tmp_path):
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append(cmd)
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    gitvault.run(tmp_path, ["--version"], check=False)
    assert "core.sshCommand=" in seen[0] and "core.hooksPath=/dev/null" in seen[0]
