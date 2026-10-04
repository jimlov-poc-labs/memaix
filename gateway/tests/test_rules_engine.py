# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for rules.engine.evaluate."""

from __future__ import annotations

import pytest

from memaix_gateway.acl import Acl
from memaix_gateway.rules.engine import evaluate
from memaix_gateway.rules.store import RulesStore


@pytest.fixture()
def store(tmp_path):
    return RulesStore.for_path(tmp_path / "rules.db")


@pytest.fixture()
def acl():
    return Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": "/v"}})


def test_matching_rule_runs_its_actions(store, acl):
    calls = []

    def fake_backlog_add(acl_, user, project, **kwargs):
        calls.append(kwargs)
        return {"id": "new-item"}

    store.add_rule(
        "alice", "proj", "New client mail",
        {"type": "mail", "from_contains": "@client.com"},
        [{"type": "backlog_add", "params": {"project": "proj", "title_from": "subject"}}],
    )
    event = {"type": "mail", "project": "proj", "id": "uid-1", "payload": {"from": "a@client.com", "subject": "Help!"}}
    results = evaluate(store, acl, event, tools={"backlog_add": fake_backlog_add})

    assert len(results) == 1
    assert results[0]["ok"] is True
    assert calls[0]["title"] == "Help!"


def test_dedupe_prevents_double_run_for_same_event(store, acl):
    calls = []

    def fake_action(acl_, user, project, **kwargs):
        calls.append(1)
        return {}

    store.add_rule(
        "alice", "proj", "r", {"type": "mail", "from_contains": "@client.com"},
        [{"type": "backlog_add", "params": {"project": "proj", "title_from": "subject"}}],
    )
    event = {"type": "mail", "project": "proj", "id": "uid-1", "payload": {"from": "a@client.com", "subject": "x"}}
    tools = {"backlog_add": fake_action}

    first = evaluate(store, acl, event, tools=tools)
    second = evaluate(store, acl, event, tools=tools)  # e.g. mail-poll saw it again
    assert len(first) == 1
    assert len(second) == 0  # already handled — not re-run
    assert len(calls) == 1


def test_non_matching_rule_is_skipped(store, acl):
    store.add_rule(
        "alice", "proj", "r", {"type": "mail", "from_contains": "@client.com"},
        [{"type": "backlog_add", "params": {"project": "proj", "title_from": "subject"}}],
    )
    event = {"type": "mail", "project": "proj", "id": "uid-2", "payload": {"from": "spam@random.com", "subject": "x"}}
    results = evaluate(store, acl, event, tools={})
    assert results == []


def test_disabled_rule_never_matches(store, acl):
    rule = store.add_rule(
        "alice", "proj", "r", {"type": "mail", "from_contains": "@client.com"}, []
    )
    store.set_enabled(rule["id"], False)
    event = {"type": "mail", "project": "proj", "id": "uid-3", "payload": {"from": "a@client.com"}}
    assert evaluate(store, acl, event, tools={}) == []


def test_one_failing_action_does_not_stop_the_rest(store, acl):
    def boom(acl_, user, project, **kwargs):
        raise RuntimeError("fail")

    def ok_fn(acl_, user, project, **kwargs):
        return {"ok": True}

    store.add_rule(
        "alice", "proj", "r", {"type": "mail", "from_contains": "@client.com"},
        [
            {"type": "backlog_add", "params": {"project": "proj", "title_from": "subject"}},
            {"type": "pm_raid_add", "params": {"project": "proj", "raid_type": "Risk", "summary": "x"}},
        ],
    )
    event = {"type": "mail", "project": "proj", "id": "uid-4", "payload": {"from": "a@client.com", "subject": "s"}}
    results = evaluate(store, acl, event, tools={"backlog_add": boom, "pm_raid_add": ok_fn})
    assert results[0]["ok"] is False  # overall rule marked failed
    assert len(results[0]["actions"]) == 2  # but both actions still ran
    assert results[0]["actions"][1]["ok"] is True


def test_dry_run_never_reserves_so_it_can_be_repeated(store, acl):
    def fake_action(acl_, user, project, **kwargs):
        return {"id": "x"}

    store.add_rule(
        "alice", "proj", "r", {"type": "mail", "from_contains": "@client.com"},
        [{"type": "backlog_add", "params": {"project": "proj", "title_from": "subject"}}],
    )
    event = {"type": "mail", "project": "proj", "id": "uid-5", "payload": {"from": "a@client.com", "subject": "s"}}
    tools = {"backlog_add": fake_action}

    first = evaluate(store, acl, event, tools=tools, dry_run=True)
    second = evaluate(store, acl, event, tools=tools, dry_run=True)
    assert len(first) == 1
    assert len(second) == 1  # dry_run doesn't consume the dedupe slot
    assert first[0]["actions"][0]["dry_run"] is True


