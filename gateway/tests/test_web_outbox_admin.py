# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Fas C web APIs: approver-scoped outbox + admin read views
(FEATURE-WEB-UI-OUTBOX-AND-ADMIN.md).

The load-bearing property: a reader must not see queued action content —
not in the list, not via get. Visibility == approval right."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.web import routes as web_routes_mod
from memaix_gateway.web.api import admin as api_admin_mod
from memaix_gateway.web.api import outbox as api_outbox_mod


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    acl = Acl(
        users={
            "alice": {"grants": {"proj": "owner"}},
            "carol": {"grants": {"proj": "collaborator"}},
            "bob": {"grants": {"proj": "reader"}},
            "root": {"admin": True},
        },
        projects={"proj": {"vault": str(tmp_path / "v"), "outbox": "review", "allow_send": True}},
    )
    queue = ActionQueue.for_path(tmp_path / "outbox.db")
    AuditLog._clear_instances()
    audit = AuditLog.for_path(tmp_path / "audit.db")

    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    current = {"user": "alice"}
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: current["user"])
    monkeypatch.setattr(api_outbox_mod, "_queue", lambda: queue)
    monkeypatch.setattr(api_outbox_mod, "_audit", lambda: audit)
    monkeypatch.setattr(api_admin_mod, "_audit_log", lambda: audit)

    app = Starlette(routes=web_routes_mod.web_routes)
    client = TestClient(app)
    yield client, queue, audit, current
    AuditLog._clear_instances()


# ------------------------------------------------------------------
# Outbox — approver scoping
# ------------------------------------------------------------------


def test_owner_sees_pending_email(rig):
    client, queue, _, _ = rig
    queue.enqueue("alice", "proj", "email_send", {"to": "x@y.com", "body": "secret"}, "preview")
    actions = client.get("/app/api/outbox").json()
    assert len(actions) == 1
    assert actions[0]["tool"] == "email_send"


def test_reader_sees_nothing_and_gets_403_on_get(rig):
    client, queue, _, current = rig
    aid = queue.enqueue("alice", "proj", "email_send", {"to": "x@y.com", "body": "secret"}, "p")
    current["user"] = "bob"  # reader — must not see queued email content
    assert client.get("/app/api/outbox").json() == []
    assert client.get(f"/app/api/outbox/{aid}").status_code == 403
    # …and cannot decide either.
    assert client.post(f"/app/api/outbox/{aid}/approve").status_code == 403
    assert client.post(f"/app/api/outbox/{aid}/reject", json={"reason": "x"}).status_code == 403


def test_collaborator_sees_calendar_but_not_email(rig):
    client, queue, _, current = rig
    queue.enqueue("alice", "proj", "email_send", {"to": "x@y.com"}, "p")
    queue.enqueue("alice", "proj", "calendar_create", {"title": "mtg"}, "p")
    current["user"] = "carol"  # collaborator: calendar yes, email (owner) no
    actions = client.get("/app/api/outbox").json()
    assert [a["tool"] for a in actions] == ["calendar_create"]


def test_approve_executes_once_and_409_on_race(rig, monkeypatch):
    client, queue, audit, _ = rig
    aid = queue.enqueue(
        "alice", "proj", "email_send",
        {"to": "x@y.com", "subject": "s", "body": "b", "cc": None}, "p",
    )
    calls = []
    monkeypatch.setattr(
        "memaix_gateway.outbox.execute._default_dispatch",
        lambda: {"email_send": lambda acl, u, p, **kw: calls.append(kw) or {"status": "sent"}},
    )
    resp = client.post(f"/app/api/outbox/{aid}/approve")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(calls) == 1
    assert calls[0]["_confirmed"] is True

    # Second decision → 409, no second execution.
    resp = client.post(f"/app/api/outbox/{aid}/approve")
    assert resp.status_code == 409
    assert len(calls) == 1

    # Audit trail recorded the execution.
    events = audit.query(tool="outbox_execute:email_send")
    assert len(events) == 1 and events[0]["ok"] is True


