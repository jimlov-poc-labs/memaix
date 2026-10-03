# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP-level behaviour: access changes are queued, run only after approval."""

from __future__ import annotations

import pytest
import yaml

from memaix_gateway import server
from memaix_gateway.access_admin import AccessAdmin
from memaix_gateway.acl import AccessDenied, Acl
from memaix_gateway.invites import InviteStore
from memaix_gateway.outbox.execute import execute_pending
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.tools import access as t
from memaix_gateway.web.acl_writer import AclWriter


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    path = tmp_path / "acl.yaml"
    path.write_text(yaml.safe_dump({
        "users": {
            "root": {"admin": True, "grants": {"acme": "owner"}},
            "alice": {"grants": {"acme": "owner"}},
            "bob": {"grants": {"acme": "reader"}},
        },
        "projects": {"acme": {"vault": str(tmp_path / "v"), "allow_send": False}},
    }))
    admin = AccessAdmin(AclWriter(path), InviteStore(tmp_path / "i.db"), tmp_path)
    monkeypatch.setattr(t, "_admin", lambda: admin)
    monkeypatch.setattr(t, "_fresh_acl", lambda: Acl.from_config(yaml.safe_load(path.read_text())))
    monkeypatch.setattr(server, "reload_acl", lambda: None)
    monkeypatch.setattr(t.config, "load", lambda: {"acl": yaml.safe_load(path.read_text()), "memaix": {"server": {"public_url": "https://m.example"}}})
    q = ActionQueue(tmp_path / "o.db")
    return admin, q, path, lambda: t._fresh_acl()


def test_invite_is_queued_not_executed(rig):
    admin, q, path, acl = rig
    out = t.user_invite(acl(), "alice", "acme", "newbie", "reader", _outbox=q)
    assert out["pending"] is True
    assert "newbie" not in yaml.safe_load(path.read_text())["users"]


def test_non_owner_cannot_even_queue(rig):
    admin, q, path, acl = rig
    with pytest.raises(AccessDenied):
        t.user_invite(acl(), "bob", "acme", "newbie", "reader", _outbox=q)
    with pytest.raises(AccessDenied):
        t.project_member_set(acl(), "bob", "acme", "bob", "owner", _outbox=q)


def test_approval_runs_it_and_returns_invite_url(rig):
    admin, q, path, acl = rig
    aid = t.user_invite(acl(), "alice", "acme", "newbie", "reader", _outbox=q)["action_id"]
    action = q.get(aid)
    assert action["tool"] == "user_invite"
    result = execute_pending(acl(), action)
    assert result["status"] == "invited"
    assert result["invite_url"].startswith("https://m.example/app/invite/")
    assert "token" not in result
    assert yaml.safe_load(path.read_text())["users"]["newbie"]["grants"] == {"acme": "reader"}


def test_member_change_queued_then_applied(rig):
    admin, q, path, acl = rig
    aid = t.project_member_set(acl(), "alice", "acme", "bob", "collaborator", _outbox=q)["action_id"]
    assert yaml.safe_load(path.read_text())["users"]["bob"]["grants"]["acme"] == "reader"
    assert "error" not in execute_pending(acl(), q.get(aid))
    assert yaml.safe_load(path.read_text())["users"]["bob"]["grants"]["acme"] == "collaborator"


def test_approval_rechecks_permission_at_execution(rig):
    admin, q, path, acl = rig
    aid = t.project_member_set(acl(), "alice", "acme", "bob", "owner", _outbox=q)["action_id"]
    data = yaml.safe_load(path.read_text())
    data["users"]["alice"]["grants"]["acme"] = "reader"  # demoted before approval
    path.write_text(yaml.safe_dump(data))
    assert "error" in execute_pending(acl(), q.get(aid))
    assert yaml.safe_load(path.read_text())["users"]["bob"]["grants"]["acme"] == "reader"


def test_project_create_is_admin_only(rig):
    admin, q, path, acl = rig
    with pytest.raises(AccessDenied):
        t.project_create(acl(), "alice", "newproj")
    assert t.project_create(acl(), "root", "newproj")["project"] == "newproj"
