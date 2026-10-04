# SPDX-License-Identifier: AGPL-3.0-or-later
"""Characterization tests for the board's ``api_board`` route: auth, ACL,
vault lookup, sprint filtering and column grouping/sorting. They pin the
current behaviour so the function can be restructured without changing it."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.board import routes as board_routes_mod

COLUMN_KEYS = ["inbox", "triaged", "evaluated", "approved", "in-dev", "done", "rejected"]


def _card(vault, cid, status=None, value=None, updated=None, title=None):
    lines = ["---", f"id: {cid}", f"title: {title or cid}"]
    if status is not None:
        lines.append(f"status: {status}")
    if value is not None:
        lines.append(f"value: {value}")
    if updated is not None:
        lines.append(f"updated_at: '{updated}'")
    lines += ["---", "body", ""]
    d = vault / "backlog"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.md").write_text("\n".join(lines), encoding="utf-8")


def _sprint(vault, sid, status, items):
    d = vault / "pm" / "sprints"
    d.mkdir(parents=True, exist_ok=True)
    body = ["---", f"id: {sid}", f"status: {status}", "goal: g"]
    if items:
        body.append("items:")
        body += [f"  - id: {i}" for i in items]
    else:
        body.append("items: []")
    body += ["---", ""]
    (d / f"{sid}.md").write_text("\n".join(body), encoding="utf-8")


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    acl = Acl(
        users={"alice": {"grants": {"proj": "owner"}}},
        projects={"proj": {"vault": str(vault)}, "novault": {}},
    )
    acl.users["alice"]["grants"]["novault"] = "owner"
    monkeypatch.setattr(board_routes_mod, "_acl", lambda: acl)
    state = {"user": "alice"}
    monkeypatch.setattr(board_routes_mod, "_require_user", lambda request: state["user"])
    app = Starlette(routes=[r for r in board_routes_mod.board_routes if r.path == "/board/api/board"])
    return TestClient(app), vault, state


def _ids(body, key):
    col = next(c for c in body["columns"] if c["key"] == key)
    return [c["id"] for c in col["cards"]]


def test_unauthenticated_is_401(rig):
    client, _, state = rig
    state["user"] = None
    r = client.get("/board/api/board?project=proj")
    assert r.status_code == 401
    assert r.json() == {"error": "not authenticated"}


def test_project_not_visible_is_403(rig):
    client, _, _ = rig
    r = client.get("/board/api/board?project=other")
    assert r.status_code == 403
    assert r.json() == {"error": "access denied"}


def test_missing_project_param_is_403(rig):
    client, _, _ = rig
    assert client.get("/board/api/board").status_code == 403


def test_user_without_grant_is_403(rig):
    client, _, state = rig
    state["user"] = "mallory"
    assert client.get("/board/api/board?project=proj").status_code == 403


def test_project_without_vault_is_404(rig):
    client, _, _ = rig
    r = client.get("/board/api/board?project=novault")
    assert r.status_code == 404
    assert r.json() == {"error": "no vault for project"}


def test_empty_vault_returns_all_columns_empty(rig):
    client, _, _ = rig
    r = client.get("/board/api/board?project=proj")
    assert r.status_code == 200
    body = r.json()
    assert body["project"] == "proj"
    assert body["sprint"] is None
    assert body["total_cards"] == 0
    assert [c["key"] for c in body["columns"]] == COLUMN_KEYS
    assert [c["label"] for c in body["columns"]][0] == "Inbox"
    assert all(c["cards"] == [] for c in body["columns"])
    assert [c["muted"] for c in body["columns"]] == [False] * 6 + [True]


def test_cards_grouped_by_status_unknown_and_missing_go_to_inbox(rig):
    client, vault, _ = rig
    _card(vault, "A", status="done")
    _card(vault, "B", status="bogus")
    _card(vault, "C")  # no status -> inbox
    (vault / "backlog" / "broken.md").write_text("---\nid: [unclosed\n---\n", encoding="utf-8")
    body = client.get("/board/api/board?project=proj").json()
    assert body["total_cards"] == 4
    assert _ids(body, "done") == ["A"]
    assert sorted(_ids(body, "inbox")) == ["B", "C", "broken"]


def test_cards_sorted_by_value_desc_then_updated_at_asc(rig):
    client, vault, _ = rig
    _card(vault, "low", status="inbox", value=1, updated="2026-01-01")
    _card(vault, "high", status="inbox", value=9, updated="2026-01-05")
    _card(vault, "none-late", status="inbox", updated="2026-02-01")
    _card(vault, "none-early", status="inbox", updated="2026-01-01")
    body = client.get("/board/api/board?project=proj").json()
    assert _ids(body, "inbox") == ["high", "low", "none-early", "none-late"]


def _sprint_rig(vault):
    for cid in ("A", "B", "C"):
        _card(vault, cid, status="triaged")
    _sprint(vault, "s1", "completed", ["A"])
    _sprint(vault, "s2", "active", ["B", "C"])


def test_sprint_active_filters_to_active_sprint_items(rig):
    client, vault, _ = rig
    _sprint_rig(vault)
    body = client.get("/board/api/board?project=proj&sprint=active").json()
    assert body["sprint"] == "s2"
    assert body["total_cards"] == 2
    assert sorted(_ids(body, "triaged")) == ["B", "C"]


def test_sprint_active_in_progress_status_counts_as_active(rig):
    client, vault, _ = rig
    _card(vault, "A", status="triaged")
    _card(vault, "B", status="triaged")
    _sprint(vault, "s1", "in-progress", ["A"])
    body = client.get("/board/api/board?project=proj&sprint=active").json()
    assert body["sprint"] == "s1"
    assert _ids(body, "triaged") == ["A"]


def test_sprint_active_without_active_sprint_does_not_filter(rig):
    client, vault, _ = rig
    _card(vault, "A", status="triaged")
    _sprint(vault, "s1", "completed", [])
    body = client.get("/board/api/board?project=proj&sprint=active").json()
    assert body["sprint"] == "active"  # falls back to the raw filter value
    assert body["total_cards"] == 1


def test_sprint_active_without_any_sprints_dir(rig):
    client, vault, _ = rig
    _card(vault, "A")
    body = client.get("/board/api/board?project=proj&sprint=active").json()
    assert body["sprint"] == "active"
    assert body["total_cards"] == 1


def test_sprint_by_id_filters(rig):
    client, vault, _ = rig
    _sprint_rig(vault)
    body = client.get("/board/api/board?project=proj&sprint=s1").json()
    assert body["sprint"] == "s1"
    assert body["total_cards"] == 1
    assert _ids(body, "triaged") == ["A"]


def test_unknown_sprint_is_400(rig):
    client, vault, _ = rig
    _sprint_rig(vault)
    r = client.get("/board/api/board?project=proj&sprint=nope")
    assert r.status_code == 400
    assert r.json() == {"error": "unknown sprint: nope"}


def test_unknown_sprint_without_sprints_dir_is_400(rig):
    client, _, _ = rig
    r = client.get("/board/api/board?project=proj&sprint=s9")
    assert r.status_code == 400
    assert r.json() == {"error": "unknown sprint: s9"}


def test_existing_sprint_with_no_items_is_400_like_unknown(rig):
    # Quirk locked as-is: an existing but empty sprint is reported as unknown.
    client, vault, _ = rig
    _card(vault, "A")
    _sprint(vault, "empty", "planned", [])
    r = client.get("/board/api/board?project=proj&sprint=empty")
    assert r.status_code == 400
    assert r.json() == {"error": "unknown sprint: empty"}


def test_active_sprint_with_no_items_filters_out_every_card(rig):
    client, vault, _ = rig
    _card(vault, "A", status="triaged")
    _sprint(vault, "s1", "active", [])
    body = client.get("/board/api/board?project=proj&sprint=active").json()
    assert body["sprint"] == "s1"
    assert body["total_cards"] == 0
