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
    0600 file, created O_EXCL (never silently overwritten) the same way
    nextcloud_provision._write_secret already does it (docs/SECRETS.md).
  * ``projects.<project>.mailbox`` (and ``.smtp``, only if SMTP flags were
    given) into acl.yaml via the existing ``AclWriter``
    (web/acl_writer.py) — the same atomic, backed-up write path
    access_admin.py uses for every other acl.yaml mutation.

Fail-closed:
  * unknown project -> error, nothing written at all.
  * a project that already has a ``mailbox:`` resource -> error unless
    ``force=True`` (never silently replaced).
  * malformed email, empty host, empty/whitespace-padded password -> error.

Known limitation (see docs/ACCESS-ADMIN.md): the running gateway caches
acl.yaml in-process (``server._acl``); a CLI-written change only takes
effect after ``docker compose restart gateway``, unlike the login-app,
which re-reads acl.yaml on every request.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .web.acl_writer import AclWriter

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class MailboxAdminError(Exception):
    """Invalid request: bad project/email/host/password, or an existing
    mailbox resource without --force. Never carries the password."""


def _secret_path(secrets_dir: Path, project: str) -> Path:
    return Path(secrets_dir) / f"mailbox-{project}"


def _write_secret(path: Path, password: str, *, force: bool) -> None:
    """Create the 0600 secret file (0700 parent dir).

    Without ``force``: O_EXCL create, so an existing file (e.g. a leftover
    from a failed earlier run, or someone else's resource) is never
    silently overwritten — raises MailboxAdminError instead.
    With ``force``: write-to-temp-then-rename, so a write that fails
    partway (disk full, permissions) never corrupts or truncates a secret
    that was still in use.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not force:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise MailboxAdminError(
                f"secret file already exists: {path} (pass force=True/--force to replace it)"
            ) from None
        with os.fdopen(fd, "w") as fh:
            fh.write(password)
        return

    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(password)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


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

    *port* is accepted for symmetry with ``link-imap`` and stored on the
    resource, but the current shared-IMAP adapter (``tools/email.py``'s
    ``_make_mailbox``) does not read it yet — only ``host``/``user``/
    ``password_ref`` are consumed for IMAP today. SMTP's ``host``/``port``
    on the other hand ARE read (``_smtp_deliver``), from the sibling
    ``smtp:`` resource this function writes when ``smtp_host`` is given.
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

    secret_path = _secret_path(secrets_dir, project)

    def apply(data: dict) -> dict:
        projects = data.get("projects", {})
        if project not in projects:
            raise MailboxAdminError(f"unknown project: {project}")
        existing = projects[project].get("mailbox")
        if existing is not None and not force:
            raise MailboxAdminError(
                f"{project} already has a mailbox resource; pass force=True/--force to replace it"
            )

        # Only touch disk once every check above has passed, and still
        # inside the writer's lock, so a concurrent run of this same
        # command can't both pass the check and then both write.
        _write_secret(secret_path, password, force=force)

        mailbox: dict = {"host": host, "user": email, "password_ref": f"file:{secret_path}"}
        if port is not None:
            mailbox["port"] = int(port)
        projects[project]["mailbox"] = mailbox

        if smtp_host:
            smtp: dict = {"host": smtp_host}
            if smtp_port is not None:
                smtp["port"] = int(smtp_port)
            projects[project]["smtp"] = smtp

        return {"project": project, "mailbox": mailbox}

    return writer.update(apply)
