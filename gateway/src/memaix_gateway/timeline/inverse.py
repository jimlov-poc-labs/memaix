# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inverse-operation builders — one entry per reversible MCP tool.

Each builder receives the exact (tail_args, kwargs, result) triple that
server._audited() sees for that tool call (tail_args = the positional
arguments after acl/user/project; see server.py's _maybe_record_timeline).
It returns an {"tool": ..., "args": {...}} inverse spec, or None if this
particular call can't be inverted (e.g. the underlying operation failed).

v1 covers actions whose inverse is fully determined by the call's own
result — no "capture the old value before mutating" plumbing is needed:
  memory_write/append -> memory_revert(commit)   (git already has the diff)
  calendar_create     -> calendar_delete(id)     (tools-layer only, not an
                                                    AI-callable MCP tool)
  backlog_add         -> backlog_set_status(rejected, expected_version=1)
                          (a freshly created item is always version 1)

Field-level undo for backlog_set_status/score/comment and calendar_update
needs the *previous* value, which isn't available from (args, result) alone
— building it would require fetching state before every mutation. Deferred
to a future iteration (docs/FEATURE-UNDO-TIMELINE.md "Framtida arbete");
those tools are simply absent from TOOL_HANDLERS below, so they show up in
the timeline (audit-visible) but without an undo button (reversible=False).
"""

from __future__ import annotations

from typing import Callable, Optional

InverseBuilder = Callable[[tuple, dict, dict], Optional[dict]]
SummaryBuilder = Callable[[tuple, dict, dict], str]


def _is_dict(x) -> bool:
    return isinstance(x, dict)


# ------------------------------------------------------------------
# memory_write / memory_append
# ------------------------------------------------------------------


def _memory_summary(verb: str):
    def _fn(tail: tuple, kwargs: dict, result: dict) -> str:
        note = tail[0] if tail else "?"
        return f"{verb} minnesnot {note}"
    return _fn


def _memory_inverse(tail: tuple, kwargs: dict, result: dict) -> dict | None:
    if not _is_dict(result):
        return None
    commit = result.get("commit")
    if not commit:
        return None
    return {"tool": "memory_revert", "args": {"commit": commit}}


# ------------------------------------------------------------------
# calendar_create
# ------------------------------------------------------------------


def _calendar_create_summary(tail: tuple, kwargs: dict, result: dict) -> str:
    title = tail[0] if tail else "?"
    return f'Skapade kalenderhändelse "{title}"'


def _calendar_create_inverse(tail: tuple, kwargs: dict, result: dict) -> dict | None:
    if not _is_dict(result):
        return None
    event_id = result.get("id")
    if not event_id:
        return None
    return {"tool": "calendar_delete", "args": {"id": event_id}}


# ------------------------------------------------------------------
# backlog_add
# ------------------------------------------------------------------


def _backlog_add_summary(tail: tuple, kwargs: dict, result: dict) -> str:
    title = tail[0] if tail else "?"
    item_id = result.get("id", "?") if _is_dict(result) else "?"
    return f'Skapade backlog-item "{title}" ({item_id})'


def _backlog_add_inverse(tail: tuple, kwargs: dict, result: dict) -> dict | None:
    if not _is_dict(result):
        return None
    item_id = result.get("id")
    if not item_id:
        return None
    # A freshly created backlog item is always version 1 (backlog.backlog_add
    # hardcodes it) — safe to target without a prior read.
    return {
        "tool": "backlog_set_status",
        "args": {"id": item_id, "status": "rejected", "expected_version": 1},
    }


# ------------------------------------------------------------------
# Outgoing mail — logged, never undoable (a sent mail can't be recalled)
# ------------------------------------------------------------------


def _mail_summary(verb: str):
    def _fn(tail: tuple, kwargs: dict, result: dict) -> str:
        res = result if _is_dict(result) else {}
        to = res.get("to") or kwargs.get("to") or (tail[0] if tail else "?")
        subject = res.get("subject") or (tail[1] if len(tail) > 1 else "")
        account = res.get("account")
        via = f" från {account}" if account else ""
        return f'{verb} "{subject}" till {to}{via}'
    return _fn


def _no_inverse(tail: tuple, kwargs: dict, result: dict) -> dict | None:
    return None


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

TOOL_HANDLERS: dict[str, tuple[SummaryBuilder, InverseBuilder]] = {
    "memory_write": (_memory_summary("Skrev"), _memory_inverse),
    "memory_append": (_memory_summary("La till i"), _memory_inverse),
    "calendar_create": (_calendar_create_summary, _calendar_create_inverse),
    "backlog_add": (_backlog_add_summary, _backlog_add_inverse),
    "email_send": (_mail_summary("Skickade"), _no_inverse),
    "email_forward": (_mail_summary("Vidarebefordrade"), _no_inverse),
    "email_reply": (_mail_summary("Svarade på"), _no_inverse),
}
