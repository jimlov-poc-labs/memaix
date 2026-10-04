# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public invite page: single-use link, user picks own password."""

from __future__ import annotations

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway import server
from memaix_gateway.access_admin import AccessAdmin
from memaix_gateway.acl import Acl
from memaix_gateway.invites import InviteStore
from memaix_gateway.tools import access as t_access
from memaix_gateway.web import invite as invite_mod
from memaix_gateway.web.acl_writer import AclWriter
from memaix_gateway.web.routes import web_routes


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    path = tmp_path / "acl.yaml"
    path.write_text(yaml.safe_dump({
        "users": {"alice": {"grants": {"acme": "owner"}}},
        "projects": {"acme": {"vault": str(tmp_path / "v"), "allow_send": False}},
    }))
    admin = AccessAdmin(AclWriter(path), InviteStore(tmp_path / "i.db"), tmp_path)
    monkeypatch.setattr(t_access, "_admin", lambda: admin)
    monkeypatch.setattr(server, "reload_acl", lambda: None)
    acl = Acl.from_config(yaml.safe_load(path.read_text()))
    token = admin.invite(acl, "alice", "acme", "newbie", "reader")["token"]
    client = TestClient(Starlette(routes=list(web_routes)))
    return client, token, path


def test_get_shows_form_without_leaking(rig):
    client, token, _ = rig
    r = client.get(f"/app/invite/{token}")
    assert r.status_code == 200 and "newbie" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_unknown_token_is_404(rig):
    client, _, _ = rig
    assert client.get("/app/invite/nope").status_code == 404
    assert client.post("/app/invite/nope", data={"password": "x" * 14, "confirm": "x" * 14}).status_code == 404


def test_mismatch_and_short_password_rejected_token_survives(rig):
    client, token, path = rig
    r = client.post(f"/app/invite/{token}", data={"password": "a" * 14, "confirm": "b" * 14})
    assert r.status_code == 400
    r = client.post(f"/app/invite/{token}", data={"password": "short", "confirm": "short"})
    assert r.status_code == 400
    assert "password_hash" not in yaml.safe_load(path.read_text())["users"]["newbie"]


def test_success_sets_hash_and_link_is_single_use(rig):
    client, token, path = rig
    pw = "a very good password"
    assert client.post(f"/app/invite/{token}", data={"password": pw, "confirm": pw}).status_code == 200
    assert "password_hash" in yaml.safe_load(path.read_text())["users"]["newbie"]
    assert client.get(f"/app/invite/{token}").status_code == 404
    assert client.post(f"/app/invite/{token}", data={"password": pw, "confirm": pw}).status_code == 404


def test_done_page_shows_connector_url_and_app_link(rig, monkeypatch):
    client, token, _ = rig
    monkeypatch.setattr(invite_mod, "_public_url", lambda: "https://mcp.example")
    pw = "a very good password"
    r = client.post(f"/app/invite/{token}", data={"password": pw, "confirm": pw})
    assert "https://mcp.example" in r.text
    assert 'href="https://mcp.example/app/"' in r.text


def test_done_page_without_public_url_says_to_ask(rig, monkeypatch):
    client, token, _ = rig
    monkeypatch.setattr(invite_mod, "_public_url", lambda: "")
    pw = "a very good password"
    r = client.post(f"/app/invite/{token}", data={"password": pw, "confirm": pw})
    assert r.status_code == 200
    assert "connector-URL" in r.text
