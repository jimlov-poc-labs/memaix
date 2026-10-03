# SPDX-License-Identifier: AGPL-3.0-or-later
"""Human-readable previews for queued outbox actions."""

from __future__ import annotations

_MAX_BODY_CHARS = 400


def _truncate(text: str, limit: int = _MAX_BODY_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _email_send(args: dict) -> str:
    lines = [f"Till: {args.get('to', '')}"]
    if args.get("cc"):
        lines.append(f"Kopia: {args['cc']}")
    lines += [f"Ämne: {args.get('subject', '')}", "", _truncate(args.get("body", ""))]
    return "\n".join(lines)


def _calendar(args: dict) -> str:
    start, end = args.get("start", ""), args.get("end", "")
    lines = [f"Händelse: {args.get('title', '(oförändrad titel)')}"]
    if start or end:
        lines.append(f"Tid: {start} – {end}")
    if args.get("location"):
        lines.append(f"Plats: {args['location']}")
    if args.get("attendees"):
        lines.append(f"Deltagare: {', '.join(args['attendees'])}")
    return "\n".join(lines)


def _user_invite(args: dict) -> str:
    lines = [f"Bjud in: {args.get('invitee', '')} som {args.get('role', '')}"]
    if args.get("email"):
        lines.append(f"E-post: {args['email']}")
    return "\n".join(lines)


def _member_set(args: dict) -> str:
    role = args.get("role")
    change = f"sätt roll {role}" if role else "ta bort från projektet"
    return f"Medlem: {args.get('member', '')}\nÄndring: {change}"


def _reset_link(args: dict) -> str:
    return f"Skapa återställningslänk för lösenord: {args.get('member', '')}"


_RENDERERS = {
    "email_send": _email_send,
    "calendar_create": _calendar,
    "calendar_update": _calendar,
    "user_invite": _user_invite,
    "project_member_set": _member_set,
    "user_reset_link": _reset_link,
}


def render_preview(tool: str, args: dict) -> str:
    """Return a short human-readable summary of a queued action's args."""
    renderer = _RENDERERS.get(tool)
    if renderer:
        return renderer(args)
    # Generic fallback for future action types.
    return f"{tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
