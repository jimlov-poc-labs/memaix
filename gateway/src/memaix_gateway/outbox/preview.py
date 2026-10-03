# SPDX-License-Identifier: AGPL-3.0-or-later
"""Human-readable previews for queued outbox actions."""

from __future__ import annotations

_MAX_BODY_CHARS = 400


def _truncate(text: str, limit: int = _MAX_BODY_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def render_preview(tool: str, args: dict) -> str:
    """Return a short human-readable summary of a queued action's args."""
    if tool == "email_send":
        to = args.get("to", "")
        cc = args.get("cc") or ""
        subject = args.get("subject", "")
        body = _truncate(args.get("body", ""))
        lines = [f"Till: {to}"]
        if cc:
            lines.append(f"Kopia: {cc}")
        lines.append(f"Ämne: {subject}")
        lines.append("")
        lines.append(body)
        return "\n".join(lines)

    if tool in ("calendar_create", "calendar_update"):
        title = args.get("title", "(oförändrad titel)")
        start = args.get("start", "")
        end = args.get("end", "")
        location = args.get("location") or ""
        attendees = args.get("attendees") or []
        lines = [f"Händelse: {title}"]
        if start or end:
            lines.append(f"Tid: {start} – {end}")
        if location:
            lines.append(f"Plats: {location}")
        if attendees:
            lines.append(f"Deltagare: {', '.join(attendees)}")
        return "\n".join(lines)

    if tool == "user_invite":
        lines = [f"Bjud in: {args.get('invitee', '')} som {args.get('role', '')}"]
        if args.get("email"):
            lines.append(f"E-post: {args['email']}")
        return "\n".join(lines)

    if tool == "project_member_set":
        role = args.get("role")
        change = f"sätt roll {role}" if role else "ta bort från projektet"
        return f"Medlem: {args.get('member', '')}\nÄndring: {change}"

    # Generic fallback for future action types.
    return f"{tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
