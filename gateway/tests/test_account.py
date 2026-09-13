# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for account_* OAuth linking tools."""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from memaix_gateway.acl import Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.tools.account import (
    _pending_states,
    account_link,
    account_link_imap,
    account_list,
    account_unlink,
    validate_state,
)


@pytest.fixture(autouse=True)
def clear_pending_states():
    """Ensure _pending_states is empty before and after each test."""
    _pending_states.clear()
    yield
    _pending_states.clear()


@pytest.fixture()
def acl():
    return Acl(users={"alice": {"grants": {}}}, projects={})


@pytest.fixture()
def store(tmp_path):
    return TokenStore.for_path(tmp_path / "t.db", Fernet.generate_key())


# ---------------------------------------------------------------------------
# account_link
# ---------------------------------------------------------------------------


def test_account_link_returns_link_url(acl):
    result = account_link(acl, "alice", "google", "http://localhost:8080")
    assert "/link/google" in result["link_url"]
    assert result["provider"] == "google"
    assert result["expires_in"] == 600


def test_account_link_unknown_provider_raises(acl):
    with pytest.raises(ValueError, match="unknown provider"):
        account_link(acl, "alice", "yahoo", "http://localhost:8080")


def test_account_link_state_stored(acl):
    result = account_link(acl, "alice", "google", "http://localhost:8080")
    # Extract state from link_url query param
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(result["link_url"])
    state = parse_qs(parsed.query)["state"][0]
    assert state in _pending_states
    assert _pending_states[state]["user_id"] == "alice"
    assert _pending_states[state]["provider"] == "google"


# ---------------------------------------------------------------------------
# validate_state
# ---------------------------------------------------------------------------


def test_validate_state_valid(acl):
    result = account_link(acl, "alice", "microsoft", "http://localhost:8080")
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(result["link_url"])
    state = parse_qs(parsed.query)["state"][0]

    pending = validate_state(state)
    assert pending is not None
    assert pending["user_id"] == "alice"
    assert pending["provider"] == "microsoft"


def test_validate_state_expired(acl):
    """A state with exp in the past should be rejected."""
    _pending_states["stale_state"] = {
        "user_id": "alice",
        "provider": "google",
        "exp": int(time.time()) - 1,
    }
    result = validate_state("stale_state")
    assert result is None


def test_validate_state_unknown():
    result = validate_state("totally-unknown-state")
    assert result is None


def test_validate_state_consumed_once(acl):
    """validate_state should pop the state so it can't be used twice."""
    result = account_link(acl, "alice", "google", "http://localhost:8080")
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(result["link_url"])
    state = parse_qs(parsed.query)["state"][0]

    first = validate_state(state)
    second = validate_state(state)
    assert first is not None
    assert second is None


