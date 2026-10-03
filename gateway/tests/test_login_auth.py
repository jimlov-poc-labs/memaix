# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Hydra login app's password logic (login-app/auth.py, MEX-019).

The security-critical property: because the app mints an OAuth identity with
subject=username, a shared password must NOT be able to authenticate as an
arbitrary allowed user (impersonation). The shared hash is honoured only in the
single-user case; otherwise a per-user hash (env or acl.yaml) is required.

auth.py is dependency-light (stdlib + yaml) precisely so it can be tested here
without the FastAPI web stack the app module pulls in.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

# Load login-app/auth.py by path — it's not part of the installed package.
_AUTH_PATH = Path(__file__).resolve().parents[2] / "login-app" / "auth.py"
_spec = importlib.util.spec_from_file_location("memaix_login_auth", _AUTH_PATH)
assert _spec and _spec.loader
auth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auth)


def _hash(password: str, salt: bytes = b"\x00" * 16) -> str:
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}:{derived.hex()}"


# ---------------------------------------------------------------------------
# pbkdf2_check
# ---------------------------------------------------------------------------


def test_pbkdf2_check_accepts_correct_and_rejects_wrong():
    h = _hash("s3cret")
    assert auth.pbkdf2_check("s3cret", h) is True
    assert auth.pbkdf2_check("wrong", h) is False


def test_pbkdf2_check_rejects_malformed_hash():
    assert auth.pbkdf2_check("x", "") is False
    assert auth.pbkdf2_check("x", "no-colon") is False
    assert auth.pbkdf2_check("x", "nothex:nothex") is False


# ---------------------------------------------------------------------------
# password_hash_for — the impersonation defence
# ---------------------------------------------------------------------------


def test_shared_hash_ignored_for_multiuser():
    """With more than one allowed user, the shared hash must NOT resolve for a
    user who has no per-user hash — this is the impersonation guard."""
    resolved = auth.password_hash_for(
        "bob",
        allowed_users={"alice", "bob"},
        per_user_hashes={},
        shared_hash=_hash("shared"),
        env={},
    )
    assert resolved == ""


def test_shared_hash_used_for_single_user():
    shared = _hash("shared")
    resolved = auth.password_hash_for(
        "alice",
        allowed_users={"alice"},
        per_user_hashes={},
        shared_hash=shared,
        env={},
    )
    assert resolved == shared


def test_per_user_acl_hash_takes_precedence_over_shared():
    per_user = _hash("alicepw")
    resolved = auth.password_hash_for(
        "alice",
        allowed_users={"alice", "bob"},
        per_user_hashes={"alice": per_user},
        shared_hash=_hash("shared"),
        env={},
    )
    assert resolved == per_user


def test_env_per_user_hash_takes_precedence_over_acl():
    env_hash = _hash("fromenv")
    resolved = auth.password_hash_for(
        "alice",
        allowed_users={"alice", "bob"},
        per_user_hashes={"alice": _hash("fromacl")},
        shared_hash="",
        env={"MEMAIX_LOGIN_PASSWORD_HASH_ALICE": env_hash},
    )
    assert resolved == env_hash


def test_verify_password_no_impersonation_end_to_end():
    """Alice's password must not authenticate Bob in a multi-user setup."""
    alice_hash = _hash("alicepw")
    common = dict(
        allowed_users={"alice", "bob"},
        per_user_hashes={"alice": alice_hash},
        shared_hash=_hash("sharedpw"),
        env={},
    )
    # Alice logs in as herself: OK.
    assert auth.verify_password("alice", "alicepw", **common) is True
    # Bob has no per-user hash; the shared password must NOT let him in.
    assert auth.verify_password("bob", "sharedpw", **common) is False
    assert auth.verify_password("bob", "alicepw", **common) is False


# ---------------------------------------------------------------------------
# load_per_user_hashes
# ---------------------------------------------------------------------------


def test_load_per_user_hashes_from_acl(tmp_path):
    acl_yaml = tmp_path / "acl.yaml"
    acl_yaml.write_text(
        "users:\n"
        "  alice:\n"
        "    password_hash: 'aa:bb'\n"
        "  bob:\n"
        "    grants: {acme: owner}\n"  # no password_hash → not included
    )
    hashes = auth.load_per_user_hashes(str(acl_yaml))
    assert hashes == {"alice": "aa:bb"}


def test_load_per_user_hashes_missing_file_returns_empty(tmp_path):
    assert auth.load_per_user_hashes(str(tmp_path / "nope.yaml")) == {}


# --- AclLoginState: invited users can sign in without a restart ------------


def _write_acl(path, users):
    import yaml

    path.write_text(yaml.safe_dump({"users": users}))


def test_acl_login_state_picks_up_new_user_without_restart(tmp_path):
    p = tmp_path / "acl.yaml"
    _write_acl(p, {"a": {"password_hash": "s:h"}, "pending": {"grants": {}}})
    st = auth.AclLoginState(str(p))
    st.refresh()
    assert st.enabled == {"a"} and "pending" not in st.hashes
    _write_acl(p, {"a": {"password_hash": "s:h"}, "pending": {"password_hash": "s2:h2"}})
    os.utime(p, ns=(1, 2_000_000_000_000_000_000))
    st.refresh()
    assert st.enabled == {"a", "pending"} and st.hashes["pending"] == "s2:h2"


def test_acl_login_state_excludes_disabled_users(tmp_path):
    p = tmp_path / "acl.yaml"
    _write_acl(p, {"a": {"password_hash": "s:h", "disabled": True}})
    st = auth.AclLoginState(str(p))
    st.refresh()
    assert st.enabled == set()


def test_acl_login_state_keeps_last_good_state_on_bad_file(tmp_path):
    p = tmp_path / "acl.yaml"
    _write_acl(p, {"a": {"password_hash": "s:h"}})
    st = auth.AclLoginState(str(p))
    st.refresh()
    p.write_text("users: [unterminated")
    os.utime(p, ns=(1, 3_000_000_000_000_000_000))
    st.refresh()
    assert st.enabled == {"a"}
