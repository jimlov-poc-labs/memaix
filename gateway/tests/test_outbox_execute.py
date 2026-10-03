# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for outbox.execute.execute_pending."""

from __future__ import annotations

from memaix_gateway.outbox.execute import execute_pending


def test_execute_pending_calls_tool_with_confirmed_true():
    calls = []

    def fake_email_send(acl, user, project, **kwargs):
        calls.append((acl, user, project, kwargs))
        return {"status": "sent"}

    action = {
        "memaix_user": "alice", "project": "proj", "tool": "email_send",
        "args": {"to": "x@y.com", "subject": "s", "body": "b", "cc": None},
    }
    result = execute_pending("acl-stub", action, tools={"email_send": fake_email_send})
    assert result == {"status": "sent"}
    acl, user, project, kwargs = calls[0]
    assert (acl, user, project) == ("acl-stub", "alice", "proj")
    assert kwargs["_confirmed"] is True
    assert kwargs["to"] == "x@y.com"


def test_execute_pending_catches_exception():
    def failing(acl, user, project, **kwargs):
        raise RuntimeError("smtp down")

    action = {"memaix_user": "alice", "project": "proj", "tool": "email_send", "args": {}}
    result = execute_pending("acl-stub", action, tools={"email_send": failing})
    assert result == {"error": "smtp down"}


def test_execute_pending_unknown_tool_returns_error():
    action = {"memaix_user": "alice", "project": "proj", "tool": "mystery_tool", "args": {}}
    result = execute_pending("acl-stub", action, tools={})
    assert "error" in result


def test_execute_pending_default_dispatch_covers_gated_tools():
    from memaix_gateway.outbox.execute import _default_dispatch

    dispatch = _default_dispatch()
    assert set(dispatch) == {
        "email_send", "calendar_create", "calendar_update",
        "project_member_set", "user_invite", "user_reset_link",
    }