def test_sqlite_state_backend_roundtrip(acl, tmp_path, monkeypatch):
    """With MEMAIX_STATE_DB set, pending states persist in SQLite (multi-worker)."""
    import memaix_gateway.tools.account as account_mod
    monkeypatch.setenv("MEMAIX_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(account_mod, "_sqlite_store", None)

    from urllib.parse import urlparse, parse_qs
    result = account_link(acl, "alice", "google", "http://localhost:8080")
    state = parse_qs(urlparse(result["link_url"]).query)["state"][0]

    # Not held in the in-memory dict — it lives in SQLite.
    assert state not in account_mod._pending_states
    # A fresh store instance (simulating another worker) can validate it.
    monkeypatch.setattr(account_mod, "_sqlite_store", None)
    pending = validate_state(state)
    assert pending is not None and pending["user_id"] == "alice"
    # Consumed once.
    assert validate_state(state) is None


# ---------------------------------------------------------------------------
# account_list
# ---------------------------------------------------------------------------


def test_account_list_empty(acl, store):
    result = account_list(acl, "alice", store)
    assert result == []


def test_account_list_shows_stored(acl, store):
    store.store("alice", "google", "jimmy@gmail.com", {"scope": "email"})
    result = account_list(acl, "alice", store)
    assert len(result) == 1
    assert result[0]["provider"] == "google"
    assert result[0]["account"] == "jimmy@gmail.com"


# ---------------------------------------------------------------------------
# account_unlink
# ---------------------------------------------------------------------------


def test_account_unlink_deletes(acl, store):
    store.store("alice", "google", "jimmy@gmail.com", {"token": "t"})
    result = account_unlink(acl, "alice", "google", "jimmy@gmail.com", store)
    assert result == {"ok": True}
    assert account_list(acl, "alice", store) == []


def test_account_unlink_missing_raises(acl, store):
    with pytest.raises(FileNotFoundError):
        account_unlink(acl, "alice", "google", "ghost@gmail.com", store)


# ---------------------------------------------------------------------------
# IMAP (non-OAuth per-user link path)
# ---------------------------------------------------------------------------


def test_account_link_imap_returns_settings_page_url_not_oauth_state(acl):
    """IMAP has no OAuth flow — account_link(provider='imap') must return a
    link to the web form, not a /link/{provider}?state=... OAuth URL."""
    result = account_link(acl, "alice", "imap", "http://localhost:8080")
    assert result["provider"] == "imap"
    assert "/link/imap" not in result["link_url"]
    assert "state=" not in result["link_url"]
    assert "/app/settings" in result["link_url"]
    # No OAuth state was allocated for a non-OAuth provider.
    assert _pending_states == {}


def test_account_link_imap_stores_encrypted_credential(store):
    result = account_link_imap(
        "alice", "alice@personal.example.com", "imap.personal.example.com",
        "alice", "s3cret-pw", store,
    )
    assert result == {"ok": True, "provider": "imap", "account": "alice@personal.example.com"}
    stored = store.load_one("alice", "imap", "alice@personal.example.com")
    assert stored == {"host": "imap.personal.example.com", "user": "alice", "password": "s3cret-pw"}


def test_account_link_imap_stores_optional_port(store):
    account_link_imap(
        "alice", "alice@personal.example.com", "imap.personal.example.com",
        "alice", "pw", store, port=1993,
    )
    stored = store.load_one("alice", "imap", "alice@personal.example.com")
    assert stored["port"] == 1993


def test_account_link_imap_missing_field_raises(store):
    with pytest.raises(ValueError):
        account_link_imap("alice", "", "imap.example.com", "alice", "pw", store)
    with pytest.raises(ValueError):
        account_link_imap("alice", "a@x.com", "", "alice", "pw", store)
    with pytest.raises(ValueError):
        account_link_imap("alice", "a@x.com", "imap.example.com", "", "pw", store)
    with pytest.raises(ValueError):
        account_link_imap("alice", "a@x.com", "imap.example.com", "alice", "", store)


def test_account_link_imap_return_value_never_contains_password(store):
    result = account_link_imap(
        "alice", "alice@personal.example.com", "imap.personal.example.com",
        "alice", "s3cret-pw", store,
    )
    assert "s3cret-pw" not in str(result)
    assert "password" not in result


def test_account_list_never_leaks_imap_password(store):
    account_link_imap(
        "alice", "alice@personal.example.com", "imap.personal.example.com",
        "alice", "s3cret-pw", store,
    )
    acl = Acl(users={"alice": {"grants": {}}}, projects={})
    accounts = account_list(acl, "alice", store)
    assert len(accounts) == 1
    assert accounts[0]["provider"] == "imap"
    assert accounts[0]["account"] == "alice@personal.example.com"
    assert "s3cret-pw" not in str(accounts)
    assert "password" not in accounts[0]


def test_account_unlink_allows_imap_provider(store):
    account_link_imap(
        "alice", "alice@personal.example.com", "imap.personal.example.com",
        "alice", "pw", store,
    )
    acl = Acl(users={"alice": {"grants": {}}}, projects={})
    result = account_unlink(acl, "alice", "imap", "alice@personal.example.com", store)
    assert result == {"ok": True}
    assert account_list(acl, "alice", store) == []


def test_account_link_still_rejects_unknown_provider(acl):
    with pytest.raises(ValueError, match="unknown provider"):
        account_link(acl, "alice", "yahoo", "http://localhost:8080")
