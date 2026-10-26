# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve single mailboxes for the dialog mail client
(docs/FEATURE-MAIL-DIALOG.md).

registry.get_all() builds (and logs into) every source of ONE project.
The overview spans every project the user can read, and a linked mailbox
shared with seven projects would be logged into seven times. Here each
mailbox is planned once — under the first project (alphabetically) that
may use it — and only built when the overview actually reads it.

Labels are the registry's own ("imap:{project}" for a project's acl.yaml
mailbox, "{type}:{account}" for a linked account), so ids stay compatible
with email_list/email_read.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable

from ..acl import AccessDenied


@dataclass(frozen=True)
class PlannedSource:
    project: str
    label: str
    account: str
    build: Callable[[], Any]


def _shared_cfg(acl, project: str) -> dict | None:
    cfg = acl.resource(project, "mailbox")
    return cfg if isinstance(cfg, dict) and cfg else None


def _per_user_mail_specs(registry) -> list:
    return [s for s in registry.specs_for("mail") if s.auth == "per_user"]


def _can(acl, user: str, project: str, role: str) -> bool:
    try:
        acl.enforce(user, project, role)
        return True
    except AccessDenied:
        return False


def plan_sources(acl, token_store, registry, user: str, projects: list[str] | None = None) -> list[PlannedSource]:
    """Every mailbox the user may read, once each, with the project whose
    grant is used. `projects` narrows the projects considered."""
    candidates = [p for p in acl.visible_projects(user) if projects is None or p in projects]
    candidates = [p for p in candidates if _can(acl, user, p, "collaborator")]
    out: list[PlannedSource] = []
    seen_accounts: set[str] = set()
    seen_labels: set[str] = set()

    def add(project: str, label: str, account: str) -> None:
        key = (account or label).lower()
        if key in seen_accounts or label in seen_labels:
            return
        seen_accounts.add(key)
        seen_labels.add(label)
        out.append(PlannedSource(
            project, label, account or label,
            lambda: build_source(acl, token_store, registry, user, project, label),
        ))

    for project in candidates:
        cfg = _shared_cfg(acl, project)
        if cfg is not None:
            type_ = cfg.get("type", "imap")
            spec = registry.get_spec("mail", type_)
            if spec is not None and spec.auth == "shared":
                add(project, f"{type_}:{project}", cfg.get("user", ""))

    accounts = token_store.list_accounts(user) if token_store is not None else []
    for spec in _per_user_mail_specs(registry):
        provider = spec.provider or spec.type
        for account in accounts:
            if account["provider"] != provider or account.get("status") == "needs_relink":
                continue
            project = next(
                (p for p in candidates
                 if token_store.is_allowed(user, provider, account["account"], "mail", p)),
                None,
            )
            if project is not None:
                add(project, f"{spec.type}:{account['account']}", account["account"])
    return out


def build_source(acl, token_store, registry, user: str, project: str, label: str):
    """Build one logged-in adapter for `label` as seen from `project`.

    Re-checks everything the registry would: the project still has the
    shared mailbox, or the linked account is still scoped to the project
    and not flagged needs_relink.
    """
    type_, _, rest = label.partition(":")
    spec = registry.get_spec("mail", type_)
    if spec is None:
        raise ValueError(f"okänd lådtyp i {label!r}")
    if spec.auth == "shared":
        cfg = _shared_cfg(acl, project)
        if rest != project or cfg is None or cfg.get("type", "imap") != type_:
            raise PermissionError(f"lådan {label!r} hör inte till projektet {project!r}")
        return spec.factory(acl, project, user, cfg, None)
    provider = spec.provider or spec.type
    linked = {a["account"]: a for a in token_store.list_accounts(user) if a["provider"] == provider}
    entry = linked.get(rest)
    if entry is None or entry.get("status") == "needs_relink":
        raise PermissionError(f"kontot {rest!r} är inte länkat (eller behöver kopplas om)")
    if not token_store.is_allowed(user, provider, rest, "mail", project):
        raise PermissionError(f"kontot {rest!r} är inte delat med projektet {project!r}")
    token = token_store.load_one(user, provider, rest)
    if token is None:
        raise PermissionError(f"kontot {rest!r} saknar inloggningsuppgifter")
    return spec.factory(acl, project, user, _shared_cfg(acl, project) or {}, token)


def sender_address(acl, project: str, label: str) -> str:
    """The From address for mail sent from `label`."""
    type_, _, rest = label.partition(":")
    if "@" in rest:
        return rest
    cfg = _shared_cfg(acl, project) or {}
    smtp = acl.resource(project, "smtp") or {}
    return smtp.get("from_addr") or cfg.get("user", "")


def _derive_smtp_host(imap_host: str) -> str:
    """imap.example.com -> smtp.example.com (Purelymail: imap./smtp.purelymail.com)."""
    if imap_host.startswith("imap."):
        return "smtp." + imap_host[len("imap."):]
    raise ValueError(
        f"okänd SMTP-server för {imap_host!r} — länka om kontot med smtp_host (se docs/FEATURE-MAIL-DIALOG.md)"
    )


def _smtp_settings(acl, token_store, user: str, project: str, label: str) -> tuple[str, int, str, str]:
    """(host, port, login, password) for sending from `label`. Never logged."""
    from .. import config

    type_, _, rest = label.partition(":")
    if type_ == "imap_user":
        token = token_store.load_one(user, "imap", rest) or {}
        host = token.get("smtp_host") or _derive_smtp_host(token.get("host", ""))
        port = int(token.get("smtp_port") or 587)
        login = token.get("smtp_user") or token.get("user", "")
        password = token.get("password")
        if not password or not login:
            raise ValueError(f"kontot {rest!r} saknar inloggning för SMTP")
        return host, port, login, password
    if type_ == "imap":
        cfg = _shared_cfg(acl, project) or {}
        smtp = acl.resource(project, "smtp") or {}
        host = smtp.get("host") or _derive_smtp_host(cfg.get("host", ""))
        port = int(smtp.get("port", 587))
        login = smtp.get("user") or cfg.get("user", "")
        password = config.secret(smtp.get("password_ref") or cfg.get("password_ref"))
        if password is None:
            raise ValueError(f"inget lösenord konfigurerat för projektets låda ({project!r})")
        return host, port, login, password
    raise ValueError("utskick stöds bara från IMAP-lådor (inte Gmail/Outlook-kopplade konton)")


def smtp_send(acl, token_store, user: str, project: str, label: str, msg: EmailMessage) -> None:
    """Send `msg` through the mailbox's own SMTP server (TLS verified)."""
    import smtplib

    host, port, login, password = _smtp_settings(acl, token_store, user, project, label)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as s:
            s.login(login, password)
            s.send_message(msg)
        return
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=context)
        s.login(login, password)
        s.send_message(msg)
