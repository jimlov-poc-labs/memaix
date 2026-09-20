# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Fas B (MVP) web APIs: memory explorer, accounts, calendar
mode — plus the memory_list tool wrapper (FEATURE-WEB-UI-MVP.md).

Same auth-bypass pattern as the other web tests: _require_user is patched;
role behaviour is exercised through the Acl."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.backends.memory_store import MemoryStore
from memaix_gateway.tools.memory import memory_list, memory_write
from memaix_gateway.web import routes as web_routes_mod


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    MemoryStore._clear_instances()

    acl = Acl(
        users={
            "alice": {"grants": {"proj": "owner"}},
            "bob": {"grants": {"proj": "reader"}},
        },
        projects={"proj": {"vault": str(vault)}},
    )
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    current = {"user": "alice"}
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: current["user"])

    app = Starlette(routes=web_routes_mod.web_routes)
    return TestClient(app), acl, current, vault


# ------------------------------------------------------------------
# memory_list tool wrapper
# ------------------------------------------------------------------


def test_memory_list_returns_paths_and_mtime(rig):
    _, acl, _, _ = rig
    memory_write(acl, "alice", "proj", "ideas/x.md", "# Hello")
    memory_write(acl, "alice", "proj", "notes.md", "content")
    notes = memory_list(acl, "alice", "proj")
    paths = {n["path"] for n in notes}
    assert paths == {"ideas/x.md", "notes.md"}
    assert all(n["mtime"] for n in notes)


def test_memory_list_requires_reader(rig):
    from memaix_gateway.acl import AccessDenied

    _, acl, _, _ = rig
    with pytest.raises(AccessDenied):
        memory_list(acl, "mallory", "proj")


# ------------------------------------------------------------------
# Memory web API
# ------------------------------------------------------------------


def test_memory_notes_and_note_roundtrip(rig):
    client, acl, _, _ = rig
    memory_write(acl, "alice", "proj", "a.md", "# Title\n\nbody")
    resp = client.get("/app/api/memory/notes?project=proj")
    assert resp.status_code == 200
    assert resp.json() == [
        {"path": "a.md", "mtime": resp.json()[0]["mtime"], "status": "hypotes"}
    ]

    note = client.get("/app/api/memory/note?project=proj&path=a.md")
    assert note.status_code == 200
    assert note.json()["content"] == "# Title\n\nbody"


def test_memory_note_404_and_traversal_400(rig):
    client, _, _, _ = rig
    assert client.get("/app/api/memory/note?project=proj&path=nope.md").status_code == 404
    assert client.get("/app/api/memory/note?project=proj&path=../etc/passwd").status_code == 400


def test_memory_search(rig):
    client, acl, _, _ = rig
    memory_write(acl, "alice", "proj", "findme.md", "unique zebra content")
    resp = client.get("/app/api/memory/search?project=proj&q=zebra")
    assert resp.status_code == 200
    assert any(h["path"] == "findme.md" for h in resp.json())


def test_memory_history_and_revert_owner_only(rig):
    client, acl, current, _ = rig
    memory_write(acl, "alice", "proj", "doc.md", "v1")
    memory_write(acl, "alice", "proj", "doc.md", "v2")

    hist = client.get("/app/api/memory/history?project=proj&path=doc.md")
    assert hist.status_code == 200
    entries = hist.json()
    assert len(entries) >= 2
    # Revert the newest commit (v2) — cleanly restores v1.
    newest_sha = entries[0]["hash"]

    # Reader must not revert (owner-gated in the web layer).
    current["user"] = "bob"
    resp = client.post("/app/api/memory/revert", json={"project": "proj", "sha": newest_sha})
    assert resp.status_code == 403

    current["user"] = "alice"
    resp = client.post("/app/api/memory/revert", json={"project": "proj", "sha": newest_sha})
    assert resp.status_code == 200
    assert resp.json()["reverted_to"] == newest_sha


def test_memory_api_401_unauthenticated(rig, monkeypatch):
    client, _, _, _ = rig
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    # The api modules resolved _require_user at call time via the routes module.
    from memaix_gateway.web.api import memory as api_memory_mod
    monkeypatch.setattr(api_memory_mod, "_require_user", lambda request: None)
    assert client.get("/app/api/memory/notes?project=proj").status_code == 401


