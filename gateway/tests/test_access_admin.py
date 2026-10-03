# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behörighetsregler för projektskapande, medlemmar och inbjudningar.

Det här är åtkomstkontroll: varje regel som skyddar mot eskalering har ett
test som bevisar nekandet, inte bara att det lyckade fallet fungerar.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from memaix_gateway import gitvault
from memaix_gateway.access_admin import AccessAdmin, AccessError
from memaix_gateway.acl import Acl, AccessDenied
from memaix_gateway.cli import hash_password
from memaix_gateway.invites import InviteStore
from memaix_gateway.web.acl_writer import AclWriter

_BASE = {
    "users": {
        "root": {"admin": True, "grants": {"acme": "owner", "beta": "owner"}},
        "alice": {"grants": {"acme": "owner"}},
        "bob": {"grants": {"acme": "collaborator"}},
        "carol": {"grants": {"acme": "reader"}},
        "dave": {"grants": {"beta": "owner"}, "password_hash": "aa:bb"},
    },
    "projects": {
        "acme": {"vault": "/srv/vaults/acme", "allow_send": False},
        "beta": {"vault": "/srv/vaults/beta", "allow_send": False},
    },
}


@pytest.fixture()
def env(tmp_path: Path):
    acl_path = tmp_path / "acl.yaml"
    acl_path.write_text(yaml.safe_dump(_BASE), encoding="utf-8")
    vaults = tmp_path / "vaults"
    vaults.mkdir()
    writer = AclWriter(acl_path)
    invites = InviteStore(tmp_path / "invites.db")

    class Env:
        pass

    e = Env()
    e.path = acl_path
    e.vaults = vaults
    e.invites = invites
    e.admin = AccessAdmin(writer, invites, vaults)

    def acl() -> Acl:
        return Acl.from_config(yaml.safe_load(acl_path.read_text(encoding="utf-8")))

    e.acl = acl
    return e


# ---------------------------------------------------------------- projekt


def test_non_admin_cannot_create_project(env):
    for user in ("alice", "bob", "carol", "dave"):
        with pytest.raises(AccessDenied):
            env.admin.create_project(env.acl(), user, "newproj")
    assert "newproj" not in env.acl().projects


def test_admin_creates_project_with_vault_and_owner_grant(env):
    out = env.admin.create_project(env.acl(), "root", "itsq")
    acl = env.acl()
    assert out["project"] == "itsq"
    assert acl.projects["itsq"]["vault"] == str(env.vaults / "itsq")
    assert acl.projects["itsq"]["allow_send"] is False
    assert acl.grants("root")["itsq"] == "owner"
    assert gitvault.is_repo(env.vaults / "itsq")
    assert (env.vaults / "itsq" / "memory").is_dir()
    assert (env.vaults / "itsq" / "backlog").is_dir()


@pytest.mark.parametrize(
    "name", ["", "A", "x", "Itsq", "with space", "../etc", "a/b", "-lead", "x" * 40, "ünder"],
)
def test_invalid_project_names_rejected(env, name):
    with pytest.raises(AccessError):
        env.admin.create_project(env.acl(), "root", name)


def test_duplicate_project_rejected_and_vault_untouched(env):
    with pytest.raises(AccessError):
        env.admin.create_project(env.acl(), "root", "acme")


def test_existing_vault_dir_is_never_reused(env):
    (env.vaults / "taken").mkdir()
    (env.vaults / "taken" / "secret.md").write_text("x")
    with pytest.raises(AccessError):
        env.admin.create_project(env.acl(), "root", "taken")
    assert (env.vaults / "taken" / "secret.md").read_text() == "x"
    assert "taken" not in env.acl().projects


def test_disabled_admin_cannot_create_project(env):
    data = yaml.safe_load(env.path.read_text())
    data["users"]["root"]["disabled"] = True
    env.path.write_text(yaml.safe_dump(data))
    with pytest.raises(AccessDenied):
        env.admin.create_project(env.acl(), "root", "newproj")


# --------------------------------------------------------------- medlemmar


