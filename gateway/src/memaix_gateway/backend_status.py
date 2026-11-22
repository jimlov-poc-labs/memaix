# SPDX-License-Identifier: AGPL-3.0-or-later
"""backend-status — list project mail/calendar resources an admin has
declared but nothing has linked yet.

This is the "awaiting_link" hook the setup wizard (a later, separate step —
not built here, see docs/BACKENDS.md fas 5) will write to once it runs
*after* a container exists and can reach the encrypted TokenStore. Today it
is written by hand in acl.yaml, or left absent (nothing pending).

Scope, deliberately narrow:
- This module only ever READS acl.yaml and the TokenStore's metadata (which
  provider/account/project a token is scoped to) — never a credential's
  cleartext value, never `password_ref`'s target. It cannot answer "is the
  password still valid?", only "does something claim to serve this
  capability for this project?" (the `needs_relink` check below is the one
  exception -- that flag means a refresh already failed, so it is treated
  as known-dead rather than merely unverified).
- A project resource is a SHARED credential (acl.yaml `mailbox`/`calendar`
  + a `*_ref` secret) — see BACKENDS.md. That is not the same thing as a
  per-user link (`account_link_imap`/`cli.py link-imap`), which only ever
  gives the ONE user who ran it their OWN mailbox, scoped into the projects
  they chose (FEATURE-CONNECTOR-FRAMEWORK.md §6b,
  test_bobs_linked_account_never_appears_for_alice). Both count as
  "satisfied" here — the declared need is "does project X have *a* way to
  read mail", not "does every team member have one".
"""

from __future__ import annotations

from dataclasses import dataclass

from .acl import Acl
from .connectors.registry import DEFAULT_TYPES, RESOURCE_KEYS

# Capabilities this view understands. Mirrors the connector registry's
# capability vocabulary (DEFAULT_TYPES ∪ RESOURCE_KEYS) rather than
# hardcoding a second list that could drift from it.
KNOWN_CAPABILITIES: frozenset[str] = frozenset(set(DEFAULT_TYPES) | set(RESOURCE_KEYS))

# Fields that must be non-empty on a SHARED resource (acl.yaml's
# mailbox/calendar/... dict) for it to count as configured, rather than a
# placeholder stub a wizard wrote with intent but no secret yet.
# `password_ref` is the actual credential pointer both tools/email.py's
# `_make_mailbox` and tools/calendar.py's `_RealDavAdapter` read — host/user
# alone (exactly what "I know who but not the password yet" looks like) must
# not count as configured. We only check the key is PRESENT, never resolve
# it (resolving a `*_ref` belongs to the adapter at request time, not to a
# read-only status view).
_REQUIRED_SHARED_FIELDS: dict[str, tuple[str, ...]] = {
    "mail": ("host", "user", "password_ref"),
    "calendar": ("url", "password_ref"),
    "files": ("url", "password_ref"),
}


@dataclass(frozen=True)
class StatusEntry:
    project: str
    capability: str | None
    status: str  # "satisfied" | "waiting" | "error"
    detail: str

    def line(self) -> str:
        cap = self.capability if self.capability is not None else "?"
        return f"{self.project}: {cap} — {self.detail}"


def _shared_satisfied(acl: Acl, project: str, capability: str) -> bool:
    resource_cfg = acl.resource(project, RESOURCE_KEYS.get(capability, capability))
    if not resource_cfg:
        return False
    required = _REQUIRED_SHARED_FIELDS.get(capability)
    if required is None:
        return True
    return all(resource_cfg.get(field) for field in required)


def _per_user_satisfied(acl: Acl, token_store, project: str, capability: str) -> str | None:
    """A "kopplad för <user> (<provider>/<account>)" detail string, or None.

    Walks every user's scope rows rather than asking the token store for a
    cross-user query (there isn't one, by design — TokenStore.list_scopes is
    always scoped to one `memaix_user`, matching the per-user isolation
    FEATURE-CONNECTOR-FRAMEWORK.md §8 requires everywhere else). Skips
    disabled users (enforce()'s kill-switch applies here too — a disabled
    user's link does not count as the project having access) and any token
    already flagged `needs_relink` (known-dead, not merely unverified; see
    ConnectorRegistry._scoped_accounts for the same rule on the read path).

    `project in acl.visible_projects(user_id)` gates BOTH an explicit
    project name and a '*' scope, not just '*' — a scope row naming this
    exact project can still be stale if the user's grant on it was later
    revoked (`project_member_set(role=None)`/acl.yaml edit). `enforce()`
    would refuse that call at request time regardless of the scope row, so
    reporting "kopplad" from the row alone would be fail-open; this view
    must agree with what a real request would actually get.
    """
    for user_id in sorted(acl.users):
        if acl.is_disabled(user_id):
            continue
        if project not in acl.visible_projects(user_id):
            continue
        accounts = {(a["provider"], a["account"]): a for a in token_store.list_accounts(user_id)}
        for scope in token_store.list_scopes(user_id):
            if scope["capability"] != capability:
                continue
            projects = scope["projects"]
            if project not in projects and "*" not in projects:
                continue
            account = accounts.get((scope["provider"], scope["account"]))
            if account is None or account.get("status") == "needs_relink":
                continue
            return f"kopplad för {user_id} ({scope['provider']}/{scope['account']})"
    return None


def _status_for(acl: Acl, token_store, project: str, capability: str) -> StatusEntry:
    if capability not in KNOWN_CAPABILITIES:
        return StatusEntry(project, capability, "error", f"okänd kapabilitet: {capability!r}")
    if _shared_satisfied(acl, project, capability):
        return StatusEntry(project, capability, "satisfied", "delad projektresurs konfigurerad")
    per_user_detail = _per_user_satisfied(acl, token_store, project, capability)
    if per_user_detail is not None:
        return StatusEntry(project, capability, "satisfied", per_user_detail)
    return StatusEntry(project, capability, "waiting", "väntar på koppling")


def collect_status(acl: Acl, token_store) -> list[StatusEntry]:
    """One StatusEntry per (project, capability) the project has declared
    `awaiting_link` for in acl.yaml. Projects without the key are skipped
    entirely — that key's absence means "nothing declared pending", not
    "everything is fine"; this view only ever reports on what was declared.

    `awaiting_link` must be a list of capability-name strings (e.g.
    `["mail"]`). A malformed value or an unknown capability name is
    reported as an "error" entry rather than raised — this is read-only
    informational tooling, so one admin's typo must never be able to stop
    the gateway booting (that constraint belongs to `Acl.from_config`,
    which this module does not touch) or crash this command outright.
    """
    entries: list[StatusEntry] = []
    for project in sorted(acl.projects):
        awaiting = acl.resource(project, "awaiting_link")
        if awaiting is None:
            continue
        if not isinstance(awaiting, list) or not all(isinstance(c, str) for c in awaiting):
            entries.append(
                StatusEntry(
                    project, None, "error",
                    f"awaiting_link måste vara en lista av strängar, fick: {awaiting!r}",
                )
            )
            continue
        for capability in awaiting:
            entries.append(_status_for(acl, token_store, project, capability))
    return entries
