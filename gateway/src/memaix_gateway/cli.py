# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin CLI: python -m memaix_gateway.cli <command>.

Commands:
  hash-password  — generate a PBKDF2-HMAC-SHA256 password hash in the
                   salt_hex:key_hex format that login-app/auth.py and the
                   board UI verify against (200 000 iterations). Paste the
                   output into acl.yaml (users.<id>.password_hash) or set it
                   as MEMAIX_LOGIN_PASSWORD_HASH_<USER>.
  link-imap      — link a per-user IMAP mailbox (docs/FEATURE-MAIL-DIALOG.md).
                   This gives the ONE user named --user their OWN mailbox,
                   scoped into --projects; it does not make a shared "team
                   inbox" (every teammate who wants their own mail linked
                   runs this once for themselves). For a mailbox the whole
                   team shares as one identity (e.g. info@example.se read by
                   everyone), use acl.yaml's `mailbox:` resource with a
                   `password_ref` secret instead (docs/BACKENDS.md) — this
                   command is not that.
                   The password is read from stdin (one line), never from
                   argv, so it stays out of shell history, `ps` and logs:
                     read -rs PW   # prompts, doesn't echo, not in history
                     printf '%s\n' "$PW" | docker compose exec -T gateway \
                       python -m memaix_gateway.cli link-imap --user jimmy \
                       --account info@example.se --host imap.purelymail.com \
                       --projects memaix,jimlov --verify
                   (-T is required when piping stdin into `exec` -- without
                   it docker refuses with "the input device is not a TTY".
                   For a one-off run without a pipe, drop -T and --user's
                   password prompt (getpass) appears instead.)
                   Pass --verify to log in over IMAP before anything is
                   stored, so a typo'd host/port/password fails loudly here
                   instead of silently at the next email_list.
  backend-status — list project mail/calendar resources declared
                   `awaiting_link` in acl.yaml that nothing has linked yet
                   (docs/ACCESS-ADMIN.md §Koppla teamets mejl). Needs the
                   running gateway's TokenStore, so like link-imap it only
                   makes sense via `docker compose exec`, not on the host:
                     docker compose exec -T gateway \
                       python -m memaix_gateway.cli backend-status
  mailbox-set    — set up a SHARED team mailbox (e.g. info@example.se, read
                   as the same identity by everyone with access to the
                   project) for an instance-wide system admin to run — this
                   is not a person's own inbox (that's link-imap above).
                   Writes acl.yaml's `mailbox:` resource for --project and
                   stores the password as a 0600 secret file under
                   config/secrets/ (docs/ACCESS-ADMIN.md §Ett delat
                   teamkonto); refuses to replace an existing mailbox
                   resource for the same project unless --force is given.
                   Password on stdin, same as link-imap:
                     printf '%s\n' "$PW" | docker compose exec -T gateway \
                       python -m memaix_gateway.cli mailbox-set acme \
                       info@acme.se --host imap.purelymail.com
                   Restart the gateway afterwards (`docker compose restart
                   gateway`) — like every acl.yaml change made outside the
                   admin web UI, it only takes effect once the running
                   process re-reads the file.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import secrets
import sys

# Must match login-app/auth.py::pbkdf2_check and board/routes.py::_verify_password.
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return salt_hex:key_hex for *password* (PBKDF2-HMAC-SHA256)."""
    if salt is None:
        salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{derived.hex()}"


def _cmd_hash_password() -> int:
    pw = getpass.getpass("Lösenord: ")
    confirm = getpass.getpass("Bekräfta: ")
    if pw != confirm:
        print("Lösenorden matchar inte.", file=sys.stderr)
        return 1
    if not pw:
        print("Tomt lösenord vägras.", file=sys.stderr)
        return 1
    print(hash_password(pw))
    print(
        "\nKlistra in under users.<id>.password_hash i acl.yaml,\n"
        "eller sätt MEMAIX_LOGIN_PASSWORD_HASH_<USER> i .env",
        file=sys.stderr,
    )
    return 0


def _read_password(stdin=None) -> str:
    """One line from stdin (trailing newline only stripped), or a prompt on a tty."""
    stream = stdin if stdin is not None else sys.stdin
    if stdin is None and stream.isatty():
        return getpass.getpass("IMAP-lösenord: ")
    line = stream.readline()
    return line[:-1] if line.endswith("\n") else line


def _verify_imap(host: str, user: str, password: str, port: int | None) -> None:
    from .connectors.adapters.mail_imap_user import build_mailbox

    token: dict = {"host": host, "user": user, "password": password}
    if port:
        token["port"] = port
    build_mailbox(token).logout()


def _acl_and_store(acl, store):
    """The given acl and token store, else the server's."""
    if acl is None or store is None:
        from .server import _get_acl, _get_token_store

        acl = acl or _get_acl()
        store = store or _get_token_store()
    return acl, store