def test_memory_reader_cannot_reach_other_project(rig):
    client, _, current, _ = rig
    current["user"] = "bob"
    # bob is reader on proj — notes list is allowed…
    assert client.get("/app/api/memory/notes?project=proj").status_code == 200
    # …but an unknown project is a 400/403, never a 500.
    resp = client.get("/app/api/memory/notes?project=ghost")
    assert resp.status_code in (400, 403)


# ------------------------------------------------------------------
# Accounts + calendar mode web API
# ------------------------------------------------------------------


class _FakeTokenStore:
    def __init__(self):
        self.records = {}

    def list_accounts(self, user):
        return [
            {"provider": p, "account": a, "status": "active", "scopes": ""}
            for (u, p, a) in self.records
            if u == user
        ]

    def list_scopes(self, user, provider=None, account=None):
        return []

    def store(self, user, provider, account, data):
        self.records[(user, provider, account)] = data

    def delete(self, user, provider, account):
        return self.records.pop((user, provider, account), None) is not None

    def load_one(self, user, provider, account):
        return self.records.get((user, provider, account))


@pytest.fixture()
def accounts_rig(rig, monkeypatch):
    client, acl, current, vault = rig
    store = _FakeTokenStore()
    from memaix_gateway.web.api import accounts as api_accounts_mod
    monkeypatch.setattr(api_accounts_mod, "_token_store", lambda: store)
    monkeypatch.setattr(api_accounts_mod, "_public_url", lambda: "https://mcp.example.com")
    return client, store, current


def test_accounts_list_empty_then_populated(accounts_rig):
    client, store, _ = accounts_rig
    assert client.get("/app/api/accounts").json() == []
    store.store("alice", "google", "alice@gmail.com", {"t": 1})
    accounts = client.get("/app/api/accounts").json()
    assert accounts[0]["provider"] == "google"


def test_accounts_unlink(accounts_rig):
    client, store, _ = accounts_rig
    store.store("alice", "google", "alice@gmail.com", {"t": 1})
    resp = client.delete("/app/api/accounts/google?account=alice@gmail.com")
    assert resp.status_code == 200
    assert client.get("/app/api/accounts").json() == []
    # Unlinking again → 404.
    resp = client.delete("/app/api/accounts/google?account=alice@gmail.com")
    assert resp.status_code == 404


def test_calendar_mode_roundtrip_and_ssrf_guard(accounts_rig):
    client, _, _ = accounts_rig
    # Set iCal mode with a safe URL.
    resp = client.post("/app/api/settings/calendar-mode", json={
        "project": "proj", "mode": "ical_secret",
        "ical_url": "https://calendar.google.com/x.ics",
    })
    assert resp.status_code == 200
    status = client.get("/app/api/settings/calendar-mode?project=proj").json()
    assert status["active_mode"] == "ical_secret"

    # SSRF: internal URL must be rejected with a 400, not stored.
    resp = client.post("/app/api/settings/calendar-mode", json={
        "project": "proj", "mode": "ical_secret",
        "ical_url": "http://169.254.169.254/latest/meta-data/",
    })
    assert resp.status_code == 400


def test_calendar_mode_requires_collaborator(accounts_rig):
    client, _, current = accounts_rig
    current["user"] = "bob"  # reader
    resp = client.post("/app/api/settings/calendar-mode", json={"project": "proj", "mode": "none"})
    assert resp.status_code == 403


# ------------------------------------------------------------------
# CLI hash-password
# ------------------------------------------------------------------


def test_cli_hash_password_matches_login_auth():
    import importlib.util
    from pathlib import Path

    from memaix_gateway.cli import hash_password

    hashed = hash_password("s3cret")
    salt_hex, key_hex = hashed.split(":", 1)
    assert len(bytes.fromhex(salt_hex)) == 16
    assert len(bytes.fromhex(key_hex)) == 32

    # The CLI's output must verify with login-app/auth.py (same format+iterations).
    auth_path = Path(__file__).resolve().parents[2] / "login-app" / "auth.py"
    spec = importlib.util.spec_from_file_location("la_auth", auth_path)
    assert spec and spec.loader
    auth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth)
    assert auth.pbkdf2_check("s3cret", hashed) is True
    assert auth.pbkdf2_check("wrong", hashed) is False