def test_project_owner_can_change_roles_in_own_project(env):
    env.admin.set_member(env.acl(), "alice", "acme", "bob", "reader")
    assert env.acl().grants("bob")["acme"] == "reader"
    env.admin.set_member(env.acl(), "alice", "acme", "bob", "owner")
    assert env.acl().grants("bob")["acme"] == "owner"


def test_project_owner_can_remove_member(env):
    env.admin.set_member(env.acl(), "alice", "acme", "bob", None)
    assert "acme" not in env.acl().grants("bob")


@pytest.mark.parametrize("actor", ["bob", "carol"])
def test_non_owner_cannot_manage_members(env, actor):
    with pytest.raises(AccessDenied):
        env.admin.set_member(env.acl(), actor, "acme", "carol", "owner")
    assert env.acl().grants("carol")["acme"] == "reader"


def test_owner_of_one_project_cannot_touch_another(env):
    with pytest.raises(AccessDenied):
        env.admin.set_member(env.acl(), "alice", "beta", "bob", "owner")
    assert "beta" not in env.acl().grants("bob")
    with pytest.raises(AccessDenied):
        env.admin.invite(env.acl(), "alice", "beta", "newbie", "reader")
    assert "newbie" not in env.acl().users


def test_change_leaves_grants_in_other_projects_alone(env):
    env.admin.set_member(env.acl(), "alice", "acme", "dave", "reader")
    assert env.acl().grants("dave") == {"beta": "owner", "acme": "reader"}


def test_cannot_alter_global_admin(env):
    with pytest.raises(AccessError):
        env.admin.set_member(env.acl(), "alice", "acme", "root", "reader")
    assert env.acl().grants("root")["acme"] == "owner"


def test_unknown_role_and_unknown_user_and_project_rejected(env):
    with pytest.raises(AccessError):
        env.admin.set_member(env.acl(), "alice", "acme", "bob", "admin")
    with pytest.raises(AccessError):
        env.admin.set_member(env.acl(), "alice", "acme", "ghost", "reader")
    with pytest.raises(AccessError):
        env.admin.set_member(env.acl(), "root", "nope", "bob", "reader")


def test_last_non_admin_owner_cannot_lock_themselves_out(env):
    with pytest.raises(AccessError):
        env.admin.set_member(env.acl(), "alice", "acme", "alice", "collaborator")
    with pytest.raises(AccessError):
        env.admin.set_member(env.acl(), "alice", "acme", "alice", None)
    assert env.acl().grants("alice")["acme"] == "owner"


def test_system_admin_may_demote_last_project_owner(env):
    env.admin.set_member(env.acl(), "root", "acme", "alice", "reader")
    assert env.acl().grants("alice")["acme"] == "reader"


def test_owner_can_step_down_when_another_owner_exists(env):
    env.admin.set_member(env.acl(), "alice", "acme", "bob", "owner")
    env.admin.set_member(env.acl(), "alice", "acme", "alice", "collaborator")
    assert env.acl().grants("alice")["acme"] == "collaborator"


def test_global_admin_can_manage_any_project(env):
    env.admin.set_member(env.acl(), "root", "acme", "carol", "collaborator")
    assert env.acl().grants("carol")["acme"] == "collaborator"


def test_disabled_owner_cannot_manage(env):
    data = yaml.safe_load(env.path.read_text())
    data["users"]["alice"]["disabled"] = True
    env.path.write_text(yaml.safe_dump(data))
    with pytest.raises(AccessDenied):
        env.admin.set_member(env.acl(), "alice", "acme", "bob", "reader")


def test_list_members_requires_owner(env):
    members = env.admin.list_members(env.acl(), "alice", "acme")
    assert {m["user"]: m["role"] for m in members} == {
        "root": "owner", "alice": "owner", "bob": "collaborator", "carol": "reader",
    }
    with pytest.raises(AccessDenied):
        env.admin.list_members(env.acl(), "bob", "acme")


# -------------------------------------------------------------- inbjudningar


