# SPDX-License-Identifier: AGPL-3.0-or-later
"""mailbox-set — write a SHARED team mailbox resource into acl.yaml.

This is the missing CLI counterpart to the manual step docs/ACCESS-ADMIN.md
("Ett delat teamkonto") used to describe as "inget CLI-kommando för det
här — en vanlig konfigurationsrad i acl.yaml plus en hemlighet i .env".
It writes exactly that configuration-rad, through code, instead of a human
hand-editing two files.

Scope: a SHARED mailbox one project reads as one identity (`info@firma.se`
read by everyone with access to the project) — not a person's own inbox.
That per-person mechanism is `account_link_imap` / `cli.py link-imap`
(docs/FEATURE-MAIL-DIALOG.md) and is untouched by this module.

Admin CLI only (`python -m memaix_gateway.cli mailbox-set`) — the same
administrative surface as `link-imap`/`backend-status`: whoever can run it
already has shell access to the gateway container (`docker compose exec`),
which is the real admin boundary here. There is no acl.yaml "actor" check
the way access_admin.py's in-process MCP tools have one — this is an
instance-wide operation a project owner cannot reach any other way, by
design (the task that introduced this module says so explicitly).

Two things get written, both through existing, already-reviewed paths —
never a new parallel writer:
  * the password into ``<secrets_dir>/mailbox-<project>`` — 0700 directory,
    0600 file, the same pattern nextcloud_provision._write_secret already
    uses (docs/SECRETS.md). Without ``force``: created O_EXCL (never
    silently overwritten). With ``force``: written to a sibling ``.tmp``
    file first and swapped in with ``os.replace`` only once the acl.yaml
    write has actually succeeded, so a failed ACL write never leaves a
    mailbox with a new password pointing at its old host/user (or vice
    versa) — see ``set_mailbox``'s try/except below.
  * ``projects.<project>.mailbox`` (and ``.smtp``, only if SMTP flags were
    given) into acl.yaml via the existing ``AclWriter`` (web/acl_writer.py)
    — the same atomic, backed-up write path access_admin.py uses for every
    other acl.yaml mutation.

Fail-closed:
  * unknown project -> error, nothing written at all.
  * a project that already has a ``mailbox:`` resource -> error unless
    ``force=True`` (never silently replaced).
  * ``smtp_host`` given for a project that already has an ``smtp:``
    resource -> same error unless ``force=True`` (the second path to a
    mailbox's send credentials gets the same guard as the first — DoD
    "samma spärr på varje väg").
  * ``force=True`` with no ``smtp_host`` drops any existing ``smtp:``
    resource rather than leaving it pointing at the *previous* mailbox's
    account — ``connectors/mail_accounts.py``'s ``_project_smtp`` prefers
    ``smtp.user``/``smtp.password_ref`` over the mailbox's, so a stale
    ``smtp:`` after swapping `info@old` for `info@new` would keep sending
    through the old account's credentials.
  * malformed email, empty host, empty/whitespace-padded password -> error.
  * ``port`` other than the shared IMAP adapter's fixed default (993) ->
    error, rather than silently accepting a value nothing reads (see
    ``set_mailbox``'s docstring).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .web.acl_writer import AclWriter

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

# tools/email.py's `_make_mailbox` (the shared-mailbox IMAP adapter this
# resource feeds) calls `MailBox(cfg["host"], ...)` with no port argument at
# all — unlike the per-user link-imap path (mail_imap_user.py's
# `build_mailbox`, explicitly "never imported by tools/email.py"), which
# does read a stored port. --port is therefore validated but never WRITTEN
# to the resource: storing an unread "port" key would be inventing schema
# (the task explicitly asks to stay exactly to docs/BACKENDS.md's
# host/user/password_ref) and would fail OPEN — the admin would believe a
# non-default port is configured while the connection silently keeps using
# the fixed default below.
_FIXED_IMAP_PORT = 993


class MailboxAdminError(Exception):
    """Invalid request: bad project/email/host/password/port, or an
    existing mailbox/smtp resource without --force. Never carries the
    password."""


def _secret_path(secrets_dir: Path, project: str) -> Path:
    return Path(secrets_dir) / f"mailbox-{project}"


def _write_secret_exclusive(path: Path, password: str) -> None:
    """Create the 0600 secret file (0700 parent dir); never overwrites one
    that already exists (e.g. an orphan left by an earlier failed run)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise MailboxAdminError(
            f"secret file already exists: {path} (pass force=True/--force to replace it)"
        ) from None
    with os.fdopen(fd, "w") as fh:
        fh.write(password)


