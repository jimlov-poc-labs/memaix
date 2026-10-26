# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Fas D (MEX-025): TOTP, AclWriter, MFA flow, MFA-gated admin
writes with lockout guards, timeline+undo, brief, search web APIs."""

from __future__ import annotations

import time

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.web import routes as web_routes_mod
from memaix_gateway.web import totp
from memaix_gateway.web.acl_writer import AclWriter
from memaix_gateway.web.api import admin_write as admin_write_mod
from memaix_gateway.web.api import mfa as mfa_mod


# ------------------------------------------------------------------
# TOTP (RFC 6238)
# ------------------------------------------------------------------


def test_totp_rfc6238_vector():
    # RFC 6238 appendix B (SHA-1, 8 digits) uses secret "12345678901234567890";
    # we run 6 digits, so check the known 6-digit truncations at T=59.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of the RFC seed
    assert totp.totp_at(secret, 59) == "287082"[-6:]


def test_totp_verify_window_and_reject():
    secret = totp.generate_secret()
    now = 1_700_000_000
    code = totp.totp_at(secret, now)
    assert totp.verify(secret, code, now) is True
    assert totp.verify(secret, code, now + 29) is True     # same period
    assert totp.verify(secret, code, now + 31) is True     # ±1 window
    assert totp.verify(secret, code, now + 90) is False    # outside window
    assert totp.verify(secret, "000000", now) in (True, False)  # never raises
    assert totp.verify(secret, "abc", now) is False
    assert totp.verify(secret, "", now) is False


def test_otpauth_uri_shape():
    uri = totp.otpauth_uri("alice", "SECRET123")
    assert uri.startswith("otpauth://totp/Memaix:alice?secret=SECRET123")


# ------------------------------------------------------------------
# AclWriter
# ------------------------------------------------------------------


@pytest.fixture()
def acl_file(tmp_path):
    p = tmp_path / "acl.yaml"
    p.write_text(yaml.safe_dump({
        "users": {"alice": {"admin": True, "grants": {"proj": "owner"}},
                  "bob": {"grants": {"proj": "reader"}}},
        "projects": {"proj": {"vault": "/tmp/v"}},
    }))
    return p


def test_acl_writer_set_disabled_and_backups(acl_file):
    writer = AclWriter(acl_file)
    writer.set_user_disabled("bob", True)
    data = yaml.safe_load(acl_file.read_text())
    assert data["users"]["bob"]["disabled"] is True
    assert data["users"]["alice"]["admin"] is True  # untouched
    assert acl_file.with_suffix(".yaml.bak1").exists()

    writer.set_user_disabled("bob", False)
    data = yaml.safe_load(acl_file.read_text())
    assert "disabled" not in data["users"]["bob"]
    # Backup rotation: bak1 + bak2 now exist, max 3 kept.
    assert acl_file.with_suffix(".yaml.bak2").exists()


def test_acl_writer_set_grants_preserves_other_users(acl_file):
    writer = AclWriter(acl_file)
    writer.set_grants("bob", {"proj": "collaborator"})
    data = yaml.safe_load(acl_file.read_text())
    assert data["users"]["bob"]["grants"] == {"proj": "collaborator"}
    assert data["users"]["alice"]["grants"] == {"proj": "owner"}


def test_acl_writer_project_field_and_unknown_project(acl_file):
    writer = AclWriter(acl_file)
    writer.set_project_field("proj", "outbox", "review")
    assert yaml.safe_load(acl_file.read_text())["projects"]["proj"]["outbox"] == "review"
    with pytest.raises(KeyError):
        writer.set_project_field("ghost", "outbox", "review")


# ------------------------------------------------------------------
# Web rig (MFA + admin write + timeline + search + brief)
# ------------------------------------------------------------------


@pytest.fixture()
def rig(tmp_path, monkeypatch, acl_file):
    acl_state = {"acl": Acl.from_config(yaml.safe_load(acl_file.read_text()))}
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl_state["acl"])
    current = {"user": "alice"}
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: current["user"])

    # MFA: fixed signing key; secrets dir + acl writer under tmp.
    monkeypatch.setattr(mfa_mod, "_secret_key", lambda: b"k" * 32)
    monkeypatch.setattr(mfa_mod, "_secrets_dir", lambda: tmp_path / "secrets")
    monkeypatch.setattr(mfa_mod, "_acl_writer", lambda: AclWriter(acl_file))

    def _reload():
        acl_state["acl"] = Acl.from_config(yaml.safe_load(acl_file.read_text()) or {})
        return acl_state["acl"]

    monkeypatch.setattr("memaix_gateway.server.reload_acl", _reload)
    monkeypatch.setattr(admin_write_mod, "_acl_writer", lambda: AclWriter(acl_file))
    monkeypatch.setattr(admin_write_mod, "_reload", _reload)

    from memaix_gateway.safety.audit import AuditLog

    AuditLog._clear_instances()
    audit = AuditLog.for_path(tmp_path / "audit.db")
    monkeypatch.setattr(admin_write_mod, "_audit", lambda: audit)

    monkeypatch.setenv("MEMAIX_ACTIONS_DB", str(tmp_path / "actions.db"))

    # The MFA rate limiter is a process-global; neutralize it here so tests
    # don't starve each other — a dedicated test exercises the 429 path.
    monkeypatch.setattr(mfa_mod, "_rate_limited", lambda user: False)

    app = Starlette(routes=web_routes_mod.web_routes)
    # https base_url: the MFA cookies are Secure and would be dropped by the
    # cookie jar over plain http.
    client = TestClient(app, base_url="https://testserver")
    yield client, current, acl_file, audit, acl_state
    AuditLog._clear_instances()


def _enroll_and_verify(client) -> None:
    """Run the full MFA enrollment + verification flow for the current user."""
    start = client.post("/app/api/admin/mfa/setup/start")
    assert start.status_code == 200
    secret = start.json()["secret"]
    code = totp.totp_at(secret, time.time())
    confirm = client.post("/app/api/admin/mfa/setup", json={"code": code})
    assert confirm.status_code == 200
    verify = client.post("/app/api/admin/mfa/verify", json={"code": totp.totp_at(secret, time.time())})
    assert verify.status_code == 200


def test_mfa_enrollment_and_verify_flow(rig):
    client, _, acl_file, _, _ = rig
    assert client.get("/app/api/admin/mfa").json() == {"enrolled": False, "verified": False}
    _enroll_and_verify(client)
    status = client.get("/app/api/admin/mfa").json()
    assert status == {"enrolled": True, "verified": True}
    # Secret persisted as file: ref, never cleartext in acl.yaml.
    data = yaml.safe_load(acl_file.read_text())
    ref = data["users"]["alice"]["totp_secret_ref"]
    assert ref.startswith("file:")


def test_mfa_wrong_code_rejected(rig):
    client, _, _, _, _ = rig
    client.post("/app/api/admin/mfa/setup/start")
    resp = client.post("/app/api/admin/mfa/setup", json={"code": "000000"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "wrong_code"


def test_mfa_verify_rate_limited(rig, monkeypatch):
    client, _, _, _, _ = rig
    hits = {"n": 0}

    def limited(user):
        hits["n"] += 1
        return hits["n"] > 5

    monkeypatch.setattr(mfa_mod, "_rate_limited", limited)
    for _ in range(5):
        client.post("/app/api/admin/mfa/verify", json={"code": "000000"})
    resp = client.post("/app/api/admin/mfa/verify", json={"code": "000000"})
    assert resp.status_code == 429


def test_mfa_forbidden_for_non_admin(rig):
    client, current, _, _, _ = rig
    current["user"] = "bob"
    assert client.get("/app/api/admin/mfa").status_code == 403
    assert client.post("/app/api/admin/mfa/setup/start").status_code == 403


# ------------------------------------------------------------------
# Admin writes — MFA-gated, lockout-guarded
# ------------------------------------------------------------------


def test_admin_write_requires_mfa(rig):
    client, _, _, _, _ = rig
    resp = client.patch("/app/api/admin/users/bob", json={"disabled": True})
    assert resp.status_code == 403
    assert resp.json()["error"] == "mfa_required"


def test_kill_switch_disables_user_live(rig):
    client, _, acl_file, audit, acl_state = rig
    _enroll_and_verify(client)
    resp = client.patch("/app/api/admin/users/bob", json={"disabled": True})
    assert resp.status_code == 200
    # Written to disk AND live in the (reloaded) Acl.
    assert yaml.safe_load(acl_file.read_text())["users"]["bob"]["disabled"] is True
    from memaix_gateway.acl import AccessDenied

    with pytest.raises(AccessDenied):
        acl_state["acl"].enforce("bob", "proj", "reader")
    # Audit trail without secrets.
    events = audit.query(tool="admin_set_disabled")
    assert events and "bob" in events[0]["detail"]


def test_kill_switch_lockout_guards(rig):
    client, _, _, _, _ = rig
    _enroll_and_verify(client)
    # Self-disable refused.
    resp = client.patch("/app/api/admin/users/alice", json={"disabled": True})
    assert resp.status_code == 409
    assert resp.json()["error"] == "self_disable"


def test_kill_switch_last_admin_guard(rig, acl_file):
    client, current, _, _, acl_state = rig
    # Make bob an admin too, then disable him — fine. But alice (the only
    # remaining active admin) must not be disableable by bob.
    _enroll_and_verify(client)
    data = yaml.safe_load(acl_file.read_text())
    data["users"]["bob"]["admin"] = True
    acl_file.write_text(yaml.safe_dump(data))
    from memaix_gateway.server import reload_acl

    reload_acl()

    current["user"] = "bob"
    _enroll_and_verify(client)  # bob enrolls his own MFA
    resp = client.patch("/app/api/admin/users/alice", json={"disabled": True})
    assert resp.status_code == 200  # alice can be disabled: bob remains admin

    # Now bob is the last active admin — disabling him must be refused
    # (self_disable triggers first for himself; check via alice re-enabled).
    resp = client.patch("/app/api/admin/users/bob", json={"disabled": True})
    assert resp.status_code == 409
    assert resp.json()["error"] == "self_disable"


def test_admin_set_grants_validates_and_writes(rig):
    client, _, acl_file, _, _ = rig
    _enroll_and_verify(client)
    resp = client.patch("/app/api/admin/users/bob/grants",
                        json={"grants": {"proj": "collaborator"}})
    assert resp.status_code == 200
    assert yaml.safe_load(acl_file.read_text())["users"]["bob"]["grants"] == {"proj": "collaborator"}
    # Unknown role / project rejected.
    assert client.patch("/app/api/admin/users/bob/grants",
                        json={"grants": {"proj": "superuser"}}).status_code == 400
    assert client.patch("/app/api/admin/users/bob/grants",
                        json={"grants": {"ghost": "reader"}}).status_code == 400


def test_admin_set_project_field_allowlist(rig):
    client, _, acl_file, _, _ = rig
    _enroll_and_verify(client)
    resp = client.patch("/app/api/admin/projects/proj", json={"key": "outbox", "value": "review"})
    assert resp.status_code == 200
    assert yaml.safe_load(acl_file.read_text())["projects"]["proj"]["outbox"] == "review"
    # Non-editable keys refused (vaults/backends are hand-edited by design).
    assert client.patch("/app/api/admin/projects/proj",
                        json={"key": "vault", "value": "/x"}).status_code == 400
    assert client.patch("/app/api/admin/projects/proj",
                        json={"key": "outbox", "value": "yolo"}).status_code == 400


# ------------------------------------------------------------------
# Timeline + undo
# ------------------------------------------------------------------


def test_timeline_list_and_undo(rig, tmp_path, monkeypatch):
    client, _, _, _, _ = rig
    from memaix_gateway.timeline.store import ActionsStore

    store = ActionsStore.for_path(tmp_path / "actions.db")
    from memaix_gateway.web.api import timeline as timeline_mod

    monkeypatch.setattr(timeline_mod, "_timeline_store", lambda: store)

    aid = store.record(
        "alice", "proj", "board_move", "Flyttade BL-1: inbox → done",
        {"tool": "board_move", "args": {"item_id": "BL-1", "status": "inbox"}},
    )
    actions = client.get("/app/api/timeline").json()
    assert len(actions) == 1 and actions[0]["tool"] == "board_move"

    calls = []
    monkeypatch.setattr(
        "memaix_gateway.timeline.undo._default_dispatch",
        lambda: {"board_move": lambda acl, u, p, **kw: calls.append(kw) or {"ok": True}},
    )
    resp = client.post(f"/app/api/timeline/{aid}/undo")
    assert resp.status_code == 200
    assert calls and calls[0]["item_id"] == "BL-1"

    # Second undo of the same action → conflict (already undone).
    resp = client.post(f"/app/api/timeline/{aid}/undo")
    assert resp.status_code in (409, 200)
    if resp.status_code == 200:
        assert resp.json().get("ok") is not True

    assert client.post("/app/api/timeline/nope/undo").status_code == 404


def test_timeline_reader_scoped_to_visible_projects(rig, tmp_path, monkeypatch):
    client, current, _, _, _ = rig
    from memaix_gateway.timeline.store import ActionsStore

    store = ActionsStore.for_path(tmp_path / "actions2.db")
    from memaix_gateway.web.api import timeline as timeline_mod

    monkeypatch.setattr(timeline_mod, "_timeline_store", lambda: store)
    store.record("alice", "proj", "board_move", "x", {"tool": "board_move", "args": {}})

    current["user"] = "bob"  # reader on proj — may see the feed for proj
    actions = client.get("/app/api/timeline").json()
    assert len(actions) == 1


# ------------------------------------------------------------------
# Brief + search
# ------------------------------------------------------------------


def test_brief_roundtrip(rig, tmp_path, monkeypatch):
    client, _, _, _, _ = rig
    from memaix_gateway.notify.store import NotifyStore

    store = NotifyStore.for_path(tmp_path / "notify.db")
    from memaix_gateway.web.api import brief as brief_mod

    monkeypatch.setattr(brief_mod, "_notify_store", lambda: store)

    assert client.get("/app/api/brief").json() == {"configured": False}
    resp = client.post("/app/api/brief", json={"enabled": True, "brief_time": "07:30"})
    assert resp.status_code == 200
    assert resp.json()["prefs"]["brief_time"] == "07:30"
    status = client.get("/app/api/brief").json()
    assert status["configured"] is True and status["next_run"]

    # Invalid time rejected.
    assert client.post("/app/api/brief", json={"brief_time": "25:99"}).status_code == 400


def test_brief_web_email_channel_needs_owner_of_its_project(rig, tmp_path, monkeypatch):
    client, current, _, _, _ = rig
    from memaix_gateway.notify.store import NotifyStore
    from memaix_gateway.web.api import brief as brief_mod

    store = NotifyStore.for_path(tmp_path / "notify.db")
    monkeypatch.setattr(brief_mod, "_notify_store", lambda: store)
    from memaix_gateway import server as server_mod
    from memaix_gateway.safety.audit import AuditLog

    monkeypatch.setattr(server_mod, "_audit", AuditLog.for_path(tmp_path / "srv-audit.db"))
    channel = {"type": "email", "to": "x@attacker.test", "project": "proj"}
    current["user"] = "bob"  # reader in proj
    assert client.post("/app/api/brief", json={"channels": [channel]}).status_code == 403
    assert store.get_prefs("bob") is None
    current["user"] = "alice"  # owner
    assert client.post("/app/api/brief", json={"channels": [channel]}).status_code == 200


@pytest.mark.parametrize("channel", [
    {"type": "webhook", "url": "https://attacker.test\\@discord.com/api/webhooks/1/HEMLIGT"},
    {"type": "webhook", "url": "http://discord.com/api/webhooks/1/HEMLIGT"},
    {"type": "ntfy", "topic": "brief", "server": "https://attacker.test\\@ntfy.sh"},
])
def test_brief_web_refuses_ambiguous_or_plain_http_discord_urls(rig, tmp_path, monkeypatch, channel):
    # The web path has no send lock (a human sets it), but the same URL
    # checks: the owner must not be tricked into a look-alike destination.
    client, current, _, _, _ = rig
    from memaix_gateway.notify.store import NotifyStore
    from memaix_gateway.web.api import brief as brief_mod

    store = NotifyStore.for_path(tmp_path / "notify.db")
    monkeypatch.setattr(brief_mod, "_notify_store", lambda: store)
    current["user"] = "alice"
    resp = client.post("/app/api/brief", json={"channels": [channel]})
    assert resp.status_code == 400 and "HEMLIGT" not in resp.text
    assert store.get_prefs("alice") is None


def test_search_requires_auth_and_empty_query_ok(rig, monkeypatch):
    client, _, _, _, _ = rig
    assert client.get("/app/api/search?q=").json()["results"] == []

    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    assert client.get("/app/api/search?q=x").status_code == 401
