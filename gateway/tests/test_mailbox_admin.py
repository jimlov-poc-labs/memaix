# SPDX-License-Identifier: AGPL-3.0-or-later
"""mailbox-set — shared team mailbox CLI/admin command.

Fail-closed is the point of this command: an existing mailbox resource
must never be silently replaced, an unknown project must never write
anything, and the password must never leak into stdout, stderr, logs or
acl.yaml itself.
"""

from __future__ import annotations

import argparse
import io
import logging
import stat
from pathlib import Path

import pytest
import yaml

from memaix_gateway.mailbox_admin import MailboxAdminError, set_mailbox
from memaix_gateway.web.acl_writer import AclWriter

_SECRET_PW = "s3cr3t-super-not-leaked-xyz"

_BASE = {
    "users": {"root": {"admin": True, "grants": {"acme": "owner"}}},
    "projects": {
        "acme": {"vault": "/srv/vaults/acme", "allow_send": False},
        "beta": {
            "vault": "/srv/vaults/beta",
            "allow_send": False,
            "mailbox": {"host": "imap.old.se", "user": "old@beta.se", "password_ref": "env:OLD_PW"},
        },
        "gamma": {
            "vault": "/srv/vaults/gamma",
            "allow_send": False,
            "mailbox": {"host": "imap.old.se", "user": "old@gamma.se", "password_ref": "env:OLD_PW"},
            "smtp": {"host": "smtp.old.se", "user": "old@gamma.se", "password_ref": "env:OLD_SMTP_PW"},
        },
        # smtp: without mailbox: isn't a realistic hand-written config, but
        # isolates the smtp: conflict check below from the mailbox: one.
        "delta": {
            "vault": "/srv/vaults/delta",
            "allow_send": False,
            "smtp": {"host": "smtp.old.se", "user": "old@delta.se", "password_ref": "env:OLD_SMTP_PW"},
        },
    },
}


@pytest.fixture()
def env(tmp_path: Path):
    acl_path = tmp_path / "acl.yaml"
    acl_path.write_text(yaml.safe_dump(_BASE), encoding="utf-8")
    secrets_dir = tmp_path / "secrets"

    class Env:
        pass

    e = Env()
    e.acl_path = acl_path
    e.secrets_dir = secrets_dir
    e.writer = AclWriter(acl_path)

    def data() -> dict:
        return yaml.safe_load(acl_path.read_text(encoding="utf-8"))

    e.data = data
    return e


# ------------------------------------------------------------------
# set_mailbox — the admin function itself
# ------------------------------------------------------------------


def test_set_mailbox_writes_acl_and_secret_file(env):
    out = set_mailbox(
        env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW,
    )
    mailbox = env.data()["projects"]["acme"]["mailbox"]
    assert mailbox["host"] == "imap.acme.se"
    assert mailbox["user"] == "info@acme.se"
    ref = mailbox["password_ref"]
    assert ref.startswith("file:")
    secret_path = Path(ref[len("file:"):])
    assert secret_path.read_text() == _SECRET_PW
    assert out["mailbox"]["password_ref"] == ref
    assert "password" not in out["mailbox"]


def test_secret_file_is_0600_and_dir_0700(env):
    set_mailbox(env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW)
    secret_path = env.secrets_dir / "mailbox-acme"
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.secrets_dir.stat().st_mode) == 0o700


def test_smtp_flags_write_sibling_smtp_resource(env):
    set_mailbox(
        env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW,
        smtp_host="smtp.acme.se", smtp_port=587,
    )
    smtp = env.data()["projects"]["acme"]["smtp"]
    assert smtp == {"host": "smtp.acme.se", "port": 587}


def test_no_smtp_flags_writes_no_smtp_resource(env):
    set_mailbox(env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW)
    assert "smtp" not in env.data()["projects"]["acme"]


def test_smtp_host_refused_over_existing_smtp_without_force(env):
    before = env.data()["projects"]["delta"]["smtp"]
    with pytest.raises(MailboxAdminError, match="already has an smtp"):
        set_mailbox(
            env.writer, env.secrets_dir, "delta", "new@delta.se", "imap.new.se", _SECRET_PW,
            smtp_host="smtp.new.se",
        )
    assert env.data()["projects"]["delta"]["smtp"] == before
    assert "mailbox" not in env.data()["projects"]["delta"]


def test_force_without_smtp_host_drops_stale_smtp(env):
    """A forced mailbox swap with no new --smtp-host must not leave the old
    smtp: resource pointing at the previous account's send credentials
    (mail_accounts.py's _project_smtp prefers smtp: over mailbox:)."""
    set_mailbox(
        env.writer, env.secrets_dir, "gamma", "new@gamma.se", "imap.new.se", _SECRET_PW, force=True,
    )
    assert "smtp" not in env.data()["projects"]["gamma"]
    assert env.data()["projects"]["gamma"]["mailbox"]["user"] == "new@gamma.se"