def _link_projects(acl, args) -> list[str] | None:
    """Projects to share the account with, or None after saying why not:
    unknown user, no projects, or a listed project the user cannot read."""
    if args.user not in acl.users:
        print(f"Okänd användare: {args.user}", file=sys.stderr)
        return None
    projects = [p.strip() for p in (args.projects or "").split(",") if p.strip()]
    if not projects:
        print("Ange --projects (kommaseparerat, eller *) — ett konto utan projekt syns ingenstans.", file=sys.stderr)
        return None
    from .acl import AccessDenied

    for project in projects:
        if project == "*":
            continue
        try:
            acl.enforce(args.user, project, "reader")
        except AccessDenied:
            print(f"{args.user} har ingen åtkomst till projektet {project}", file=sys.stderr)
            return None
    return projects


def _verified(args, verify, imap_user: str, password: str) -> bool:
    """False (after printing the error type only) if --verify login fails."""
    if not args.verify:
        return True
    try:
        verify(args.host, imap_user, password, args.port)
    except Exception as exc:  # noqa: BLE001
        # shown without the password: the type only
        print(f"IMAP-inloggning misslyckades: {type(exc).__name__}", file=sys.stderr)
        return False
    return True


def cmd_link_imap(args, *, stdin=None, acl=None, store=None, verify=_verify_imap) -> int:
    """Store an IMAP account for args.user and share it for mail with
    args.projects. Prints no secret; returns a process exit code."""
    import os

    if store is None and not os.environ.get("TOKEN_MASTER_KEY"):
        print("TOKEN_MASTER_KEY saknas — vägrar skriva (raden skulle inte gå att läsa i servern).", file=sys.stderr)
        return 1
    password = _read_password(stdin)
    if not password:
        print("Tomt lösenord på stdin — inget länkades.", file=sys.stderr)
        return 1
    acl, store = _acl_and_store(acl, store)
    projects = _link_projects(acl, args)
    if projects is None:
        return 1
    imap_user = args.imap_user or args.account
    if not _verified(args, verify, imap_user, password):
        return 1
    from .tools.account import account_link_imap

    account_link_imap(
        args.user, args.account, args.host, imap_user, password, store, port=args.port,
        smtp_host=args.smtp_host, smtp_port=args.smtp_port,
    )
    del password
    stored = store.set_scopes(args.user, "imap", args.account, "mail", projects)
    print(f"Länkade {args.account} för {args.user}; mejl delas med: {', '.join(stored)}")
    return 0


def cmd_backend_status(args, *, acl=None, store=None) -> int:
    """Print one line per (project, capability) declared `awaiting_link`.
    Fails closed: a missing TOKEN_MASTER_KEY (the gateway's own guard, see
    cmd_link_imap) is an error, never silently "nothing pending" — reading
    `scoped` status without the token store would otherwise look identical
    to an instance with nothing waiting. An unknown --project is likewise an
    error, not a silent empty list — a typo'd project name must not read as
    "everything is fine here"."""
    import os

    if store is None and not os.environ.get("TOKEN_MASTER_KEY"):
        print("TOKEN_MASTER_KEY saknas — kan inte läsa kopplingsstatus.", file=sys.stderr)
        return 1
    acl, store = _acl_and_store(acl, store)

    project = getattr(args, "project", None)
    if project and project not in acl.projects:
        print(f"Okänt projekt: {project}", file=sys.stderr)
        return 1

    from .backend_status import collect_status

    entries = collect_status(acl, store)
    if project:
        entries = [e for e in entries if e.project == project]
    if not entries:
        print("Inget väntar på koppling (eller inget projekt har awaiting_link satt).")
        return 0
    for entry in entries:
        print(entry.line())
    return 1 if any(e.status == "error" for e in entries) else 0


