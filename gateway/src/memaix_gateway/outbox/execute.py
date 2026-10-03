# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dispatch a queued action to the real tool once it has been approved."""

from __future__ import annotations

from typing import Callable


def _default_dispatch() -> dict[str, Callable]:
    # Imported lazily to avoid a circular import: tools.email/tools.calendar
    # import outbox.policy/outbox.queue/outbox.preview at module scope, so
    # outbox.execute must only import them back inside a function.
    from ..tools import access as t_access
    from ..tools import calendar as t_calendar
    from ..tools import email as t_email

    return {
        "email_send": t_email.email_send,
        "calendar_create": t_calendar.calendar_create,
        "calendar_update": t_calendar.calendar_update,
        "project_member_set": t_access.project_member_set,
        "user_invite": t_access.user_invite,
        "user_reset_link": t_access.user_reset_link,
    }


def execute_pending(acl, action: dict, *, tools: dict[str, Callable] | None = None) -> dict:
    """Run the tool behind a queued action with `_confirmed=True`.

    Returns the tool's result on success. On failure, returns
    {"error": str(exc)} — the caller (outbox_approve in server.py) is
    responsible for persisting the resulting status via ActionQueue.record_result.
    """
    dispatch = tools if tools is not None else _default_dispatch()
    tool_name = action["tool"]
    fn = dispatch.get(tool_name)
    if fn is None:
        return {"error": f"no executor registered for tool: {tool_name!r}"}

    try:
        return fn(
            acl,
            action["memaix_user"],
            action["project"],
            **action["args"],
            _confirmed=True,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to caller as a result dict
        return {"error": str(exc)}
