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
