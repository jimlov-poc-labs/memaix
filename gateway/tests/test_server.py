# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for the server-layer tool wrappers (_tool_call).

Drives tools through the FastMCP-registered functions in stdio mode
(MEMAIX_USER) to verify the shared identity + rate-limit + ACL + audit path.
"""

from __future__ import annotations

import time

import pytest

from memaix_gateway import server
from memaix_gateway.acl import AccessDenied, Acl
from memaix_gateway.outbox.queue import ActionQueue
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.notify.store import NotifyStore
from memaix_gateway.rules.store import RulesStore
from memaix_gateway.search.embedder import FakeEmbedder
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.timeline.store import ActionsStore


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True)
    acl = Acl(
        users={
            "alice": {"grants": {"proj": "owner"}},
            "bob": {"grants": {"proj": "reader"}},
        },
        projects={"proj": {"vault": str(vault)}},
    )
    AuditLog._clear_instances()
    monkeypatch.setattr(server, "_acl", acl)
    monkeypatch.setattr(server, "_audit", AuditLog.for_path(tmp_path / "audit.db"))
    monkeypatch.setattr(server, "_outbox_queue", ActionQueue.for_path(tmp_path / "outbox.db"))
    monkeypatch.setattr(server, "_timeline_store", ActionsStore.for_path(tmp_path / "actions.db"))
    monkeypatch.setattr(server, "_search_store", EmbeddingStore.for_path(tmp_path / "index.db"))
    monkeypatch.setattr(server, "_search_embedder", None)
    monkeypatch.setattr(server, "_search_embedder_loaded", True)  # skip config.load(); no embedder by default
    monkeypatch.setattr(server, "_notify_store", NotifyStore.for_path(tmp_path / "notify.db"))
    monkeypatch.setattr(server, "_rules_store", RulesStore.for_path(tmp_path / "rules.db"))
    monkeypatch.setenv("MEMAIX_USER", "alice")
    server._rate_limiter._windows.clear()
    return vault, acl


def test_files_roundtrip_via_server(wired):
    server.files_write("proj", "/notes.txt", "hello")
    assert server.files_read("proj", "/notes.txt") == "hello"


def test_backlog_via_server_records_audit(wired):
    r = server.backlog_add("proj", "Title", "Body")
    assert server.backlog_get("proj", r["id"])["title"] == "Title"
    tools = [e["tool"] for e in server._get_audit().tail(20)]
    assert "backlog_add" in tools and "backlog_get" in tools


def test_server_enforces_acl(wired, monkeypatch):
    # bob is only a reader; files_read requires collaborator.
    monkeypatch.setenv("MEMAIX_USER", "bob")
    with pytest.raises(AccessDenied):
        server.files_read("proj", "/notes.txt")


def test_server_enforces_rate_limit(wired):
    # Fill the user's window so the next call is denied.
    server._rate_limiter._inject_timestamps("user:alice", [time.monotonic()] * 60)
    with pytest.raises(RuntimeError, match="rate_limited"):
        server.memory_search("proj", "anything")


def test_failed_tool_call_is_audited(wired):
    with pytest.raises(Exception):
        server.backlog_get("proj", "../../escape")  # blocked by path validation
    last = server._get_audit().tail(5)
    assert any(e["tool"] == "backlog_get" and e["ok"] is False for e in last)


# ------------------------------------------------------------------
# TOKEN_MASTER_KEY hardening
# ------------------------------------------------------------------


def test_http_mode_requires_master_key(monkeypatch):
    monkeypatch.setattr(server, "_token_store", None)
    monkeypatch.delenv("TOKEN_MASTER_KEY", raising=False)
    monkeypatch.delenv("MEMAIX_ALLOW_EPHEMERAL_KEY", raising=False)
    monkeypatch.setenv("MEMAIX_TRANSPORT", "http")
    with pytest.raises(RuntimeError, match="TOKEN_MASTER_KEY"):
        server._get_token_store()


def test_http_mode_ephemeral_opt_in(monkeypatch):
    monkeypatch.setattr(server, "_token_store", None)
    monkeypatch.delenv("TOKEN_MASTER_KEY", raising=False)
    monkeypatch.setenv("MEMAIX_TRANSPORT", "http")
    monkeypatch.setenv("MEMAIX_ALLOW_EPHEMERAL_KEY", "1")
    with pytest.warns(RuntimeWarning):
        store = server._get_token_store()
    assert store is not None


def test_stdio_mode_allows_ephemeral_key(monkeypatch):
    monkeypatch.setattr(server, "_token_store", None)
    monkeypatch.delenv("TOKEN_MASTER_KEY", raising=False)
    monkeypatch.delenv("MEMAIX_TRANSPORT", raising=False)
    monkeypatch.delenv("MEMAIX_ALLOW_EPHEMERAL_KEY", raising=False)
    with pytest.warns(RuntimeWarning):
        store = server._get_token_store()
    assert store is not None


# ------------------------------------------------------------------
# Identity fail-closed in HTTP mode
# ------------------------------------------------------------------


def test_user_fails_closed_in_http_mode_without_token(monkeypatch):
    # HTTP mode with no verified OAuth subject in context must deny — never
    # silently fall through to the MEMAIX_USER env identity.
    monkeypatch.setenv("MEMAIX_TRANSPORT", "http")
    monkeypatch.setenv("MEMAIX_USER", "alice")  # present, but must be ignored in HTTP mode
    with pytest.raises(RuntimeError, match="HTTP mode"):
        server._user()


def test_user_uses_env_in_stdio_mode(monkeypatch):
    monkeypatch.delenv("MEMAIX_TRANSPORT", raising=False)
    monkeypatch.setenv("MEMAIX_USER", "alice")
    assert server._user() == "alice"


# ------------------------------------------------------------------
# Outbox tools (FEATURE-APPROVAL-OUTBOX.md)
# ------------------------------------------------------------------


def test_outbox_list_and_get_via_server(wired):
    aid = server._get_outbox().enqueue("alice", "proj", "email_send", {"to": "x@y.com"}, "p")
    listed = server.outbox_list("proj", "pending")
    assert any(a["id"] == aid for a in listed)
    fetched = server.outbox_get(aid)
    assert fetched["id"] == aid


def test_outbox_get_denied_for_invisible_project(wired):
    aid = server._get_outbox().enqueue("alice", "other-proj", "email_send", {}, "p")
    with pytest.raises(AccessDenied):
        server.outbox_get(aid)


def test_outbox_approve_executes_and_is_idempotent(wired, monkeypatch):
    aid = server._get_outbox().enqueue(
        "alice", "proj", "email_send", {"to": "x@y.com", "subject": "s", "body": "b", "cc": None}, "p"
    )
    calls = []

    def fake_email_send(acl, user, project, **kwargs):
        calls.append(kwargs)
        return {"status": "sent"}

    monkeypatch.setattr(
        "memaix_gateway.outbox.execute._default_dispatch",
        lambda: {"email_send": fake_email_send},
    )

    result = server.outbox_approve(aid)
    assert result["ok"] is True
    assert calls[0]["_confirmed"] is True
    assert server.outbox_get(aid)["status"] == "executed"

    # Second approval on the same action is a no-op conflict, not a re-send.
    second = server.outbox_approve(aid)
    assert second["conflict"] is True
    assert len(calls) == 1


def test_outbox_approve_requires_correct_role(wired):
    aid = server._get_outbox().enqueue("alice", "proj", "email_send", {"to": "x@y.com"}, "p")
    import os
    os.environ["MEMAIX_USER"] = "bob"  # reader — email_send needs owner
    try:
        with pytest.raises(AccessDenied):
            server.outbox_approve(aid)
    finally:
        os.environ["MEMAIX_USER"] = "alice"
    assert server.outbox_get(aid)["status"] == "pending"


def test_outbox_reject_never_executes(wired, monkeypatch):
    aid = server._get_outbox().enqueue("alice", "proj", "email_send", {"to": "x@y.com"}, "p")
    calls = []
    monkeypatch.setattr(
        "memaix_gateway.outbox.execute._default_dispatch",
        lambda: {"email_send": lambda *a, **kw: calls.append(1) or {"status": "sent"}},
    )
    result = server.outbox_reject(aid, reason="not needed")
    assert result["status"] == "rejected"
    assert calls == []
    assert server.outbox_get(aid)["status"] == "rejected"

    # Rejecting again is a conflict, not a second decision.
    second = server.outbox_reject(aid)
    assert second["conflict"] is True


def test_outbox_approve_records_failure_on_exception(wired, monkeypatch):
    aid = server._get_outbox().enqueue("alice", "proj", "email_send", {"to": "x@y.com"}, "p")

    def boom(*a, **kw):
        raise RuntimeError("smtp unreachable")

    monkeypatch.setattr(
        "memaix_gateway.outbox.execute._default_dispatch",
        lambda: {"email_send": boom},
    )
    result = server.outbox_approve(aid)
    assert result["ok"] is False
    assert server.outbox_get(aid)["status"] == "failed"


# ------------------------------------------------------------------
# Timeline tools (FEATURE-UNDO-TIMELINE.md)
# ------------------------------------------------------------------


def test_memory_write_via_server_is_recorded_and_undoable(wired):
    server.memory_write("proj", "notes/x.md", "hello")
    entries = server.timeline_list("proj")
    assert len(entries) == 1
    assert entries[0]["tool"] == "memory_write"
    assert entries[0]["reversible"] is True

    hist_before = server.memory_history("proj", "notes/x.md")
    result = server.timeline_undo(entries[0]["id"])
    assert result["ok"] is True
    hist_after = server.memory_history("proj", "notes/x.md")
    assert len(hist_after) > len(hist_before)  # memory_revert added a new commit

    all_entries = server.timeline_list("proj")
    assert any(e.get("undo_of") == entries[0]["id"] for e in all_entries)


def test_backlog_add_via_server_is_recorded_and_undoable(wired):
    r = server.backlog_add("proj", "New idea", "desc")
    entries = server.timeline_list("proj")
    add_entry = next(e for e in entries if e["tool"] == "backlog_add")
    assert add_entry["reversible"] is True

    result = server.timeline_undo(add_entry["id"])
    assert result["ok"] is True
    item = server.backlog_get("proj", r["id"])
    assert item["status"] == "rejected"


def test_timeline_undo_second_call_refuses(wired):
    server.memory_write("proj", "notes/x.md", "hello")
    aid = server.timeline_list("proj")[0]["id"]
    assert server.timeline_undo(aid)["ok"] is True
    second = server.timeline_undo(aid)
    assert second["ok"] is False


def test_failed_tool_call_is_not_recorded_in_timeline(wired, monkeypatch):
    monkeypatch.setenv("MEMAIX_USER", "bob")  # reader — can't write memory
    with pytest.raises(Exception):
        server.memory_write("proj", "notes/x.md", "hello")
    monkeypatch.setenv("MEMAIX_USER", "alice")
    assert server.timeline_list("proj") == []


def test_timeline_list_filters_invisible_projects(wired):
    server.memory_write("proj", "notes/x.md", "hello")
    # Alice can't see a project she has no grant on.
    other_entries = server.timeline_list("no-such-project")
    assert other_entries == []


def test_queued_outbox_action_is_not_recorded_in_timeline(wired):
    """A tool result of {'pending': True, ...} means nothing happened yet —
    it must not show up in the timeline until it's actually approved/executed."""
    server._maybe_record_timeline(
        "alice", "proj", "calendar_create", ("Standup",), {},
        {"pending": True, "action_id": "x"},
    )
    assert server.timeline_list("proj") == []