def test_invite_creates_pending_user_limited_to_one_project(env):
    out = env.admin.invite(
        env.acl(), "root", "acme", "newbie", "owner", email="n@example.com",
    )
    u = env.acl().users["newbie"]
    assert u["grants"] == {"acme": "owner"}
    assert u["email"] == "n@example.com"
    assert "password_hash" not in u
    assert u.get("admin") is not True
    assert out["status"] == "invited"
    assert out["token"] and len(out["token"]) >= 40


def test_invite_for_existing_user_with_password_just_grants(env):
    out = env.admin.invite(env.acl(), "alice", "acme", "dave", "collaborator")
    assert out["status"] == "granted"
    assert "token" not in out
    assert env.acl().grants("dave")["acme"] == "collaborator"


def test_invite_for_existing_admin_is_refused(env):
    with pytest.raises(AccessError):
        env.admin.invite(env.acl(), "alice", "acme", "root", "reader")


@pytest.mark.parametrize("uid", ["admin", "root", "A", "x", "a b", "../x", "x" * 40])
def test_invite_rejects_bad_or_reserved_user_ids(env, uid):
    with pytest.raises(AccessError):
        env.admin.invite(env.acl(), "alice", "acme", uid, "reader")


def test_reinvite_before_acceptance_replaces_old_token(env):
    first = env.admin.invite(env.acl(), "alice", "acme", "newbie", "reader")
    second = env.admin.invite(env.acl(), "alice", "acme", "newbie", "reader")
    assert first["token"] != second["token"]
    assert env.invites.peek(first["token"]) is None
    assert env.invites.peek(second["token"]) is not None


def test_accept_sets_password_hash_once(env):
    token = env.admin.invite(env.acl(), "alice", "acme", "newbie", "reader")["token"]
    user = env.admin.accept_invite(token, "correct horse battery")
    assert user == "newbie"
    stored = env.acl().users["newbie"]["password_hash"]
    salt, _ = stored.split(":")
    assert stored == hash_password("correct horse battery", salt=bytes.fromhex(salt))
    with pytest.raises(AccessError):
        env.admin.accept_invite(token, "another password 123")
    assert env.acl().users["newbie"]["password_hash"] == stored


def test_accept_rejects_short_password_and_keeps_token_usable(env):
    token = env.admin.invite(env.acl(), "alice", "acme", "newbie", "reader")["token"]
    with pytest.raises(AccessError):
        env.admin.accept_invite(token, "short")
    assert "password_hash" not in env.acl().users["newbie"]
    assert env.admin.accept_invite(token, "long enough password") == "newbie"


def test_accept_rejects_unknown_and_expired_tokens(env):
    with pytest.raises(AccessError):
        env.admin.accept_invite("nope", "long enough password")
    token = env.admin.invite(
        env.acl(), "alice", "acme", "newbie", "reader", ttl_s=1,
    )["token"]
    time.sleep(1.2)
    with pytest.raises(AccessError):
        env.admin.accept_invite(token, "long enough password")
    assert "password_hash" not in env.acl().users["newbie"]


def test_token_is_not_stored_in_clear(env):
    token = env.admin.invite(env.acl(), "alice", "acme", "newbie", "reader")["token"]
    assert token.encode() not in (env.invites._path).read_bytes()


def test_accept_never_overwrites_an_existing_password(env):
    """Ett inbjudningslänk-token får bara sätta lösenord på ett konto som
    saknar ett — annars kan en gammal länk ta över ett levande konto."""
    token = env.admin.invite(env.acl(), "alice", "acme", "newbie", "reader")["token"]
    env.admin.accept_invite(token, "first password here")
    again = env.admin.invite(env.acl(), "alice", "acme", "newbie", "reader")
    assert again["status"] == "granted"
    assert "token" not in again


def test_user_disabled_after_invite_cannot_accept(env):
    token = env.admin.invite(env.acl(), "alice", "acme", "newbie", "reader")["token"]
    data = yaml.safe_load(env.path.read_text())
    data["users"]["newbie"]["disabled"] = True
    env.path.write_text(yaml.safe_dump(data))
    with pytest.raises(AccessError):
        env.admin.accept_invite(token, "long enough password")
