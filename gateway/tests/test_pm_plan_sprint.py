# SPDX-License-Identifier: AGPL-3.0-or-later
"""Characterization tests for tools.pm.pm_plan_sprint (messages, return
values, files written, backlog stamping, git commit flag)."""

from __future__ import annotations

import subprocess

import pytest

from memaix_gateway import frontmatter as fm
from memaix_gateway.acl import Acl, AccessDenied
from memaix_gateway.tools import pm as t_pm


@pytest.fixture()
def env(tmp_path):
    vault = tmp_path / "vault"
    (vault / "backlog").mkdir(parents=True)
    acl = Acl(
        users={"owner": {"grants": {"proj": "owner"}}, "reader": {"grants": {"proj": "reader"}}},
        projects={"proj": {"vault": str(vault)}, "novault": {}},
    )
    return acl, vault


def _item(vault, item_id, estimate=None, **extra):
    meta = {"id": item_id, "title": item_id, **extra}
    if estimate is not None:
        meta["estimate"] = estimate
    fm.write_atomic(vault / "backlog" / f"{item_id}.md", fm.join(meta, "body\n"))


def _playbook(vault, **meta):
    fm.write_atomic(vault / "playbook.md", fm.join(meta, "pb\n"))


def test_requires_owner(env):
    acl, _ = env
    with pytest.raises(AccessDenied):
        t_pm.pm_plan_sprint(acl, "reader", "proj", "S1", [])


def test_invalid_sprint_id_rejected(env):
    acl, _ = env
    with pytest.raises(ValueError):
        t_pm.pm_plan_sprint(acl, "owner", "proj", "../x", [])


def test_invalid_item_id_rejected(env):
    acl, _ = env
    with pytest.raises(ValueError):
        t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["../evil"])


def test_project_without_vault_raises(env):
    acl, _ = env
    acl.users["owner"]["grants"]["novault"] = "owner"
    with pytest.raises(ValueError, match="has no vault configured"):
        t_pm.pm_plan_sprint(acl, "owner", "novault", "S1", [])


def test_no_playbook_means_uncapped(env):
    acl, vault = env
    _item(vault, "A-1", 3)
    _item(vault, "A-2", 5)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1", "A-2"], goal="Ship it")

    assert r == {
        "ok": True, "sprint": "S1", "goal": "Ship it",
        "items": [{"id": "A-1", "estimate": 3}, {"id": "A-2", "estimate": 5}],
        "committed_points": 8, "capacity_points": None, "over_capacity": False,
        "warnings": ["no playbook capacity; sprint uncapped"], "errors": [], "committed": False,
    }
    text = (vault / "pm" / "sprints" / "S1.md").read_text(encoding="utf-8")
    assert "capacity_points: uncapped" in text
    assert "committed_points: 8" in text
    assert "length_days: 14" in text
    assert "goal: Ship it" in text
    assert "status: planned" in text
    assert "# S1 — Ship it" in text
    assert "Capacity: uncapped pts · Committed: 8 pts" in text
    assert "| A-1 | 3 |" in text and "| A-2 | 5 |" in text
    assert "  - id: A-1\n    estimate: 3" in text


def test_capacity_sums_playbook_and_custom_length(env):
    acl, vault = env
    _playbook(vault, capacity={"anna": 5, "erik": "3"}, sprint_length_days=7)
    _item(vault, "A-1", 4)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1"])

    assert r["capacity_points"] == 8
    assert r["over_capacity"] is False
    assert r["warnings"] == []
    text = (vault / "pm" / "sprints" / "S1.md").read_text(encoding="utf-8")
    assert "length_days: 7" in text
    assert "capacity_points: 8" in text


def test_over_capacity_warns(env):
    acl, vault = env
    _playbook(vault, capacity={"anna": 3})
    _item(vault, "A-1", 2)
    _item(vault, "A-2", 2)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1", "A-2"])

    assert r["over_capacity"] is True
    assert r["warnings"] == ["committed 4 > capacity 3"]
    assert r["ok"] is True