# ------------------------------------------------------------------
# Search tools (FEATURE-SEMANTIC-SEARCH.md)
# ------------------------------------------------------------------


def test_memory_write_is_indexed_and_searchable(wired):
    server.memory_write("proj", "notes/invoice.md", "the invoice is overdue")
    result = server.search_all("invoice")
    assert result["semantic"] is False  # no embedder configured in this fixture
    refs = [r["ref"] for r in result["results"]]
    assert "notes/invoice.md" in refs


def test_backlog_add_is_indexed_and_searchable(wired):
    r = server.backlog_add("proj", "Fix invoice bug", "customers see wrong totals")
    result = server.search_all("invoice")
    hits = [x for x in result["results"] if x["source_type"] == "backlog"]
    assert any(h["ref"] == r["id"] for h in hits)


def test_files_write_is_indexed_and_searchable(wired):
    server.files_write("proj", "/docs/charter.txt", "project charter and scope")
    result = server.search_all("charter")
    refs = [r["ref"] for r in result["results"]]
    assert "/docs/charter.txt" in refs


def test_search_all_hides_files_from_reader(wired, monkeypatch):
    server.files_write("proj", "/secret.txt", "confidential budget numbers")
    monkeypatch.setenv("MEMAIX_USER", "bob")
    result = server.search_all("confidential")
    assert result["results"] == []