def test_approve_returns_invite_link_once_and_does_not_store_it(rig, monkeypatch):
    client, queue, _, _ = rig
    aid = queue.enqueue("alice", "proj", "user_invite", {"invitee": "n", "role": "reader", "email": None}, "p")
    url = "https://mcp.example/app/invite/SECRET"
    monkeypatch.setattr(
        "memaix_gateway.outbox.execute._default_dispatch",
        lambda: {"user_invite": lambda acl, u, p, **kw: {"user": "n", "invite_url": url}},
    )
    body = client.post(f"/app/api/outbox/{aid}/approve").json()
    assert body["result"]["invite_url"] == url

    stored = queue.get(aid)
    assert stored["status"] == "executed"
    assert "invite_url" not in stored["result"]
    assert stored["result"]["link_shown_once"] is True
    assert stored["result"]["user"] == "n"
    assert "SECRET" not in client.get(f"/app/api/outbox/{aid}").text


def test_reject_records_reason_and_never_executes(rig, monkeypatch):
    client, queue, audit, _ = rig
    aid = queue.enqueue("alice", "proj", "email_send", {"to": "x@y.com"}, "p")
    calls = []
    monkeypatch.setattr(
        "memaix_gateway.outbox.execute._default_dispatch",
        lambda: {"email_send": lambda acl, u, p, **kw: calls.append(kw)},
    )
    resp = client.post(f"/app/api/outbox/{aid}/reject", json={"reason": "fel mottagare"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert calls == []
    events = audit.query(tool="outbox_reject:email_send")
    assert events[0]["detail"] == "fel mottagare"


def test_outbox_get_404(rig):
    client, _, _, _ = rig
    assert client.get("/app/api/outbox/nope").status_code == 404


# ------------------------------------------------------------------
# Admin read views
# ------------------------------------------------------------------


def test_admin_endpoints_403_for_non_admin(rig):
    client, _, _, current = rig
    current["user"] = "alice"  # owner but NOT admin
    for path in ("/app/api/admin/users", "/app/api/admin/projects",
                 "/app/api/admin/audit", "/app/api/admin/system"):
        assert client.get(path).status_code == 403, path


def test_admin_users_and_projects_shape(rig):
    client, _, _, current = rig
    current["user"] = "root"
    users = client.get("/app/api/admin/users").json()
    ids = {u["id"] for u in users}
    assert ids == {"alice", "carol", "bob", "root"}
    root = next(u for u in users if u["id"] == "root")
    assert root["admin"] is True and root["disabled"] is False

    projects = client.get("/app/api/admin/projects").json()
    assert projects == [
        {"name": "proj", "allow_send": True, "outbox": "review", "users": 3,
         "vault": projects[0]["vault"]},
    ]


def test_admin_audit_filters_and_has_more(rig):
    client, _, audit, current = rig
    current["user"] = "root"
    for i in range(3):
        audit.log("alice", "proj", "email_send", True, detail=f"e{i}")
    audit.log("bob", "proj", "files_read", False, detail="denied")

    data = client.get("/app/api/admin/audit?user=alice").json()
    assert len(data["entries"]) == 3
    assert all(e["user"] == "alice" for e in data["entries"])

    data = client.get("/app/api/admin/audit?ok=false").json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["tool"] == "files_read"

    page = client.get("/app/api/admin/audit?limit=2").json()
    assert len(page["entries"]) == 2 and page["has_more"] is True
    page2 = client.get("/app/api/admin/audit?limit=2&offset=2").json()
    assert len(page2["entries"]) == 2 and page2["has_more"] is False


def test_admin_401_when_unauthenticated(rig, monkeypatch):
    client, _, _, _ = rig
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    assert client.get("/app/api/admin/users").status_code == 401


# ------------------------------------------------------------------
# Board API leak regression (the pre-existing /board/api/outbox list)
# ------------------------------------------------------------------


def test_board_outbox_list_is_approver_scoped(rig, tmp_path, monkeypatch):
    from memaix_gateway.board import routes as board_routes_mod

    _, queue, _, _ = rig
    acl = web_routes_mod._get_acl()
    monkeypatch.setattr(board_routes_mod, "_acl", lambda: acl)
    monkeypatch.setattr(board_routes_mod, "_outbox", lambda: queue)
    current = {"user": "bob"}  # reader
    monkeypatch.setattr(board_routes_mod, "_require_user", lambda request: current["user"])

    app = Starlette(routes=[r for r in board_routes_mod.board_routes if "outbox" in r.path])
    board_client = TestClient(app)

    queue.enqueue("alice", "proj", "email_send", {"to": "x@y.com", "body": "secret"}, "p")
    # Reader must NOT see the queued email through the board API either.
    assert board_client.get("/board/api/outbox?project=proj").json()["actions"] == []
    current["user"] = "alice"
    assert len(board_client.get("/board/api/outbox?project=proj").json()["actions"]) == 1
