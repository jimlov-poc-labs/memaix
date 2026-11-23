# SPDX-License-Identifier: AGPL-3.0-or-later
"""H5: a model-driven MCP caller must not be able to queue a mail and then
approve (= send) it itself. Outgoing mail is approved by a person only: the web
API with a session cookie (or elicitation). outbox_approve over MCP refuses
mail tools fail closed, whatever the unlock settings.
"""

from __future__ import annotations

import asyncio

import pytest

from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.outbox import queue as outbox_queue
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.tools import email as t_email


def _project(tmp_path, **extra):
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True, exist_ok=True)
    proj = {
        "vault": str(vault),
        "allow_send": True,
        "outbox": "review",
        "mailbox": {"host": "imap.invalid", "user": "box@example.test", "password_ref": "env:X"},
        "smtp": {"from_addr": "box@example.test"},
    }
    proj.update(extra)
    return Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": proj})


@pytest.fixture
def env(tmp_path, monkeypatch):
    AuditLog._clear_instances()
    monkeypatch.setattr(server, "_audit", AuditLog.for_path(tmp_path / "audit.db"))
    monkeypatch.setenv("MEMAIX_OUTBOX_DB", str(tmp_path / "outbox.db"))
    outbox_queue._reset_default_queue()
    monkeypatch.setattr(server, "_outbox_queue", ActionQueue.for_path(tmp_path / "outbox.db"))
    monkeypatch.setenv("MEMAIX_USER", "alice")
    server._rate_limiter._windows.clear()
    sent: list = []
    monkeypatch.setattr(t_email, "_smtp_deliver", lambda msg, *a, **k: sent.append(msg))
    return sent


def _cfg(monkeypatch, unlock_enabled: bool):
    cfg = {"memaix": {"mail_send_unlock": {"enabled": unlock_enabled}}}
    monkeypatch.setattr(server.config, "load", lambda: cfg)
    monkeypatch.setattr(t_email.config, "load", lambda: cfg)


def test_default_config_model_cannot_queue_then_self_approve(env, tmp_path, monkeypatch):
    """Standard config (unlock not enabled): queue via email_send, approve via
    outbox_approve in the same MCP session, no unlock. Must NOT send."""
    sent = env
    monkeypatch.setattr(server, "_acl", _project(tmp_path))
    _cfg(monkeypatch, False)

    out = asyncio.run(server.email_send("proj", "victim@example.test", "hej", "hemligt", ctx=None))
    assert out.get("pending") is True, out

    with pytest.raises(PermissionError, match="approval_web_only"):
        server.outbox_approve(out["action_id"], ctx=None)
    assert sent == [], "mejlet gick ut: modellen godkände sin egen köade åtgärd"
    assert server._get_outbox().get(out["action_id"])["status"] == "pending"


def test_project_send_unlock_not_honoured_for_preexisting_queued_mail(env, tmp_path, monkeypatch):
    """Project has send_unlock: true (global flag off). A queued mail (e.g. from
    a rule or from before the flag was set) is approved via MCP with no unlock.
    Must NOT send: outbox_approve only looks at the global flag."""
    sent = env
    acl = _project(tmp_path, send_unlock=True)
    monkeypatch.setattr(server, "_acl", acl)
    _cfg(monkeypatch, False)

    args = {"to": "victim@example.test", "subject": "s", "body": "b", "cc": None}
    action_id = server._get_outbox().enqueue("alice", "proj", "email_send", args, "preview")

    with pytest.raises(PermissionError, match="approval_web_only"):
        server.outbox_approve(action_id, ctx=None)
    assert sent == [], "projektets send_unlock ignorerades av outbox_approve"
    assert server._get_outbox().get(action_id)["status"] == "pending"


def test_global_unlock_enabled_blocks_self_approve(env, tmp_path, monkeypatch):
    """Control: with the global flag on, MCP approval is refused too."""
    sent = env
    monkeypatch.setattr(server, "_acl", _project(tmp_path))
    _cfg(monkeypatch, True)
    action_id = server._get_outbox().enqueue(
        "alice", "proj", "email_send",
        {"to": "victim@example.test", "subject": "s", "body": "b", "cc": None}, "preview",
    )
    with pytest.raises(PermissionError, match="approval_web_only"):
        server.outbox_approve(action_id, ctx=None)
    assert sent == []
    assert server._get_outbox().get(action_id)["status"] == "pending"


def test_non_mail_actions_still_approvable_over_mcp(env, tmp_path, monkeypatch):
    """Scope: only mail tools are refused; other outbox actions are unchanged."""
    monkeypatch.setattr(server, "_acl", _project(tmp_path))
    _cfg(monkeypatch, False)
    called = []
    monkeypatch.setattr(
        "memaix_gateway.outbox.execute.execute_pending", lambda acl, a: called.append(a["tool"]) or {}
    )
    aid = server._get_outbox().enqueue("alice", "proj", "calendar_create", {"title": "x"}, "preview")
    assert server.outbox_approve(aid, ctx=None)["ok"] is True
    assert called == ["calendar_create"]


def test_web_approval_with_session_still_sends(env, tmp_path, monkeypatch):
    """The human path: web API with a session user approves and the mail goes out."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from memaix_gateway.web import routes as web_routes_mod
    from memaix_gateway.web.api import outbox as api_outbox_mod

    sent = env
    acl = _project(tmp_path, send_unlock=True)  # even with send_unlock: a person approves
    _cfg(monkeypatch, True)
    queue = server._get_outbox()
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: "alice")
    monkeypatch.setattr(api_outbox_mod, "_queue", lambda: queue)
    monkeypatch.setattr(api_outbox_mod, "_audit", lambda: server._get_audit())
    aid = queue.enqueue(
        "alice", "proj", "email_send",
        {"to": "x@example.test", "subject": "s", "body": "b", "cc": None}, "preview",
    )
    client = TestClient(Starlette(routes=web_routes_mod.web_routes))
    resp = client.post(f"/app/api/outbox/{aid}/approve")
    assert resp.status_code == 200 and resp.json()["ok"] is True, resp.text
    assert len(sent) == 1
