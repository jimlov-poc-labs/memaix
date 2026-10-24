# SPDX-License-Identifier: AGPL-3.0-or-later
"""Godkännande via elicitation: enhetstester på resolvern + hela vägen genom FastMCP."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.outbox.elicit import resolve_via_elicitation
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.safety.audit import AuditLog

PENDING = {"pending": True, "action_id": "a1", "note": "x"}
ACTION = {"id": "a1", "tool": "user_invite", "project": "proj", "preview": "Bjud in carol", "status": "pending"}


class FakeCtx:
    def __init__(self, action="accept", approve=True, supports=True, boom=False):
        self.calls = []
        self._answer = SimpleNamespace(action=action, data=SimpleNamespace(approve=approve) if action == "accept" else None)
        self._supports = supports
        self._boom = boom
        self.session = SimpleNamespace(check_client_capability=lambda caps: self._supports)

    async def elicit(self, message, schema):
        self.calls.append(message)
        if self._boom:
            raise RuntimeError("client hung up")
        return self._answer


def _run(ctx, result=PENDING, enabled=True, action=ACTION):
    log = []
    out = asyncio.run(resolve_via_elicitation(
        ctx, result, enabled=enabled,
        get_action=lambda aid: action,
        approve=lambda aid: log.append(("approve", aid)) or {"ok": True, "action_id": aid},
        reject=lambda aid, why: log.append(("reject", aid, why)) or {"ok": True, "status": "rejected"},
    ))
    return out, log


def test_accept_approves_and_marks_source():
    out, log = _run(FakeCtx())
    assert log == [("approve", "a1")] and out["approved_via"] == "elicitation"


def test_message_shows_tool_project_and_preview():
    ctx = FakeCtx()
    _run(ctx)
    assert "user_invite" in ctx.calls[0] and "proj" in ctx.calls[0] and "Bjud in carol" in ctx.calls[0]


@pytest.mark.parametrize("ctx", [FakeCtx(action="decline"), FakeCtx(action="accept", approve=False)])
def test_decline_or_unticked_rejects(ctx):
    out, log = _run(ctx)
    assert [e[0] for e in log] == ["reject"] and out["rejected_via"] == "elicitation"


def test_cancel_leaves_pending():
    out, log = _run(FakeCtx(action="cancel"))
    assert log == [] and out["pending"] is True and "avbröts" in out["note"]


@pytest.mark.parametrize("kwargs", [
    dict(ctx=FakeCtx(supports=False)),
    dict(ctx=None),
    dict(ctx=FakeCtx(), enabled=False),
    dict(ctx=FakeCtx(), result={"ok": True}),
    dict(ctx=FakeCtx(), result={"pending": True}),
    dict(ctx=FakeCtx(), action={**ACTION, "status": "approved"}),
    dict(ctx=FakeCtx(), action=None),
])
def test_no_dialog_means_untouched(kwargs):
    result = kwargs.get("result", PENDING)
    out, log = _run(**kwargs)
    assert log == [] and out == result


def test_dialog_error_never_fails_the_call():
    out, log = _run(FakeCtx(boom=True))
    assert log == [] and out == PENDING


# ---- hela vägen: riktig FastMCP-server + klient med elicitation_callback ----

@pytest.fixture
def wired(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True)
    acl = Acl(
        users={"alice": {"grants": {"proj": "owner"}}, "bob": {"grants": {"proj": "reader"}}},
        projects={"proj": {"vault": str(vault)}},
    )
    AuditLog._clear_instances()
    monkeypatch.setattr(server, "_acl", acl)
    monkeypatch.setattr(server, "_audit", AuditLog.for_path(tmp_path / "audit.db"))
    # Som i drift: verktygen köar via default_queue(), servern läser _get_outbox() — samma fil.
    from memaix_gateway.outbox import queue as outbox_queue

    monkeypatch.setenv("MEMAIX_OUTBOX_DB", str(tmp_path / "outbox.db"))
    outbox_queue._reset_default_queue()
    monkeypatch.setattr(server, "_outbox_queue", ActionQueue.for_path(tmp_path / "outbox.db"))
    monkeypatch.setenv("MEMAIX_USER", "alice")
    server._rate_limiter._windows.clear()
    return acl


def _flag(monkeypatch, on):
    monkeypatch.setattr(server.config, "load", lambda: {"memaix": {"outbox": {"elicit_approval": on}}})


async def _call(tool, args, callback):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=callback
    ) as client:
        res = await client.call_tool(tool, args)
    return json.loads(res.content[0].text)


def _answer(action, approve=True):
    from mcp.types import ElicitResult

    async def cb(context, params):
        cb.seen = params.message
        return ElicitResult(action=action, content={"approve": approve} if action == "accept" else None)

    return cb


def test_e2e_member_set_executes_after_accept(wired, monkeypatch):
    _flag(monkeypatch, True)
    called = []
    monkeypatch.setattr(
        "memaix_gateway.outbox.execute.execute_pending",
        lambda acl, action, **kw: called.append(action["tool"]) or {"done": True},
    )
    cb = _answer("accept")
    out = asyncio.run(_call("project_member_set", {"project": "proj", "member": "bob", "role": "collaborator"}, cb))
    assert out["approved_via"] == "elicitation" and out["ok"] is True
    assert called == ["project_member_set"]
    assert "project_member_set" in cb.seen
    assert server._get_outbox().get(out["action_id"])["status"] == "executed"


def test_e2e_decline_rejects_and_never_executes(wired, monkeypatch):
    _flag(monkeypatch, True)
    called = []
    monkeypatch.setattr("memaix_gateway.outbox.execute.execute_pending", lambda *a, **k: called.append(1) or {})
    out = asyncio.run(_call("user_reset_link", {"project": "proj", "member": "bob"}, _answer("decline")))
    assert out["rejected_via"] == "elicitation" and called == []


def test_e2e_flag_off_stays_pending_and_never_asks(wired, monkeypatch):
    _flag(monkeypatch, False)
    cb = _answer("accept")
    out = asyncio.run(_call("project_member_set", {"project": "proj", "member": "bob", "role": "reader"}, cb))
    assert out["pending"] is True and not hasattr(cb, "seen")
    assert server._get_outbox().get(out["action_id"])["status"] == "pending"


def test_e2e_client_without_elicitation_stays_pending(wired, monkeypatch):
    _flag(monkeypatch, True)
    out = asyncio.run(_call("project_member_set", {"project": "proj", "member": "bob", "role": "reader"}, None))
    assert out["pending"] is True