def test_evaluate_scopes_to_event_project(store, acl):
    store.add_rule(
        "alice", "other-proj", "r", {"type": "mail", "from_contains": "@client.com"}, []
    )
    event = {"type": "mail", "project": "proj", "id": "uid-6", "payload": {"from": "a@client.com"}}
    assert evaluate(store, acl, event, tools={}) == []


def test_internal_event_triggers_rule():
    """Proves the engine handles internal (non-mail) events identically."""
    from memaix_gateway.rules.store import RulesStore as _RS
    import tempfile
    from pathlib import Path

    store = _RS.for_path(Path(tempfile.mkdtemp()) / "r.db")
    acl_ = Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": "/v"}})

    store.add_rule(
        "alice", "proj", "on-done",
        {"type": "internal", "event": "backlog.status", "to": "done"},
        [{"type": "notify", "params": {"text": "Item finished!"}}],
    )
    event = {
        "type": "internal", "project": "proj", "id": "backlog-item-1:done",
        "payload": {"event": "backlog.status", "to": "done", "from": "in-dev"},
    }
    results = evaluate(store, acl_, event, tools={"_channels": []})
    assert len(results) == 1
    assert results[0]["actions"][0]["ok"] is True


# ───────── Karakterisering (Sonar S3776-sanering av evaluate) ─────────

_MAIL = {"type": "mail", "from_contains": "@client.com"}
_ACTION = {"type": "backlog_add", "params": {"project": "proj", "title_from": "subject"}}


def _mail_event(eid="e1", **payload):
    return {"type": "mail", "project": "proj", "id": eid,
            "payload": {"from": "a@client.com", "subject": "s", **payload}}


def test_failing_conditions_skip_rule_without_reserving_the_event(store, acl):
    rule = store.add_rule("alice", "proj", "r", _MAIL, [_ACTION],
                          conditions=[{"field": "subject", "op": "contains", "value": "urgent"}])
    tools = {"backlog_add": lambda acl_, user, project, **kw: {"id": 1}}
    assert evaluate(store, acl, _mail_event("c1", subject="plain"), tools=tools) == []
    assert store.list_runs(rule["id"]) == []  # varken reserverad eller loggad
    # samma event-id med matchande villkor körs fortfarande (inget slot förbrukat)
    assert len(evaluate(store, acl, _mail_event("c1", subject="URGENT!"), tools=tools)) == 1


def test_result_shape_and_run_detail_for_success_and_failure(store, acl):
    def boom(acl_, user, project, **kw):
        raise RuntimeError("kraschade")

    ok_rule = store.add_rule("alice", "proj", "bra", _MAIL, [_ACTION])
    bad_rule = store.add_rule("alice", "proj", "dålig", _MAIL, [_ACTION, _ACTION])
    results = evaluate(store, acl, _mail_event("r1"), tools={"backlog_add": boom})
    by_name = {r["rule_name"]: r for r in results}
    assert set(by_name) == {"bra", "dålig"}  # ena regelns fel stoppar inte den andra
    assert by_name["bra"]["rule_id"] == ok_rule["id"] and by_name["bra"]["ok"] is False
    assert by_name["dålig"]["rule_id"] == bad_rule["id"]
    assert by_name["dålig"]["actions"] == [{"ok": False, "error": "kraschade"}] * 2
    runs = store.list_runs(bad_rule["id"])
    assert runs[0]["ok"] == 0 and runs[0]["detail"] == "kraschade; kraschade"
    assert runs[0]["event_key"] == "r1"

    fine = store.add_rule("alice", "proj", "fin", _MAIL, [_ACTION])
    res = evaluate(store, acl, _mail_event("r2"),
                   tools={"backlog_add": lambda acl_, user, project, **kw: {"id": 1}})
    assert [r["ok"] for r in res] == [True, True, True]
    run = store.list_runs(fine["id"])[0]
    assert run["ok"] == 1 and run["detail"] == ""


def test_rule_with_no_actions_is_ok_and_empty(store, acl):
    store.add_rule("alice", "proj", "tom", _MAIL, [])
    res = evaluate(store, acl, _mail_event("n1"), tools={})
    assert len(res) == 1 and res[0]["ok"] is True and res[0]["actions"] == []


def test_event_without_project_id_or_payload_uses_defaults(store, acl):
    store.add_rule("alice", "proj", "sched", {"type": "schedule", "cron": "* * * * *"}, [])
    res = evaluate(store, acl, {"type": "schedule"}, tools={})  # inget project/id/payload
    assert len(res) == 1
    # event_key blir "" — samma event igen är redan hanterat
    assert evaluate(store, acl, {"type": "schedule"}, tools={}) == []


def test_dry_run_records_no_run_detail(store, acl):
    rule = store.add_rule("alice", "proj", "r", _MAIL, [_ACTION])
    evaluate(store, acl, _mail_event("d1"), tools={"backlog_add": lambda *a, **k: {}}, dry_run=True)
    assert store.list_runs(rule["id"]) == []
