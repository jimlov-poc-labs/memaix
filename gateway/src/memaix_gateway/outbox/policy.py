# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decide whether an outgoing action executes immediately or needs approval.

See docs/FEATURE-APPROVAL-OUTBOX.md §5. Most restrictive source wins: an
unlisted recipient forces 'review' even when the project's mode is 'auto'.
"""

from __future__ import annotations


def _recipients(tool: str, args: dict) -> list[str]:
    """Extract the addresses/handles an outgoing action would reach."""
    if tool == "email_send":
        out: list[str] = []
        for field in ("to", "cc"):
            value = args.get(field)
            if value:
                out.extend(a.strip() for a in value.split(",") if a.strip())
        return out
    if tool in ("calendar_create", "calendar_update"):
        return list(args.get("attendees") or [])
    return []


def _allowlisted(recipient: str, allowlist: list[str]) -> bool:
    """Exact address match, or '@domain.example' suffix match."""
    r = recipient.strip().lower()
    for entry in allowlist:
        e = (entry or "").strip().lower()
        if not e:
            continue
        if e.startswith("@"):
            if r.endswith(e):
                return True
        elif r == e:
            return True
    return False


# Approving/rejecting a queued action requires the same role the underlying
# tool itself enforces — a reader must never be able to approve an email_send.
# Single source of truth for the MCP tools (server.py), the board API and the
# web-UI API; do not fork this table.
APPROVAL_ROLE: dict[str, str] = {
    "email_send": "owner",
    "calendar_create": "collaborator",
    "calendar_update": "collaborator",
    "project_member_set": "owner",
    "user_invite": "owner",
    "user_reset_link": "owner",
}


def approval_role(tool: str | None) -> str:
    return APPROVAL_ROLE.get(tool or "", "owner")


def can_approve(acl, user: str, action: dict) -> bool:
    """True if the user holds the role required to approve this action. A queued
    outgoing action's args include the full email body/recipients, so only
    someone who could actually send it should see it — a reader must not read
    another user's pending email content (docs/THREAT-MODEL.md)."""
    from ..acl import AccessDenied

    try:
        acl.enforce(user, action.get("project") or "", approval_role(action.get("tool")))
        return True
    except AccessDenied:
        return False


def action_mode(cfg: dict | None, acl, project: str, tool: str, args: dict) -> str:
    """Return 'auto' or 'review' for this action on this project.

    project's acl.resource(project, 'outbox') wins over the global
    memaix.outbox.default_mode (which defaults to 'auto' — unset config is a
    no-op, matching pre-outbox behaviour). An 'allowlist' resource, if
    present, forces 'review' for any recipient not on it, regardless of mode.
    """
    project_mode = acl.resource(project, "outbox")
    default_mode = ((cfg or {}).get("memaix", {}) or {}).get("outbox", {}).get(
        "default_mode", "auto"
    )
    mode = project_mode if project_mode in ("auto", "review") else default_mode

    allowlist = acl.resource(project, "allowlist")
    if allowlist:
        for recipient in _recipients(tool, args):
            if not _allowlisted(recipient, allowlist):
                return "review"

    return mode