def _write_secret_tmp(tmp_path: Path, password: str) -> None:
    """Write the 0600 secret to a sibling .tmp path (0700 parent dir); the
    caller swaps it into place only after the acl.yaml write succeeds."""
    tmp_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(password)


def set_mailbox(
    writer: AclWriter,
    secrets_dir: Path,
    project: str,
    email: str,
    host: str,
    password: str,
    *,
    port: int | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    force: bool = False,
) -> dict:
    """Write the shared ``mailbox:`` (+ optional ``smtp:``) resource for
    *project*. Returns {"project", "mailbox": {...}} — never the password.

    *port*, if given, must be 993 (the shared IMAP adapter's fixed,
    unconfigurable port) — see the module-level note on _FIXED_IMAP_PORT.
    It is accepted (rather than simply refused outright) only for symmetry
    with link-imap's --port, as a way to say "yes, I know, and it's 993" —
    it is validated but never written to the resource, so any OTHER value
    is rejected rather than silently stored-but-ignored.
    """
    if not isinstance(project, str) or not _PROJECT_RE.match(project):
        raise MailboxAdminError("project name must be 2-32 chars of a-z, 0-9, '-' or '_'")
    if not isinstance(email, str) or not _EMAIL_RE.match(email):
        raise MailboxAdminError(f"not a valid email address: {email!r}")
    if not host or not isinstance(host, str):
        raise MailboxAdminError("host is required")
    if not isinstance(password, str) or not password or password != password.strip():
        raise MailboxAdminError(
            "password must be non-empty with no leading/trailing whitespace"
        )
    if port is not None and port != _FIXED_IMAP_PORT:
        raise MailboxAdminError(
            f"port: the shared IMAP adapter always connects on {_FIXED_IMAP_PORT} "
            f"and does not read a configured port; omit --port or pass {_FIXED_IMAP_PORT}"
        )

    secret_path = _secret_path(secrets_dir, project)
    tmp_path = secret_path.with_name(secret_path.name + ".tmp") if force else None
    created_exclusive = False

    def apply(data: dict) -> dict:
        nonlocal created_exclusive
        projects = data.get("projects", {})
        if project not in projects:
            raise MailboxAdminError(f"unknown project: {project}")
        proj = projects[project]
        if proj.get("mailbox") is not None and not force:
            raise MailboxAdminError(
                f"{project} already has a mailbox resource; pass force=True/--force to replace it"
            )
        if smtp_host and proj.get("smtp") is not None and not force:
            raise MailboxAdminError(
                f"{project} already has an smtp resource; pass force=True/--force to replace it"
            )

        if force:
            _write_secret_tmp(tmp_path, password)  # type: ignore[arg-type]
        else:
            _write_secret_exclusive(secret_path, password)
            created_exclusive = True

        mailbox: dict = {"host": host, "user": email, "password_ref": f"file:{secret_path}"}
        proj["mailbox"] = mailbox

        if smtp_host:
            smtp: dict = {"host": smtp_host}
            if smtp_port is not None:
                smtp["port"] = int(smtp_port)
            proj["smtp"] = smtp
        elif force:
            # A forced mailbox swap with no new --smtp-host must not leave
            # an old smtp: resource pointing at the previous account's
            # credentials (mail_accounts.py's _project_smtp prefers smtp:
            # over mailbox: for both user and password_ref).
            proj.pop("smtp", None)

        return {"project": project, "mailbox": mailbox}

    try:
        result = writer.update(apply)
    except BaseException:
        if created_exclusive:
            secret_path.unlink(missing_ok=True)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    if force:
        os.replace(tmp_path, secret_path)  # type: ignore[arg-type]
    return result