def cmd_mailbox_set(args, *, stdin=None, writer=None, secrets_dir=None) -> int:
    """Write a SHARED mailbox resource for args.project. Prints no secret;
    returns a process exit code. See `mailbox-set`'s help text in main()."""
    password = _read_password(stdin)
    if not password:
        print("Tomt lösenord på stdin — ingenting skrevs.", file=sys.stderr)
        return 1

    from pathlib import Path

    from .mailbox_admin import MailboxAdminError, set_mailbox
    from .web.acl_writer import AclWriter

    if writer is None:
        from . import config

        writer = AclWriter(config.CONFIG_DIR / "acl.yaml")
    if secrets_dir is None:
        from . import config

        secrets_dir = config.CONFIG_DIR / "secrets"

    try:
        out = set_mailbox(
            writer,
            Path(secrets_dir),
            args.project,
            args.email,
            args.host,
            password,
            port=args.port,
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            force=args.force,
        )
    except MailboxAdminError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        del password

    print(
        f"Satte delad mailbox för {out['project']}: {out['mailbox']['user']} "
        f"@ {out['mailbox']['host']}. Starta om gatewayen för att den läses in."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m memaix_gateway.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hash-password", help="generate a PBKDF2 password hash for acl.yaml/.env")
    link = sub.add_parser("link-imap", help="link a per-user IMAP mailbox; password on stdin")
    link.add_argument("--user", required=True, help="Memaix user id (acl.yaml)")
    link.add_argument("--account", required=True, help="mailbox address, e.g. info@example.se")
    link.add_argument("--host", required=True, help="IMAP host, e.g. imap.purelymail.com")
    link.add_argument("--port", type=int, default=None, help="IMAP port (default 993)")
    link.add_argument("--imap-user", default=None, help="IMAP login if not the address")
    link.add_argument("--smtp-host", default=None, help="SMTP host (default: imap. -> smtp.)")
    link.add_argument("--smtp-port", type=int, default=None, help="SMTP port (default 587; 465 = TLS)")
    link.add_argument("--projects", required=True, help="comma-separated projects to share mail with, or *")
    link.add_argument("--verify", action="store_true", help="log in over IMAP before storing")
    status = sub.add_parser(
        "backend-status",
        help="list project mail/calendar resources declared awaiting_link that nothing has linked yet",
    )
    status.add_argument("--project", default=None, help="show only this project (default: all)")
    mailbox = sub.add_parser(
        "mailbox-set",
        help="set up a SHARED team mailbox for a project (system admin only); password on stdin",
    )
    mailbox.add_argument("project", help="Memaix project name (acl.yaml)")
    mailbox.add_argument("email", help="shared mailbox address, e.g. info@example.se")
    mailbox.add_argument("--host", required=True, help="IMAP host, e.g. imap.purelymail.com")
    mailbox.add_argument(
        "--port", type=int, default=None,
        help="IMAP port (accepted, not yet read by the shared-IMAP adapter)",
    )
    mailbox.add_argument("--smtp-host", default=None, help="SMTP host for outgoing mail from this mailbox")
    mailbox.add_argument("--smtp-port", type=int, default=None, help="SMTP port (default 587, STARTTLS)")
    mailbox.add_argument(
        "--force", action="store_true",
        help="replace an existing mailbox resource for this project instead of refusing",
    )
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        return _cmd_hash_password()
    if args.command == "link-imap":
        return cmd_link_imap(args)
    if args.command == "backend-status":
        return cmd_backend_status(args)
    if args.command == "mailbox-set":
        return cmd_mailbox_set(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