def test_force_with_smtp_host_replaces_smtp(env):
    set_mailbox(
        env.writer, env.secrets_dir, "gamma", "new@gamma.se", "imap.new.se", _SECRET_PW,
        smtp_host="smtp.new.se", smtp_port=587, force=True,
    )
    assert env.data()["projects"]["gamma"]["smtp"] == {"host": "smtp.new.se", "port": 587}


def test_port_993_is_accepted_but_not_stored(env):
    """993 is accepted as a no-op confirmation, not written: nothing reads
    a port on the shared IMAP resource (see mailbox_admin's module docs),
    so writing one — even the correct value — would invent schema."""
    out = set_mailbox(
        env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW, port=993,
    )
    assert "port" not in out["mailbox"]
    assert "port" not in env.data()["projects"]["acme"]["mailbox"]


def test_non_default_port_rejected(env):
    """The shared IMAP adapter never reads a configured port (tools/email.py
    _make_mailbox) — accepting and storing an unread value would fail open:
    the admin would believe 1993 is configured while 993 is actually used."""
    with pytest.raises(MailboxAdminError, match="port"):
        set_mailbox(
            env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW, port=1993,
        )
    assert "mailbox" not in env.data()["projects"]["acme"]


def test_unknown_project_writes_nothing(env):
    with pytest.raises(MailboxAdminError, match="unknown project"):
        set_mailbox(env.writer, env.secrets_dir, "nope", "info@nope.se", "imap.nope.se", _SECRET_PW)
    assert "nope" not in env.data()["projects"]
    assert not env.secrets_dir.exists()


def test_existing_mailbox_refused_without_force(env):
    before = env.data()["projects"]["beta"]["mailbox"]
    with pytest.raises(MailboxAdminError, match="already has a mailbox"):
        set_mailbox(env.writer, env.secrets_dir, "beta", "new@beta.se", "imap.new.se", _SECRET_PW)
    # acl.yaml is untouched: the old resource is exactly as it was.
    assert env.data()["projects"]["beta"]["mailbox"] == before


def test_force_replaces_existing_mailbox(env):
    set_mailbox(
        env.writer, env.secrets_dir, "beta", "new@beta.se", "imap.new.se", _SECRET_PW, force=True,
    )
    mailbox = env.data()["projects"]["beta"]["mailbox"]
    assert mailbox["user"] == "new@beta.se"
    assert mailbox["host"] == "imap.new.se"


def test_malformed_email_rejected(env):
    with pytest.raises(MailboxAdminError, match="valid email"):
        set_mailbox(env.writer, env.secrets_dir, "acme", "not-an-email", "imap.acme.se", _SECRET_PW)
    assert "mailbox" not in env.data()["projects"]["acme"]


def test_empty_host_rejected(env):
    with pytest.raises(MailboxAdminError, match="host"):
        set_mailbox(env.writer, env.secrets_dir, "acme", "info@acme.se", "", _SECRET_PW)


