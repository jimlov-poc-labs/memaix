# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the /hooks/{token} inbound webhook trigger (FEATURE-AUTOMATION-RULES.md §6)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.rules.store import RulesStore


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    from memaix_gateway import server as server_mod

    acl = Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": str(tmp_path / "vault")}})
    rules = RulesStore.for_path(tmp_path / "rules.db")
    monkeypatch.setattr(server_mod, "_acl", acl)
    monkeypatch.setattr(server_mod, "_rules_store", rules)

    app = server_mod.build_http_app()
    client = TestClient(app)
    return client, rules


def test_webhook_matching_token_triggers_rule(rig):
    client, rules = rig
    sent = []
    import memaix_gateway.rules.actions as actions_mod
    orig = actions_mod._run_notify
    actions_mod._run_notify = lambda acl, user, params, *, tools, **_: (sent.append(params) or {"ok": True, "errors": [], "channels_used": 0})
    try:
        rules.add_rule(
            "alice", "proj", "form-submit", {"type": "webhook", "token": "secret123"},
            [{"type": "notify", "params": {"text_from": "message"}}],
        )
        resp = client.post("/hooks/secret123", json={"message": "New form submission"})
        assert resp.status_code == 200
        assert resp.json()["matched"] == 1
        assert sent == [{"text": "New form submission"}]
    finally:
        actions_mod._run_notify = orig


def test_webhook_wrong_token_is_404(rig):
    client, rules = rig
    rules.add_rule(
        "alice", "proj", "r", {"type": "webhook", "token": "secret123"},
        [{"type": "notify", "params": {"text": "hi"}}],
    )
    resp = client.post("/hooks/wrong-token", json={})
    assert resp.status_code == 404


def test_webhook_retried_payload_is_idempotent(rig):
    client, rules = rig
    sent = []
    import memaix_gateway.rules.actions as actions_mod
    orig = actions_mod._run_notify
    actions_mod._run_notify = lambda acl, user, params, *, tools, **_: (sent.append(1) or {"ok": True, "errors": [], "channels_used": 0})
    try:
        rules.add_rule(
            "alice", "proj", "r", {"type": "webhook", "token": "tok"},
            [{"type": "notify", "params": {"text": "hi"}}],
        )
        body = {"message": "same payload"}
        first = client.post("/hooks/tok", json=body)
        second = client.post("/hooks/tok", json=body)  # e.g. a retried delivery
        assert first.json()["matched"] == 1
        assert second.status_code == 404  # already handled — no rule "matched" again
        assert sent == [1]
    finally:
        actions_mod._run_notify = orig
