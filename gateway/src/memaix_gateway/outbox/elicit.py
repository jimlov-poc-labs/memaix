# SPDX-License-Identifier: AGPL-3.0-or-later
"""Approve a queued action in the same call, via MCP elicitation.

A gated tool still queues its action in the outbox exactly as before. When the
operator has switched `memaix.outbox.elicit_approval` on and the connected
client declares elicitation support, the tool then asks the person at the
client to confirm right there and resolves the queued action through the
normal approve/reject path (same role check, same audit trail).

Anything else — flag off, client without elicitation, a dismissed dialog, a
client error — leaves the action pending in the outbox, untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ApprovalChoice(BaseModel):
    approve: bool = Field(description="Godkänn och utför åtgärden nu")


def client_supports_elicitation(ctx: Any) -> bool:
    try:
        from mcp.types import ClientCapabilities, ElicitationCapability

        return bool(
            ctx.session.check_client_capability(
                ClientCapabilities(elicitation=ElicitationCapability())
            )
        )
    except Exception:  # noqa: BLE001 - no usable session means no elicitation
        return False


async def resolve_via_elicitation(
    ctx: Any,
    result: Any,
    *,
    enabled: bool,
    get_action: Callable[[str], dict | None],
    approve: Callable[[str], dict],
    reject: Callable[[str, str], dict],
) -> Any:
    """Return `result` unchanged unless it is a queued action we can resolve now."""
    if not enabled or not isinstance(result, dict):
        return result
    action_id = result.get("action_id")
    if not (result.get("pending") and isinstance(action_id, str)):
        return result
    if ctx is None or not client_supports_elicitation(ctx):
        return result

    action = get_action(action_id)
    if not action or action.get("status") != "pending":
        return result

    message = (
        f"Godkänn {action['tool']} i projektet {action['project']}?\n\n{action['preview']}"
    )
    try:
        answer = await ctx.elicit(message=message, schema=ApprovalChoice)
    except Exception:  # noqa: BLE001 - a failing dialog must never fail the tool call
        logger.warning("elicitation misslyckades för %s, lämnar kvar i utkorgen", action_id, exc_info=True)
        return result

    if answer.action == "accept" and answer.data is not None and answer.data.approve:
        out = approve(action_id)
        return {**out, "approved_via": "elicitation"}
    if answer.action in ("accept", "decline"):
        out = reject(action_id, "avböjd i dialog")
        return {**out, "rejected_via": "elicitation"}
    return {**result, "note": "Dialogen avbröts — väntar fortfarande på godkännande i utkorgen"}