def test_exactly_at_capacity_is_not_over(env):
    acl, vault = env
    _playbook(vault, capacity={"anna": 4})
    _item(vault, "A-1", 4)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1"])

    assert r["over_capacity"] is False
    assert r["warnings"] == []


def test_empty_capacity_map_is_uncapped(env):
    acl, vault = env
    _playbook(vault, capacity={})

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", [])

    assert r["capacity_points"] is None
    assert r["warnings"] == ["no playbook capacity; sprint uncapped"]
    assert r["items"] == [] and r["committed_points"] == 0


def test_missing_estimate_counts_zero_with_warning(env):
    acl, vault = env
    _item(vault, "A-1")
    _item(vault, "A-2", 2)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1", "A-2"])

    assert r["warnings"] == ["no playbook capacity; sprint uncapped", "A-1: no estimate, counted as 0"]
    assert r["items"] == [{"id": "A-1", "estimate": 0}, {"id": "A-2", "estimate": 2}]
    assert r["committed_points"] == 2


def test_missing_items_return_errors_and_write_nothing(env):
    acl, vault = env
    _item(vault, "A-1", 2)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1", "NOPE-1", "NOPE-2"])

    assert r == {
        "ok": False,
        "errors": ["NOPE-1: not found", "NOPE-2: not found"],
        "warnings": ["no playbook capacity; sprint uncapped"],
    }
    assert not (vault / "pm" / "sprints" / "S1.md").exists()
    meta, _ = fm.split((vault / "backlog" / "A-1.md").read_text(encoding="utf-8"))
    assert "sprint" not in meta


def test_existing_sprint_is_overwritten_with_warning(env):
    acl, vault = env
    (vault / "pm" / "sprints").mkdir(parents=True)
    (vault / "pm" / "sprints" / "S1.md").write_text("old", encoding="utf-8")
    _item(vault, "A-1", 1)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1"])

    assert r["warnings"] == ["no playbook capacity; sprint uncapped", "S1 already exists; overwriting"]
    assert "old" not in (vault / "pm" / "sprints" / "S1.md").read_text(encoding="utf-8")


def test_items_get_stamped_with_sprint(env):
    acl, vault = env
    _item(vault, "A-1", 1, status="todo")

    t_pm.pm_plan_sprint(acl, "owner", "proj", "S9", ["A-1"])

    meta, body = fm.split((vault / "backlog" / "A-1.md").read_text(encoding="utf-8"))
    assert meta["sprint"] == "S9"
    assert meta["status"] == "todo"
    assert meta["updated_at"]
    assert body.strip() == "body"


def test_empty_item_list_and_default_goal(env):
    acl, vault = env

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", [])

    assert r["goal"] == "" and r["items"] == [] and r["ok"] is True
    text = (vault / "pm" / "sprints" / "S1.md").read_text(encoding="utf-8")
    assert "# S1 — \n" in text


def test_commits_when_vault_is_a_git_repo(env):
    acl, vault = env
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    _item(vault, "A-1", 2)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1"])

    assert r["committed"] is True
    log = subprocess.run(
        ["git", "-C", str(vault), "log", "--format=%s", "-1"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert log == "pm: plan S1 (2 pts)"
    files = subprocess.run(
        ["git", "-C", str(vault), "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert sorted(files) == ["backlog/A-1.md", "pm/sprints/S1.md"]


def test_item_without_frontmatter_is_counted_but_not_stamped(env):
    acl, vault = env
    (vault / "backlog" / "A-1.md").write_text("just text, no frontmatter\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(vault)], check=True)

    r = t_pm.pm_plan_sprint(acl, "owner", "proj", "S1", ["A-1"])

    assert r["items"] == [{"id": "A-1", "estimate": 0}]
    assert "A-1: no estimate, counted as 0" in r["warnings"]
    assert (vault / "backlog" / "A-1.md").read_text(encoding="utf-8") == "just text, no frontmatter\n"
    files = subprocess.run(
        ["git", "-C", str(vault), "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert files == ["pm/sprints/S1.md"]