def test_search_reindex_requires_owner(wired, monkeypatch):
    monkeypatch.setenv("MEMAIX_USER", "bob")
    with pytest.raises(AccessDenied):
        server.search_reindex("proj")


def test_search_reindex_rebuilds_index(wired):
    server.memory_write("proj", "notes/a.md", "quarterly roadmap")
    result = server.search_reindex("proj")
    assert result["sources"] >= 1
    assert server.search_all("roadmap")["results"]


def test_search_status_reports_embedder_and_counts(wired):
    server.memory_write("proj", "notes/a.md", "content")
    status = server.search_status()
    assert status["semantic_enabled"] is False
    assert status["chunks_by_project"]["proj"] >= 1


def test_failed_write_is_not_indexed(wired, monkeypatch):
    monkeypatch.setenv("MEMAIX_USER", "bob")  # reader — can't write
    with pytest.raises(Exception):
        server.memory_write("proj", "notes/x.md", "hello")
    monkeypatch.setenv("MEMAIX_USER", "alice")
    assert server.search_all("hello")["results"] == []


def test_indexing_hook_failure_does_not_break_the_write(wired, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("index db is on fire")

    monkeypatch.setattr(server, "_get_search_store", boom)
    result = server.memory_write("proj", "notes/x.md", "hello")
    assert result["path"] == "notes/x.md"  # the actual write still succeeded


# ------------------------------------------------------------------
# Brief tools (FEATURE-PROACTIVE-BRIEF.md)
# ------------------------------------------------------------------


def test_brief_configure_then_status_reflects_settings(wired):
    result = server.brief_configure(
        enabled=True, brief_time="08:30", timezone="Europe/Stockholm",
        channels=[{"type": "ntfy", "topic": "t"}],
    )
    assert result["ok"] is True
    assert result["next_run"]

    status = server.brief_status()
    assert status["configured"] is True
    assert status["prefs"]["brief_time"] == "08:30"
    assert status["prefs"]["timezone"] == "Europe/Stockholm"
    assert status["next_run"] is not None


def test_brief_status_unconfigured(wired):
    assert server.brief_status() == {"configured": False}


def test_brief_configure_uses_config_default_timezone(wired, monkeypatch):
    monkeypatch.setattr(
        server.config, "load",
        lambda: {"memaix": {"brief": {"default_timezone": "Europe/Stockholm"}}},
    )
    result = server.brief_configure(enabled=True)
    assert result["prefs"]["timezone"] == "Europe/Stockholm"


def test_brief_configure_explicit_timezone_overrides_config_default(wired, monkeypatch):
    monkeypatch.setattr(
        server.config, "load",
        lambda: {"memaix": {"brief": {"default_timezone": "Europe/Stockholm"}}},
    )
    result = server.brief_configure(enabled=True, timezone="UTC")
    assert result["prefs"]["timezone"] == "UTC"


def test_brief_configure_rejects_bad_time(wired):
    with pytest.raises(ValueError):
        server.brief_configure(enabled=True, brief_time="25:99")


def test_brief_configure_rejects_bad_timezone(wired):
    with pytest.raises(ValueError):
        server.brief_configure(enabled=True, timezone="Not/AZone")


def test_brief_configure_rejects_unknown_channel_type(wired):
    with pytest.raises(ValueError):
        server.brief_configure(enabled=True, channels=[{"type": "carrier-pigeon"}])


def test_brief_preview_returns_content_without_sending(wired):
    server.memory_write("proj", "notes/a.md", "hello")
    content = server.brief_preview()
    assert "markdown" in content
    assert isinstance(content["markdown"], str)


def test_daily_brief_prompt_matches_preview_markdown(wired):
    prompt_text = server.daily_brief()
    preview = server.brief_preview()
    assert prompt_text == preview["markdown"]


def test_brief_send_now_requires_configuration_first(wired):
    result = server.brief_send_now()
    assert result == {"ok": False, "error": "brief not configured — call brief_configure first"}


def test_brief_send_now_delivers_via_configured_channel(wired, monkeypatch):
    server.brief_configure(enabled=True, brief_time="07:00", timezone="UTC")

    sent = []
    monkeypatch.setattr(
        "memaix_gateway.notify.deliver.build_channels",
        lambda specs, **kw: [type("C", (), {"send": staticmethod(lambda s, m, t: sent.append((s, m, t)))})()],
    )
    result = server.brief_send_now()
    assert result["ok"] is True
    assert len(sent) == 1


def test_brief_send_now_ignores_quiet_hours(wired, monkeypatch):
    server.brief_configure(
        enabled=True, brief_time="07:00", timezone="UTC",
        quiet_hours={"start": "00:00", "end": "23:59"},  # always quiet
    )
    sent = []
    monkeypatch.setattr(
        "memaix_gateway.notify.deliver.build_channels",
        lambda specs, **kw: [type("C", (), {"send": staticmethod(lambda s, m, t: sent.append(1))})()],
    )
    result = server.brief_send_now()
    assert result["ok"] is True
    assert sent == [1]


# ------------------------------------------------------------------
# Automation rules & standing instructions (FEATURE-AUTOMATION-RULES.md)
# ------------------------------------------------------------------


def test_rule_add_then_list(wired):
    result = server.rule_add(
        "proj", "New client mail", {"type": "mail", "from_contains": "@client.com"},
        [{"type": "backlog_add", "params": {"project": "proj", "title_from": "subject"}}],
    )
    assert result["ok"] is True
    rules = server.rule_list("proj")
    assert len(rules) == 1
    assert rules[0]["name"] == "New client mail"


def test_rule_add_rejects_unknown_trigger_type(wired):
    with pytest.raises(ValueError):
        server.rule_add("proj", "r", {"type": "carrier-pigeon"}, [{"type": "notify", "params": {}}])


def test_rule_add_rejects_empty_actions(wired):
    with pytest.raises(ValueError):
        server.rule_add("proj", "r", {"type": "mail"}, [])


def test_rule_add_with_outgoing_action_requires_owner(wired, monkeypatch):
    monkeypatch.setenv("MEMAIX_USER", "bob")  # reader on 'proj' in test_server acl? no—bob is reader
    with pytest.raises(AccessDenied):
        server.rule_add(
            "proj", "r", {"type": "mail"},
            [{"type": "email_send", "params": {"project": "proj", "to": "x@y.com"}}],
        )


def test_rule_set_enabled_and_delete(wired):
    added = server.rule_add("proj", "r", {"type": "mail"}, [{"type": "notify", "params": {"text": "hi"}}])
    rule_id = added["rule"]["id"]

    assert server.rule_set_enabled(rule_id, False)["ok"] is True
    rules = server.rule_list("proj")
    assert rules[0]["enabled"] is False

    assert server.rule_delete(rule_id)["ok"] is True
    assert server.rule_list("proj") == []


def test_rule_management_requires_owner(wired, monkeypatch):
    added = server.rule_add("proj", "r", {"type": "mail"}, [{"type": "notify", "params": {"text": "hi"}}])
    rule_id = added["rule"]["id"]
    monkeypatch.setenv("MEMAIX_USER", "bob")
    with pytest.raises(AccessDenied):
        server.rule_set_enabled(rule_id, False)
    with pytest.raises(AccessDenied):
        server.rule_delete(rule_id)


def test_rule_test_dry_runs_without_executing(wired, monkeypatch):
    added = server.rule_add(
        "proj", "r", {"type": "mail", "from_contains": "@client.com"},
        [{"type": "backlog_add", "params": {"project": "proj", "title_from": "subject"}}],
    )
    rule_id = added["rule"]["id"]

    before = server.backlog_list("proj")["items"]
    result = server.rule_test(
        rule_id, {"type": "mail", "project": "proj", "id": "x", "payload": {"from": "a@client.com", "subject": "Hi"}}
    )
    assert result["matched"] is True
    assert result["result"]["actions"][0]["dry_run"] is True
    after = server.backlog_list("proj")["items"]
    assert before == after  # nothing was actually created


def test_standing_set_and_get(wired):
    assert server.standing_get() == {"text": ""}
    server.standing_set("Always answer in Swedish.")
    assert server.standing_get() == {"text": "Always answer in Swedish."}


def test_standing_instructions_resource(wired):
    server.standing_set("Be concise.")
    assert server.standing_instructions_resource() == "Be concise."


def test_backlog_status_change_triggers_internal_rule(wired):
    r = server.backlog_add("proj", "Fix bug", "desc")
    item_id = r["id"]
    server.rule_add(
        "proj", "on-done", {"type": "internal", "event": "backlog.status", "to": "done"},
        [{"type": "notify", "params": {"text": "Item done!"}}],
    )
    sent = []
    import memaix_gateway.rules.actions as actions_mod
    orig_run_notify = actions_mod._run_notify
    actions_mod._run_notify = lambda acl, user, params, *, tools, **_: (sent.append(params) or {"ok": True, "errors": [], "channels_used": 0})
    try:
        server.backlog_set_status("proj", item_id, "done", expected_version=1)
    finally:
        actions_mod._run_notify = orig_run_notify
    assert sent == [{"text": "Item done!"}]


def test_backlog_status_change_conflict_does_not_trigger_rule(wired):
    r = server.backlog_add("proj", "Fix bug", "desc")
    item_id = r["id"]
    server.rule_add(
        "proj", "on-done", {"type": "internal", "event": "backlog.status", "to": "done"},
        [{"type": "notify", "params": {"text": "Item done!"}}],
    )
    sent = []
    import memaix_gateway.rules.actions as actions_mod
    orig_run_notify = actions_mod._run_notify
    actions_mod._run_notify = lambda acl, user, params, *, tools, **_: sent.append(1)
    try:
        result = server.backlog_set_status("proj", item_id, "done", expected_version=99)  # wrong version
        assert result.get("conflict") is True
    finally:
        actions_mod._run_notify = orig_run_notify


# ------------------------------------------------------------------
# _resolve_calendar_dav write= — regression for the '_MultiCalendarAdapter'
# object has no attribute 'create_event' bug (PR #100 shipped a half-fix
# that patched booking/routes.py but never fixed the actual resolver, so
# an authenticated calendar_create call still crashed for any project with
# both calendar_sa and a linked per-user account).
# ------------------------------------------------------------------


class _FakeNormalDav:
    """Stands in for whatever _resolve_normal_calendar_dav would return
    (e.g. _PerUserGoogleAdapter) — the important bit is it implements the
    write methods, unlike _ServiceAccountGoogleCalendarAdapter/
    _MultiCalendarAdapter."""

    def create_event(self, *a, **kw):
        return {"id": "ev1"}


class _FakeSaDav:
    """Stands in for _ServiceAccountGoogleCalendarAdapter — read-only,
    no create_event/update_event/delete_event."""

    def list_events(self, start, end):
        return []

    find_events = list_events


@pytest.fixture()
def sa_and_normal_calendar(monkeypatch, tmp_path):
    """A project with both calendar_sa and a normal per-user Google account
    configured — the exact production shape that made _resolve_calendar_dav
    return a read-only _MultiCalendarAdapter for a write caller."""
    acl = Acl(
        users={"alice": {"grants": {"proj": "owner"}}},
        projects={"proj": {
            "calendar": {"type": "google", "auth": "per_user"},
            "calendar_sa": {
                "type": "google", "auth": "service_account",
                "service_account_ref": "env:FAKE_SA_JSON",
                "impersonate": "alice@example.com",
            },
        }},
    )
    monkeypatch.setattr(server, "_acl", acl)
    monkeypatch.setenv("FAKE_SA_JSON", "{}")

    class _FakeTokenStore:
        def list_accounts(self, user):
            return [{"provider": "google", "account": "alice@example.com"}]

    monkeypatch.setattr(server, "_get_token_store", lambda: _FakeTokenStore())
    monkeypatch.setattr(server.config, "load", lambda: {"memaix": {}})
    monkeypatch.setattr(
        server, "_resolve_normal_calendar_dav",
        lambda acl, cfg, store, project, user, all_accounts, require_per_user, public_url: _FakeNormalDav(),
    )
    monkeypatch.setattr(
        server, "_ServiceAccountGoogleCalendarAdapter",
        lambda sa_info, impersonate: _FakeSaDav(),
    )
    return acl


def test_resolve_calendar_dav_write_skips_sa_merge(sa_and_normal_calendar):
    """The write path must get back the single writable adapter, never the
    SA adapter or a _MultiCalendarAdapter merge of the two — neither of
    those implements create_event/update_event/delete_event."""
    dav = server._resolve_calendar_dav("proj", "alice", write=True)
    assert isinstance(dav, _FakeNormalDav)
    dav.create_event()  # would raise AttributeError before the fix


def test_resolve_calendar_dav_read_still_merges_sa(sa_and_normal_calendar):
    """Unchanged behaviour for read callers (calendar_list/_find_free):
    the SA calendar's events should still show up alongside the user's own,
    via the merged adapter."""
    from memaix_gateway.tools.calendar import _MultiCalendarAdapter

    dav = server._resolve_calendar_dav("proj", "alice")
    assert isinstance(dav, _MultiCalendarAdapter)


def test_backlog_list_is_slim_and_paged(wired):
    for i in range(5):
        server.backlog_add("proj", f"Item {i}", "A long description " * 50)

    first = server.backlog_list("proj", limit=2)
    assert first["total"] == 5 and first["limit"] == 2 and first["offset"] == 0
    assert len(first["items"]) == 2 and first["next_offset"] == 2
    assert "description" not in first["items"][0]
    assert {"id", "title", "status", "version"} <= set(first["items"][0])

    last = server.backlog_list("proj", limit=2, offset=4)
    assert len(last["items"]) == 1 and last["next_offset"] is None

    seen = []
    nxt = 0
    while nxt is not None:
        page = server.backlog_list("proj", limit=2, offset=nxt)
        seen += [it["id"] for it in page["items"]]
        nxt = page["next_offset"]
    assert len(seen) == len(set(seen)) == 5


def test_backlog_list_clamps_limit_and_offset(wired):
    server.backlog_add("proj", "One", "x")
    page = server.backlog_list("proj", limit=10_000, offset=-5)
    assert page["limit"] == 200 and page["offset"] == 0
    assert server.backlog_list("proj", limit=0)["limit"] == 1
