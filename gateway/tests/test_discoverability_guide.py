# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Discoverability L1 (tour) / L2 (capabilities, memaix_help) /
L3 (nudges) — docs/FEATURE-DISCOVERABILITY.md §5-7."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.capabilities import catalog
from memaix_gateway.capabilities.nudges import NudgeState, suggest
from memaix_gateway.capabilities.registry import Capability, available_for
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.notify.store import NotifyStore
from memaix_gateway.rules.store import RulesStore
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.timeline.store import ActionsStore
from memaix_gateway.tools.onboarding import build_tour


@pytest.fixture(autouse=True)
def _real_catalog():
    """These tests read the module-level capability registry, so make sure
    it holds the real catalog regardless of what other test files left it
    as (register_defaults() is idempotent by key)."""
    catalog.register_defaults()
    yield


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True)
    shared_vault = tmp_path / "shared-vault"
    shared_vault.mkdir(parents=True)
    acl = Acl(
        users={
            "alice": {"grants": {"proj": "owner", "shared": "reader"}},
            "bob": {"grants": {"proj": "reader"}},
        },
        projects={"proj": {"vault": str(vault)}, "shared": {"vault": str(shared_vault)}},
    )
    AuditLog._clear_instances()
    monkeypatch.setattr(server, "_acl", acl)
    monkeypatch.setattr(server, "_audit", AuditLog.for_path(tmp_path / "audit.db"))
    monkeypatch.setattr(server, "_outbox_queue", ActionQueue.for_path(tmp_path / "outbox.db"))
    monkeypatch.setattr(server, "_timeline_store", ActionsStore.for_path(tmp_path / "actions.db"))
    monkeypatch.setattr(server, "_search_store", EmbeddingStore.for_path(tmp_path / "index.db"))
    monkeypatch.setattr(server, "_search_embedder", None)
    monkeypatch.setattr(server, "_search_embedder_loaded", True)
    monkeypatch.setattr(server, "_notify_store", NotifyStore.for_path(tmp_path / "notify.db"))
    monkeypatch.setattr(server, "_rules_store", RulesStore.for_path(tmp_path / "rules.db"))
    monkeypatch.setattr(server, "_token_store", TokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key()))
    monkeypatch.setattr(server, "_nudge_state", NudgeState.for_path(tmp_path / "nudges.db"))
    monkeypatch.setenv("MEMAIX_USER", "alice")
    server._rate_limiter._windows.clear()
    return vault, acl


# ------------------------------------------------------------------
# L2 — capabilities tool / memaix_help prompt / resource
# ------------------------------------------------------------------


def test_capabilities_overview_hides_locked_role_and_shows_hint(wired, monkeypatch):
    monkeypatch.setenv("MEMAIX_USER", "bob")  # reader — mail.send needs owner
    data = server.capabilities()
    titles = {c["key"] for area in data["areas"] for c in area["capabilities"]}
    assert "mail.send" not in titles
    reasons = {l["reason"] for l in data["locked"]}
    assert "no_role" in reasons or "no_mailbox" in reasons


def test_capabilities_drilldown_has_examples(wired):
    data = server.capabilities("memory")
    assert data["area"] == "memory"
    keys = {c["key"] for c in data["capabilities"]}
    assert "memory.remember" in keys
    remember = next(c for c in data["capabilities"] if c["key"] == "memory.remember")
    assert remember["examples"]
    assert remember["tools"]


def test_memaix_help_overview_then_drilldown(wired):
    overview = server.memaix_help()
    assert "Vad kan jag göra?" in overview
    detail = server.memaix_help("memory")
    assert "memory" in detail.lower() or "minne" in detail.lower()
    assert "?" in detail  # offers to act


def test_capabilities_resource_matches_tool(wired):
    assert server.capabilities_resource() == server.capabilities()


# ------------------------------------------------------------------
# L1 — build_tour + onboarding_complete wiring
# ------------------------------------------------------------------


def _t(key):
    from memaix_gateway.i18n import get_translator
    return get_translator("en")(key)


def test_build_tour_ranks_by_profile_tags():
    from memaix_gateway.acl import Acl

    acl = Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": "/tmp/whatever"}})
    available, _ = available_for(acl, "alice", [])
    tour = build_tour("Jag är projektledare och jobbar med backlog och sprintplanering.", available, _t)
    keys = [s["capability_key"] for s in tour["suggestions"]]
    assert keys[0] in ("pm.sprint_plan", "backlog.capture", "backlog.review")


def test_build_tour_falls_back_to_defaults_for_empty_profile():
    from memaix_gateway.acl import Acl

    acl = Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": "/tmp/whatever"}})
    available, _ = available_for(acl, "alice", [])
    tour = build_tour("", available, _t)
    keys = {s["capability_key"] for s in tour["suggestions"]}
    assert "memory.remember" in keys or "backlog.capture" in keys


def test_build_tour_examples_are_executable_strings():
    from memaix_gateway.acl import Acl

    acl = Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": "/tmp/whatever"}})
    available, _ = available_for(acl, "alice", [])
    tour = build_tour("", available, _t, max_items=2)
    assert len(tour["suggestions"]) <= 2
    for s in tour["suggestions"]:
        assert isinstance(s["example"], str) and s["example"]


def test_onboarding_complete_returns_tour(wired):
    result = server.onboarding_complete("Jag är projektledare. Jag jobbar med backlog och mail.")
    assert result["ok"] is True
    assert "tour" in result
    assert result["tour"]["suggestions"]


def test_onboarding_complete_denied_without_shared_grant(wired, monkeypatch):
    from memaix_gateway.acl import AccessDenied

    monkeypatch.setattr(server, "_user", lambda: "bob")
    with pytest.raises(AccessDenied):
        server.onboarding_complete("Profil")


# ------------------------------------------------------------------
# L3 — nudges
# ------------------------------------------------------------------


def test_suggest_returns_none_for_unmapped_tool(tmp_path):
    state = NudgeState.for_path(tmp_path / "n.db")
    assert suggest("alice", "whoami", [], state, now=1000.0) is None


def test_suggest_fires_once_within_min_gap(tmp_path):
    available = [
        Capability(
            key="calendar.manage", area="calendar", title_key="t", summary_key="s",
            tools=("calendar_create",), example_prompts_key="e",
        )
    ]
    state = NudgeState.for_path(tmp_path / "n.db")
    first = suggest("alice", "email_create_draft", available, state, now=1000.0, min_gap_h=6)
    assert first == {"capability_key": "calendar.manage", "title_key": "t"}
    second = suggest("alice", "email_create_draft", available, state, now=1000.0 + 60, min_gap_h=6)
    assert second is None
    third = suggest("alice", "email_create_draft", available, state, now=1000.0 + 6 * 3600 + 1, min_gap_h=6)
    assert third is not None


def test_suggest_never_fires_for_locked_capability(tmp_path):
    state = NudgeState.for_path(tmp_path / "n.db")
    # 'calendar.manage' is the mapped target for email_create_draft, but it's
    # not present in `available` (i.e. locked) — must not be suggested.
    assert suggest("alice", "email_create_draft", [], state, now=1000.0) is None


def test_next_suggestion_tool_returns_capability_or_empty(wired):
    result = server.next_suggestion("email_create_draft")
    assert result == {} or "capability_key" in result


def test_next_suggestion_is_sparse_across_calls(wired):
    first = server.next_suggestion("email_create_draft")
    second = server.next_suggestion("email_create_draft")
    # Whatever the first call returned, an immediate repeat must not re-fire.
    if first:
        assert second == {}
