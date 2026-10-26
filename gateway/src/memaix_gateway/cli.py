# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin CLI: python -m memaix_gateway.cli <command>.

Commands:
  hash-password  — generate a PBKDF2-HMAC-SHA256 password hash in the
                   salt_hex:key_hex format that login-app/auth.py and the
                   board UI verify against (200 000 iterations). Paste the
                   output into acl.yaml (users.<id>.password_hash) or set it
                   as MEMAIX_LOGIN_PASSWORD_HASH_<USER>.
  link-imap      — link a per-user IMAP mailbox (docs/FEATURE-MAIL-DIALOG.md).
                   The password is read from stdin (one line), never from
                   argv, so it stays out of shell history, `ps` and logs:
                     printf '%s\n' "$PW" | docker compose exec -T gateway \
                       python -m memaix_gateway.cli link-imap --user jimmy \
                       --account info@example.se --host imap.purelymail.com \
                       --projects memaix,jimlov
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
    if acl is None or store is None:
        from .server import _get_acl, _get_token_store

        acl = acl or _get_acl()
        store = store or _get_token_store()
    if args.user not in acl.users:
        print(f"Okänd användare: {args.user}", file=sys.stderr)
        return 1
    projects = [p.strip() for p in (args.projects or "").split(",") if p.strip()]
    if not projects:
        print("Ange --projects (kommaseparerat, eller *) — ett konto utan projekt syns ingenstans.", file=sys.stderr)
        return 1
    from .acl import AccessDenied

    for project in projects:
        if project == "*":
            continue
        try:
            acl.enforce(args.user, project, "reader")
        except AccessDenied:
            print(f"{args.user} har ingen åtkomst till projektet {project}", file=sys.stderr)
            return 1
    imap_user = args.imap_user or args.account
    if args.verify:
        try:
            verify(args.host, imap_user, password, args.port)
        except Exception as exc:  # noqa: BLE001 - shown without the password
            print(f"IMAP-inloggning misslyckades: {type(exc).__name__}", file=sys.stderr)
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
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        return _cmd_hash_password()
    if args.command == "link-imap":
        return cmd_link_imap(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
