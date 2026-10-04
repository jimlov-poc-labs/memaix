# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the board's session-auth hardening: fail-closed on the default
signing secret in HTTP mode, and per-user password binding so a shared
password can't authenticate as a different user."""

from __future__ import annotations

import hashlib
import importlib

import pytest

import memaix_gateway.board.routes as routes


def _hash(password: str, salt: bytes = b"\x01" * 16) -> str:
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}:{derived.hex()}"


@pytest.fixture()
def reload_routes(monkeypatch):
    """routes.py reads _ALLOWED_USERS / _PASSWORD_HASH at import time, so tests
    that change them must reimport the module after setting env."""
    def _reload(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        return importlib.reload(routes)
    yield _reload
    importlib.reload(routes)  # restore defaults for other tests


# ------------------------------------------------------------------
# Fail-closed on default secret in HTTP mode
# ------------------------------------------------------------------


def test_default_secret_disables_board_in_http_mode(reload_routes):
    r = reload_routes(MEMAIX_TRANSPORT="http", HYDRA_SYSTEM_SECRET=None, MEMAIX_ALLOW_DEV_SECRET=None)
    with pytest.raises(r.BoardDisabled):
        r._secret()


def test_default_secret_allowed_in_stdio_mode(reload_routes):
    r = reload_routes(MEMAIX_TRANSPORT=None, HYDRA_SYSTEM_SECRET=None)
    assert r._secret()  # stdio/dev — default secret is acceptable


def test_default_secret_allowed_with_explicit_optin(reload_routes):
    r = reload_routes(MEMAIX_TRANSPORT="http", HYDRA_SYSTEM_SECRET=None, MEMAIX_ALLOW_DEV_SECRET="1")
    assert r._secret()


def test_real_secret_enables_board_in_http_mode(reload_routes):
    r = reload_routes(MEMAIX_TRANSPORT="http", HYDRA_SYSTEM_SECRET="a-real-32-byte-long-secret-value!")
    assert r._secret()


def test_disabled_board_rejects_cookies(reload_routes):
    r = reload_routes(MEMAIX_TRANSPORT="http", HYDRA_SYSTEM_SECRET=None, MEMAIX_ALLOW_DEV_SECRET=None)

    class _Req:
        cookies = {"memaix_board": "alice:20000:deadbeef"}

    assert r._check_cookie(_Req()) is None  # fail closed, no 500


# ------------------------------------------------------------------
# Per-user password binding
# ------------------------------------------------------------------


def test_shared_password_only_works_for_single_allowed_user(reload_routes):
    r = reload_routes(MEMAIX_ALLOWED_USERS="alice", MEMAIX_LOGIN_PASSWORD_HASH=_hash("pw"))
    assert r._verify_password("alice", "pw") is True


def test_shared_password_rejected_when_multiple_users(reload_routes):
    # With >1 allowed user, the shared hash must NOT authenticate anyone —
    # otherwise bob could log in as alice with the shared password.
    r = reload_routes(MEMAIX_ALLOWED_USERS="alice,bob", MEMAIX_LOGIN_PASSWORD_HASH=_hash("pw"))
    assert r._verify_password("alice", "pw") is False
    assert r._verify_password("bob", "pw") is False


def test_per_user_hashes_bind_password_to_user(reload_routes):
    r = reload_routes(
        MEMAIX_ALLOWED_USERS="alice,bob",
        MEMAIX_LOGIN_PASSWORD_HASH_ALICE=_hash("alice-pw"),
        MEMAIX_LOGIN_PASSWORD_HASH_BOB=_hash("bob-pw"),
    )
    assert r._verify_password("alice", "alice-pw") is True
    assert r._verify_password("bob", "bob-pw") is True
    # bob's password must not authenticate as alice
    assert r._verify_password("alice", "bob-pw") is False
    assert r._verify_password("bob", "alice-pw") is False


def test_no_hash_configured_denies(reload_routes):
    r = reload_routes(MEMAIX_ALLOWED_USERS="alice", MEMAIX_LOGIN_PASSWORD_HASH=None)
    assert r._verify_password("alice", "anything") is False


# ------------------------------------------------------------------
# Invited users (password in acl.yaml) — incident 2026-10-04, `itsq`:
# /app/invite wrote users.<id>.password_hash to acl.yaml, but the board only
# read env hashes, so every invited user got "invalid credentials".
# ------------------------------------------------------------------


def _with_acl(r, monkeypatch, users):
    monkeypatch.setattr(r, "_acl_users", lambda: users)
    return r


def test_invited_acl_user_can_log_in(reload_routes, monkeypatch):
    r = _with_acl(
        reload_routes(MEMAIX_ALLOWED_USERS="alice", MEMAIX_LOGIN_PASSWORD_HASH=None),
        monkeypatch,
        {"itsq": {"password_hash": _hash("itsq-pw"), "grants": {"x": "owner"}}},
    )
    assert r._is_allowed("itsq") is True
    assert r._verify_password("itsq", "itsq-pw") is True
    assert r._verify_password("itsq", "wrong") is False


def test_disabled_acl_user_is_refused_even_with_right_password(reload_routes, monkeypatch):
    r = _with_acl(
        reload_routes(MEMAIX_ALLOWED_USERS="alice"),
        monkeypatch,
        {"itsq": {"password_hash": _hash("itsq-pw"), "disabled": True}},
    )
    assert r._is_allowed("itsq") is False


def test_disabled_in_acl_wins_over_env_allow_list(reload_routes, monkeypatch):
    r = _with_acl(
        reload_routes(MEMAIX_ALLOWED_USERS="alice", MEMAIX_LOGIN_PASSWORD_HASH_ALICE=_hash("alice-pw")),
        monkeypatch,
        {"alice": {"disabled": True}},
    )
    assert r._is_allowed("alice") is False


def test_acl_user_without_password_is_not_allowed(reload_routes, monkeypatch):
    r = _with_acl(reload_routes(MEMAIX_ALLOWED_USERS="alice"), monkeypatch, {"pending": {"grants": {}}})
    assert r._is_allowed("pending") is False
    assert r._verify_password("pending", "") is False


def test_acl_password_does_not_authenticate_another_user(reload_routes, monkeypatch):
    r = _with_acl(
        reload_routes(MEMAIX_ALLOWED_USERS="alice", MEMAIX_LOGIN_PASSWORD_HASH_ALICE=_hash("alice-pw")),
        monkeypatch,
        {"itsq": {"password_hash": _hash("itsq-pw")}},
    )
    assert r._verify_password("alice", "itsq-pw") is False
    assert r._verify_password("itsq", "alice-pw") is False


def test_shared_hash_never_authenticates_an_acl_user(reload_routes, monkeypatch):
    # One env user + a shared hash: the shared password must not open an
    # invited user's account (or one that has no hash of its own).
    r = _with_acl(
        reload_routes(MEMAIX_ALLOWED_USERS="alice", MEMAIX_LOGIN_PASSWORD_HASH=_hash("shared")),
        monkeypatch,
        {"itsq": {"password_hash": _hash("itsq-pw")}, "nohash": {}},
    )
    assert r._verify_password("alice", "shared") is True
    assert r._verify_password("itsq", "shared") is False
    assert r._verify_password("nohash", "shared") is False


def test_env_hash_takes_precedence_over_acl(reload_routes, monkeypatch):
    r = _with_acl(
        reload_routes(MEMAIX_ALLOWED_USERS="alice", MEMAIX_LOGIN_PASSWORD_HASH_ALICE=_hash("env-pw")),
        monkeypatch,
        {"alice": {"password_hash": _hash("acl-pw")}},
    )
    assert r._verify_password("alice", "env-pw") is True
    assert r._verify_password("alice", "acl-pw") is False


def test_unreadable_acl_keeps_env_users_working(reload_routes, monkeypatch):
    r = reload_routes(MEMAIX_ALLOWED_USERS="alice", MEMAIX_LOGIN_PASSWORD_HASH_ALICE=_hash("alice-pw"))

    def boom():
        raise OSError("acl.yaml unreadable")

    from memaix_gateway import config
    monkeypatch.setattr(config, "load", boom)
    assert r._acl_users() == {}
    assert r._is_allowed("alice") is True
    assert r._verify_password("alice", "alice-pw") is True
    assert r._is_allowed("itsq") is False


def test_cookie_for_invited_user_is_accepted(reload_routes, monkeypatch):
    r = _with_acl(
        reload_routes(MEMAIX_TRANSPORT=None, HYDRA_SYSTEM_SECRET=None, MEMAIX_ALLOWED_USERS="alice"),
        monkeypatch,
        {"itsq": {"password_hash": _hash("itsq-pw")}},
    )

    class _Req:
        cookies = {"memaix_board": r._make_cookie("itsq")}

    assert r._check_cookie(_Req()) == "itsq"
    monkeypatch.setattr(r, "_acl_users", lambda: {"itsq": {"password_hash": _hash("itsq-pw"), "disabled": True}})
    assert r._check_cookie(_Req()) is None  # kill-switch also ends an existing session
