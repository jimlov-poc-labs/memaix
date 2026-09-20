# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web tests for per-account project scoping — the settings page's half of
the "n+1 accounts, shared with the projects I choose" feature.

Uses a REAL TokenStore rather than a fake: the assertions here are about
grant semantics (replace-not-merge, '*' collapsing, per-capability
independence), and a fake would only prove the fake agrees with itself.

Same rig pattern as test_accounts_imap_web.py — auth bypassed via
web_routes_mod._require_user, token store injected into the api module.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.web import routes as web_routes_mod


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    acl = Acl(
        users={
            "alice": {"grants": {"proj": "owner", "other": "owner"}},
            "bob": {"grants": {"secret_proj": "owner"}},
        },
        projects={
            "proj": {"vault": str(vault)},
            "other": {"vault": str(vault)},
            "secret_proj": {"vault": str(vault)},
        },
    )
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    current = {"user": "alice"}
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: current["user"])

    store = TokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key())
    from memaix_gateway.web.api import accounts as api_accounts_mod
    monkeypatch.setattr(api_accounts_mod, "_token_store", lambda: store)
    monkeypatch.setattr(api_accounts_mod, "_public_url", lambda: "https://mcp.example.com")

    store.store("alice", "google", "a@gmail.com", {"access_token": "tok"})

    app = Starlette(routes=web_routes_mod.web_routes)
    return TestClient(app), store, current


def _set(client, **kw):
    body = {"provider": "google", "account": "a@gmail.com", "capability": "mail", "projects": []}
    body.update(kw)
    return client.post("/app/api/accounts/scopes", json=body)


def _entry(client, provider="google"):
    accounts = client.get("/app/api/accounts").json()
    return next(a for a in accounts if a["provider"] == provider)


# ------------------------------------------------------------------
# The opt-in default, as the page sees it
# ------------------------------------------------------------------


def test_a_freshly_linked_account_is_shared_with_nothing(rig):
    """The state the settings page must render as "shared with nothing":
    capabilities offered, none of them granted."""
    client, _, _ = rig
    entry = _entry(client)

    assert entry["capabilities"] == ["calendar", "mail"]
    assert entry["scopes_by_capability"] == {}


def test_capabilities_come_from_the_live_registry(rig):
    """Gmail is registered, so a Google account offers mail as well as
    calendar. This is the derivation that must NOT be used for the
    backfill — here it's right, because an added adapter should surface as
    a new unticked box."""
    client, _, _ = rig
    assert "mail" in _entry(client)["capabilities"]


def test_a_shared_imap_mailbox_offers_nothing_to_scope(rig, tmp_path, monkeypatch):
    """acl.yaml mailboxes belong to the project, not the user — the UI
    keys off an empty capability list to skip drawing a grid."""
    client, _, _ = rig
    acl = Acl(
        users={"alice": {"grants": {"proj": "owner"}}},
        projects={"proj": {"vault": str(tmp_path / "vault"),
                           "mailbox": {"user": "team@example.com", "host": "imap.example.com"}}},
    )
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)

    shared = [a for a in client.get("/app/api/accounts").json() if a.get("readonly")]

    assert shared and all("capabilities" not in a for a in shared)


# ------------------------------------------------------------------
# Granting and revoking
# ------------------------------------------------------------------


def test_granting_a_project_makes_it_show_up_in_the_listing(rig):
    client, _, _ = rig
    resp = _set(client, projects=["proj"])

    assert resp.status_code == 200
    assert resp.json()["projects"] == ["proj"]
    assert _entry(client)["scopes_by_capability"]["mail"] == ["proj"]


def test_projects_replace_rather_than_accumulate(rig):
    """The page sends the full checked set on every change, so a second
    call with one project must not leave the first one behind."""
    client, _, _ = rig
    _set(client, projects=["proj", "other"])
    _set(client, projects=["other"])

    assert _entry(client)["scopes_by_capability"]["mail"] == ["other"]


