# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the web-UI app shell routes (FEATURE-WEB-UI-FOUNDATION.md).

Auth is bypassed by monkeypatching _require_user (same pattern as the board
tests) — cookie auth itself is exercised by the board test suite; these tests
cover pages, static serving, the 301 board redirect and /app/api/me.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.web import routes as web_routes_mod


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    acl = Acl(
        users={
            "alice": {"grants": {"acme": "owner", "beta": "reader"}},
            "root": {"admin": True},
        },
        projects={"acme": {"vault": str(tmp_path / "a")}, "beta": {"vault": str(tmp_path / "b")}},
    )
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    current = {"user": "alice"}
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: current["user"])
    monkeypatch.delenv("MEMAIX_TOKEN_DB", raising=False)
    monkeypatch.setenv("MEMAIX_OUTBOX_DB", str(tmp_path / "outbox.db"))

    app = Starlette(routes=web_routes_mod.web_routes)
    return TestClient(app), current


def test_board_redirects_301_preserving_query(rig):
    client, _ = rig
    resp = client.get("/board?project=acme&sprint=active", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["Location"] == "/app/board?project=acme&sprint=active"

    resp = client.get("/board", follow_redirects=False)
    assert resp.headers["Location"] == "/app/board"


def test_app_index_serves_dark_shell(rig):
    client, _ = rig
    resp = client.get("/app")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "sidebar" in resp.text
    assert "window.I18N=" in resp.text  # i18n injected


def test_app_known_page_and_404(rig):
    client, _ = rig
    assert client.get("/app/board").status_code == 200
    assert client.get("/app/nonexistent").status_code == 404


def test_static_css_has_design_tokens(rig):
    client, _ = rig
    resp = client.get("/app/static/app.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]
    # Light is the default theme; dark is an opt-in override on the root.
    assert "--bg:           #fffdf7;" in resp.text
    assert '[data-theme="dark"]' in resp.text
    assert "--sidebar-w:    220px;" in resp.text


def test_static_path_traversal_blocked(rig):
    client, _ = rig
    # Encoded traversal must not escape the static dir.
    resp = client.get("/app/static/..%2f..%2froutes.py")
    assert resp.status_code == 404
    resp = client.get("/app/static/../routes.py")
    assert resp.status_code == 404


def test_api_me_shape(rig):
    client, _ = rig
    resp = client.get("/app/api/me")
    assert resp.status_code == 200
    me = resp.json()
    assert me["user"] == "alice"
    assert me["is_admin"] is False
    assert me["projects"] == ["acme", "beta"]
    assert me["role_map"] == {"acme": "owner", "beta": "reader"}
    assert me["needs_relink"] == []
    assert me["pending_outbox"] == 0
    assert me["onboarding_missing"] is False


def test_api_me_admin_sees_all_projects(rig):
    client, current = rig
    current["user"] = "root"
    me = client.get("/app/api/me").json()
    assert me["is_admin"] is True
    assert me["projects"] == ["acme", "beta"]
    assert me["role_map"] == {"acme": "admin", "beta": "admin"}


def test_api_me_401_when_unauthenticated(rig, monkeypatch):
    client, _ = rig
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    resp = client.get("/app/api/me")
    assert resp.status_code == 401


def test_pages_401_do_not_apply(rig, monkeypatch):
    # Pages themselves render without auth (client JS redirects on 401 from
    # the API) — mirrors the board.html pattern with its inline login card.
    client, _ = rig
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    assert client.get("/app").status_code == 200


def test_board_frame_serves_board_html_with_dark_override(rig):
    client, _ = rig
    resp = client.get("/app/board/frame")
    assert resp.status_code == 200
    assert "--bg:#0f1117" in resp.text  # dark override injected


def test_static_refs_are_versioned_in_pages(rig):
    client, _ = rig
    resp = client.get("/app")
    assert resp.status_code == 200
    import re

    refs = re.findall(r"/app/static/[A-Za-z0-9_.\-/]+(?:\?v=([0-9a-f]+))?", resp.text)
    assert refs, "shell should reference static assets"
    assert all(v for v in refs), f"unversioned static refs found: {resp.text}"


def test_static_cache_headers_versioned_vs_bare(rig):
    client, _ = rig
    versioned = client.get("/app/static/app.js?v=abc123")
    assert versioned.status_code == 200
    assert "immutable" in versioned.headers["Cache-Control"]

    bare = client.get("/app/static/app.js")
    assert bare.status_code == 200
    assert bare.headers["Cache-Control"] == "no-cache"


def _root_redirect_client():
    """BrowserRootRedirect wrapping a stand-in for the MCP app that answers
    401 on everything, mirroring the real mount at "/"."""
    from starlette.responses import JSONResponse as _JR

    async def mcp_stub(scope, receive, send):
        await _JR({"error": "invalid_token"}, status_code=401)(scope, receive, send)

    return TestClient(
        web_routes_mod.BrowserRootRedirect(mcp_stub), raise_server_exceptions=False
    )


def test_root_browser_redirects_to_app():
    client = _root_redirect_client()
    resp = client.get(
        "/", headers={"Accept": "text/html,application/xhtml+xml"}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/app"


def test_root_mcp_clients_unaffected():
    client = _root_redirect_client()
    # Streamable-HTTP GET (SSE): Accept has no text/html
    resp = client.get("/", headers={"Accept": "text/event-stream"}, follow_redirects=False)
    assert resp.status_code == 401
    # Authenticated client keeps 401-from-app even with a browser-ish Accept
    resp = client.get(
        "/",
        headers={"Accept": "text/html", "Authorization": "Bearer x"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    # POST (MCP messages) never redirects
    resp = client.post("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 401
    # Other paths pass through
    resp = client.get("/anything", headers={"Accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 401


def test_api_me_maintenance_none_by_default(rig, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMAIX_DATA_DIR", str(tmp_path / "d"))
    client, _ = rig
    assert client.get("/app/api/me").json()["maintenance"] is None


def test_api_me_maintenance_message_from_file(rig, tmp_path, monkeypatch):
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("MEMAIX_DATA_DIR", str(data))
    (data / "maintenance.json").write_text('{"message": "  Omstart kl 22:00  "}', encoding="utf-8")
    client, _ = rig
    assert client.get("/app/api/me").json()["maintenance"] == {"message": "Omstart kl 22:00"}


@pytest.mark.parametrize("content", ["not json", "[]", '{"message": ""}', '{"message": "   "}'])
def test_api_me_maintenance_bad_or_empty_file_is_ignored(rig, tmp_path, monkeypatch, content):
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("MEMAIX_DATA_DIR", str(data))
    (data / "maintenance.json").write_text(content, encoding="utf-8")
    client, _ = rig
    resp = client.get("/app/api/me")
    assert resp.status_code == 200
    assert resp.json()["maintenance"] is None


def test_api_me_maintenance_message_is_capped(rig, tmp_path, monkeypatch):
    data = tmp_path / "d"
    data.mkdir()
    monkeypatch.setenv("MEMAIX_DATA_DIR", str(data))
    (data / "maintenance.json").write_text('{"message": "' + "x" * 1000 + '"}', encoding="utf-8")
    client, _ = rig
    assert len(client.get("/app/api/me").json()["maintenance"]["message"]) == 300


class _VaultAcl:
    def __init__(self, vault):
        self._vault = vault

    def resource(self, project, kind):
        return str(self._vault) if project == "shared" and self._vault else None


def test_onboarding_missing_reads_shared_vault_only(tmp_path):
    from memaix_gateway.tools.onboarding import complete_onboarding
    from memaix_gateway.web.routes import _onboarding_missing

    acl = _VaultAcl(tmp_path)
    assert _onboarding_missing(acl, "anna", ["acme", "shared"]) is True
    complete_onboarding("anna", tmp_path, "Profil")
    assert _onboarding_missing(acl, "anna", ["acme", "shared"]) is False
    assert _onboarding_missing(acl, "bertil", ["acme", "shared"]) is True


def test_onboarding_missing_false_without_shared_grant_or_vault(tmp_path):
    from memaix_gateway.web.routes import _onboarding_missing

    assert _onboarding_missing(_VaultAcl(tmp_path), "anna", ["acme"]) is False
    assert _onboarding_missing(_VaultAcl(None), "anna", ["shared"]) is False


def test_privacy_policy_matches_what_the_service_does(rig):
    client, _ = rig
    html = client.get("/privacy").text
    assert "calendar.events" in html and "calendar.calendarlist.readonly" in html
    assert "Limited Use" in html
    assert "Jimmy Lovén" in html and "Lövgren" not in html
    # Gmail API access is no longer requested, so the policy must not claim it.
    assert "read and compose emails" not in html
    assert "never persisted" not in html and "No email content" not in html
    terms = client.get("/terms").text
    assert "Lövgren" not in terms
    assert "personal use only" not in terms
    assert "private service" not in terms
    assert "/privacy" in terms