def test_empty_password_rejected(env):
    with pytest.raises(MailboxAdminError, match="password"):
        set_mailbox(env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", "")


def test_whitespace_padded_password_rejected(env):
    """config.secret()'s file: resolver strips the file contents (config.py),
    so a stored password with padding would silently differ from what was
    typed — reject it here instead of storing a mismatch."""
    with pytest.raises(MailboxAdminError, match="password"):
        set_mailbox(env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", " pw \n")


def test_failed_acl_write_unlinks_newly_created_secret(env, monkeypatch):
    """If AclWriter._write_atomic blows up AFTER the exclusive secret file
    was created, the orphan must not survive — otherwise a retry without
    --force would wrongly report 'secret file already exists'."""
    monkeypatch.setattr(env.writer, "_write_atomic", lambda data: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        set_mailbox(env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW)
    assert not (env.secrets_dir / "mailbox-acme").exists()
    assert "mailbox" not in env.data()["projects"]["acme"]


def test_failed_acl_write_under_force_keeps_old_password(env, monkeypatch):
    """Under --force the new password is written to a temp file and only
    swapped into place once the acl.yaml write actually succeeds — so a
    failed write must leave the OLD secret completely untouched, not a
    half-replaced one."""
    old_secret = env.secrets_dir / "mailbox-beta"
    old_secret.parent.mkdir(mode=0o700, parents=True)
    old_secret.write_text("the-old-password")
    monkeypatch.setattr(env.writer, "_write_atomic", lambda data: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        set_mailbox(
            env.writer, env.secrets_dir, "beta", "new@beta.se", "imap.new.se", _SECRET_PW, force=True,
        )
    assert old_secret.read_text() == "the-old-password"
    assert not (env.secrets_dir / "mailbox-beta.tmp").exists()
    assert env.data()["projects"]["beta"]["mailbox"]["user"] == "old@beta.se"


def test_orphan_secret_file_refused_without_force(env):
    """A leftover secret file from an earlier failed/aborted run must not be
    silently overwritten either, even though acl.yaml has no mailbox: key."""
    env.secrets_dir.mkdir(mode=0o700, parents=True)
    (env.secrets_dir / "mailbox-acme").write_text("leftover")
    with pytest.raises(MailboxAdminError, match="already exists"):
        set_mailbox(env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW)
    assert "mailbox" not in env.data()["projects"]["acme"]


# ------------------------------------------------------------------
# Password never leaks
# ------------------------------------------------------------------


def test_password_not_in_acl_yaml_text(env):
    set_mailbox(env.writer, env.secrets_dir, "acme", "info@acme.se", "imap.acme.se", _SECRET_PW)
    assert _SECRET_PW not in env.acl_path.read_text(encoding="utf-8")


def test_password_not_leaked_on_any_error_path(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.secrets_dir.mkdir(mode=0o700, parents=True)
    (env.secrets_dir / "mailbox-acme").write_text("leftover")
    for kw, args in (
        ({}, ("nope", "info@nope.se", "imap.nope.se", _SECRET_PW)),
        ({}, ("acme", "not-an-email", "imap.acme.se", _SECRET_PW)),
        ({}, ("acme", "info@acme.se", "", _SECRET_PW)),
        ({}, ("acme", "info@acme.se", "imap.acme.se", _SECRET_PW)),  # orphan secret file
        ({"port": 1993}, ("beta", "new@beta.se", "imap.new.se", _SECRET_PW)),
        ({"smtp_host": "smtp.x.se"}, ("delta", "new@delta.se", "imap.new.se", _SECRET_PW)),
    ):
        try:
            set_mailbox(env.writer, env.secrets_dir, *args, **kw)
        except MailboxAdminError as exc:
            assert _SECRET_PW not in str(exc)
    assert _SECRET_PW not in caplog.text


# ------------------------------------------------------------------
# cli.cmd_mailbox_set — argv/stdin wiring
# ------------------------------------------------------------------


def _args(**kw):
    base = dict(
        project="acme", email="info@acme.se", host="imap.acme.se",
        port=None, smtp_host=None, smtp_port=None, force=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _run(env, *, stdin=_SECRET_PW + "\n", **kw):
    from memaix_gateway import cli

    return cli.cmd_mailbox_set(
        _args(**kw), stdin=io.StringIO(stdin), writer=env.writer, secrets_dir=env.secrets_dir,
    )


def test_cli_happy_path(env, capsys):
    rc = _run(env)
    assert rc == 0
    out = capsys.readouterr()
    assert _SECRET_PW not in out.out
    assert _SECRET_PW not in out.err
    assert "info@acme.se" in out.out


def test_cli_empty_stdin_writes_nothing(env, capsys):
    rc = _run(env, stdin="\n")
    assert rc == 1
    assert "mailbox" not in env.data()["projects"]["acme"]
    out = capsys.readouterr()
    assert _SECRET_PW not in out.err


def test_cli_refuses_existing_without_force(env, capsys):
    rc = _run(env, project="beta", email="new@beta.se")
    assert rc == 1
    out = capsys.readouterr()
    assert "force" in out.err
    assert _SECRET_PW not in out.err
    assert env.data()["projects"]["beta"]["mailbox"]["user"] == "old@beta.se"


def test_cli_force_replaces(env, capsys):
    rc = _run(env, project="beta", email="new@beta.se", force=True)
    assert rc == 0
    assert env.data()["projects"]["beta"]["mailbox"]["user"] == "new@beta.se"


def test_cli_unknown_project(env, capsys):
    rc = _run(env, project="ghost")
    assert rc == 1
    out = capsys.readouterr()
    assert "unknown project" in out.err
    assert "ghost" not in env.data()["projects"]


def test_cli_refuses_smtp_over_existing_without_force(env, capsys):
    rc = _run(env, project="delta", email="new@delta.se", host="imap.new.se", smtp_host="smtp.new.se")
    assert rc == 1
    out = capsys.readouterr()
    assert "smtp" in out.err
    assert _SECRET_PW not in out.err
    assert env.data()["projects"]["delta"]["smtp"]["user"] == "old@delta.se"


def test_cli_non_default_port_rejected(env, capsys):
    rc = _run(env, port=1993)
    assert rc == 1
    out = capsys.readouterr()
    assert "port" in out.err
    assert _SECRET_PW not in out.err


def test_cli_os_error_reports_type_only(env, capsys, monkeypatch):
    def boom(data):
        raise PermissionError("denied")

    monkeypatch.setattr(env.writer, "_write_atomic", boom)
    rc = _run(env)
    assert rc == 1
    out = capsys.readouterr()
    assert "PermissionError" in out.err
    assert _SECRET_PW not in out.err
    assert _SECRET_PW not in out.out