def test_an_empty_list_revokes(rig):
    client, _, _ = rig
    _set(client, projects=["proj"])
    _set(client, projects=[])

    assert _entry(client)["scopes_by_capability"].get("mail", []) == []


def test_all_projects_is_stored_as_a_wildcard_not_an_expansion(rig):
    """Unticking "Alla projekt" has to clear everything rather than leave a
    frozen snapshot of today's projects — which only works if '*' was
    never expanded in the first place. A project added tomorrow is
    included for the same reason."""
    client, _, _ = rig
    _set(client, projects=["*"])

    assert _entry(client)["scopes_by_capability"]["mail"] == ["*"]


def test_wildcard_absorbs_named_projects(rig):
    client, _, _ = rig
    resp = _set(client, projects=["*", "proj"])

    assert resp.json()["projects"] == ["*"]


def test_mail_and_calendar_are_scoped_independently(rig):
    """The per-capability promise, at the API boundary: sharing the
    calendar must leave the mailbox alone."""
    client, _, _ = rig
    _set(client, capability="calendar", projects=["proj"])

    scopes = _entry(client)["scopes_by_capability"]
    assert scopes["calendar"] == ["proj"]
    assert scopes.get("mail", []) == []


# ------------------------------------------------------------------
# Refusals
# ------------------------------------------------------------------


def test_scope_set_requires_auth(rig, monkeypatch):
    client, _, _ = rig
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    from memaix_gateway.web.api import accounts as api_accounts_mod
    monkeypatch.setattr(api_accounts_mod, "_require_user", lambda request: None)

    assert _set(client, projects=["proj"]).status_code == 401


def test_cannot_grant_to_a_project_you_cannot_reach(rig):
    """Otherwise this would be a quiet way to stage your mailbox into
    someone else's project."""
    client, store, _ = rig
    resp = _set(client, projects=["secret_proj"])

    assert resp.status_code == 403  # AccessDenied, not a generic 400
    assert store.list_scopes("alice") == []


def test_cannot_scope_an_account_you_have_not_linked(rig):
    client, _, _ = rig
    resp = _set(client, account="someone-else@gmail.com", projects=["proj"])

    assert resp.status_code == 404


def test_unknown_provider_is_rejected(rig):
    client, _, _ = rig
    assert _set(client, provider="nope", projects=["proj"]).status_code == 400


def test_projects_must_be_a_list_of_strings(rig):
    """Guards the handler against a malformed body reaching set_scopes and
    being stored as a stringified project name."""
    client, store, _ = rig
    assert _set(client, projects="proj").status_code == 400
    assert _set(client, projects=[1, 2]).status_code == 400
    assert store.list_scopes("alice") == []


def test_bad_json_is_rejected(rig):
    client, _, _ = rig
    resp = client.post(
        "/app/api/accounts/scopes", content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_scopes_route_is_not_shadowed_by_the_unlink_route(rig):
    """/app/api/accounts/{provider} sits right after this route; if the
    literal segment lost the race, "scopes" would be read as a provider
    name."""
    client, _, _ = rig
    assert _set(client, projects=["proj"]).status_code == 200


# ------------------------------------------------------------------
# Interaction with the rest of the account lifecycle
# ------------------------------------------------------------------


def test_unlinking_an_account_clears_its_grants(rig):
    """A grant must not outlive the account and silently reattach if the
    same address is linked again later."""
    client, store, _ = rig
    _set(client, projects=["proj"])

    client.delete("/app/api/accounts/google?account=a%40gmail.com")
    store.store("alice", "google", "a@gmail.com", {"access_token": "tok2"})

    assert _entry(client)["scopes_by_capability"] == {}


def test_one_users_grants_are_invisible_to_another(rig):
    client, store, current = rig
    _set(client, projects=["proj"])

    store.store("bob", "google", "b@gmail.com", {"access_token": "tok"})
    current["user"] = "bob"

    assert _entry(client)["account"] == "b@gmail.com"
    assert _entry(client)["scopes_by_capability"] == {}
