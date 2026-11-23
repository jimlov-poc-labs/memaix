# SPDX-License-Identifier: AGPL-3.0-or-later
"""H5: a model-driven MCP caller must not be able to queue a mail and then
approve (= send) it itself without a send unlock.

Desired behaviour (fail closed): outbox_approve of a queued mail tool needs an
unlocked session whenever the mail would need one at queue time — the global
memaix.mail_send_unlock.enabled flag OR the project's send_unlock resource.
These tests describe that; they fail on code where the gate only looks at the
global flag.
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

    try:
        server.outbox_approve(out["action_id"], ctx=None)
    except Exception:  # a refusal is the desired outcome
        pass
    assert sent == [], "mejlet gick ut utan upplåsning: modellen godkände sin egen köade åtgärd"


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

    try:
        server.outbox_approve(action_id, ctx=None)
    except Exception:
        pass
    assert sent == [], "projektets send_unlock ignorerades av outbox_approve"


def test_control_global_unlock_enabled_blocks_self_approve(env, tmp_path, monkeypatch):
    """Control: with the global flag on, the existing gate does refuse."""
    sent = env
    monkeypatch.setattr(server, "_acl", _project(tmp_path))
    _cfg(monkeypatch, True)
    action_id = server._get_outbox().enqueue(
        "alice", "proj", "email_send",
        {"to": "victim@example.test", "subject": "s", "body": "b", "cc": None}, "preview",
    )
    with pytest.raises(Exception, match="send_locked"):
        server.outbox_approve(action_id, ctx=None)
    assert sent == []
    assert server._get_outbox().get(action_id)["status"] == "pending"
