# SPDX-License-Identifier: AGPL-3.0-or-later
"""H3: the OAuth link flow must be bound to the web session that started it.

Attack: attacker A calls account_link("google") and sends the link to victim B.
B approves at Google; Google redirects B's browser to /link/google/callback.
Before the fix the callback resolved user_id from the state alone, so B's
tokens were stored under A. Now start, callback and confirm all require the
web session of the user the state was minted for, plus a per-flow cookie that
only /link/{provider} hands out, and a confirmation step.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest

from memaix_gateway import config as config_mod
from memaix_gateway import server as server_mod
from memaix_gateway.tools import account
from tests import link_session as ls
from tests.test_http_app_characterization import _client, _fake_async_client, _FakeResp, cfg  # noqa: F401

VICTIM_EMAIL = "victim@gmail.com"


class _Store:
    def __init__(self):
        self.stored = []

    def store(self, *a):
        self.stored.append(a)

    def load_one(self, *a):
        return None


@pytest.fixture()
def env(cfg, monkeypatch):  # noqa: F811
    store = _Store()
    ls.patch_session(monkeypatch)
    monkeypatch.setattr(server_mod, "_get_token_store", lambda: store)
    monkeypatch.setattr(config_mod, "secret", lambda ref: "x")
    cfg["oauth_providers"] = {"google": {"client_id": "cid", "client_secret_ref": "env:X"}}
    # Google token exchange (mocked) returns the VICTIM's tokens.
    _fake_async_client(
        monkeypatch,
        lambda m, u, kw: _FakeResp(200, {"email": VICTIM_EMAIL, "access_token": "victim-at",
                                         "refresh_token": "victim-rt", "scope": "openid email"}),
        [],
    )
    return store


def _attacker_state() -> str:
    res = account.account_link(None, "attacker", "google", "https://mx.example")
    return parse_qs(urlparse(res["link_url"]).query)["state"][0]


# ---- the attack -------------------------------------------------------------


def test_callback_from_other_browser_is_refused(env):
    state = _attacker_state()
    r = ls.callback(_client(), state=state, session_user=None, link_cookie=False)  # victim: no cookies
    assert r.status_code == 401
    r = ls.confirm(_client(), state=state, session_user=None, link_cookie=False)
    assert r.status_code == 401
    assert env.stored == [], "victim tokens must never be stored under the attacker"


def test_victim_logged_in_as_someone_else_is_refused(env):
    state = _attacker_state()
    forged = {"Cookie": f"memaix_board=victim; memaix_link_google={'0' * 64}"}
    assert _client().get(f"/link/google/callback?code=c&state={state}", headers=forged).status_code == 403
    assert ls.confirm(_client(), state=state, session_user="victim").status_code == 403
    assert env.stored == []


def test_attacker_session_without_flow_cookie_is_refused(env):
    """Attacker's own session is not enough: the callback browser must be the one that ran link_start."""
    state = _attacker_state()
    r = ls.callback(_client(), state=state, session_user="attacker", link_cookie=False)
    assert r.status_code == 403
    r = ls.confirm(_client(), state=state, session_user="attacker", link_cookie=False)
    assert r.status_code == 403
    assert env.stored == []


def test_link_start_without_web_session_goes_to_login_not_google(env):
    state = _attacker_state()
    r = _client().get(f"/link/google?state={state}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/app/login?next=")
    assert "accounts.google.com" not in r.headers["location"]
    assert "set-cookie" not in r.headers


def test_link_start_with_other_users_session_is_refused(env):
    state = _attacker_state()
    r = _client().get(f"/link/google?state={state}", headers=ls.cookies(state, session_user="victim", link_cookie=False),
                      follow_redirects=False)
    assert r.status_code == 403 and "set-cookie" not in r.headers


@pytest.mark.parametrize("state", ["", "nope"])
def test_missing_or_unknown_state_is_refused(env, state):
    assert _client().get(f"/link/google?state={state}", headers=ls.cookies(state), follow_redirects=False).status_code == 400
    assert ls.callback(_client(), state=state).status_code == 400
    assert ls.confirm(_client(), state=state).status_code == 400


def test_expired_state_is_refused(env):
    ls.mint("old", user="attacker", ttl=-5)
    assert ls.callback(_client(), state="old", session_user="attacker").status_code == 400
    assert ls.confirm(_client(), state="old", session_user="attacker").status_code == 400
    assert env.stored == []


def test_garbage_flow_cookie_is_refused(env):
    state = _attacker_state()
    h = {"Cookie": "memaix_board=attacker; memaix_link_google=garbage"}
    assert _client().get(f"/link/google/callback?code=c&state={state}", headers=h).status_code == 403


def test_refused_attempt_does_not_burn_the_owners_state(env):
    state = _attacker_state()
    assert ls.confirm(_client(), state=state, session_user=None, link_cookie=False).status_code == 401
    assert account.peek_state(state) is not None


def test_no_signing_key_fails_closed(env, monkeypatch):
    from memaix_gateway import server_http
    state = _attacker_state()
    monkeypatch.setattr(server_http, "_link_key", lambda: None)
    assert _client().get(f"/link/google?state={state}", headers={"Cookie": "memaix_board=attacker"},
                         follow_redirects=False).status_code == 403
    assert env.stored == []


def test_callback_is_get_only_and_does_not_store(env):
    """Step 1 only renders the confirmation page; nothing is exchanged or stored."""
    state = _attacker_state()
    start = _client().get(f"/link/google?state={state}", headers=ls.cookies(state, session_user="attacker", link_cookie=False),
                          follow_redirects=False)
    nonce = re.search(r"memaix_link_google=([0-9a-f]+)", start.headers["set-cookie"]).group(1)
    h = {"Cookie": f"memaix_board=attacker; memaix_link_google={nonce}"}
    r = _client().get(f"/link/google/callback?code=c&state={state}", headers=h)
    assert r.status_code == 200 and "attacker" in r.text and "Ja, koppla" in r.text
    assert env.stored == []


# ---- the legitimate path still works --------------------------------------------


def test_legitimate_flow_start_callback_confirm_stores_under_owner(env):
    state = _attacker_state()  # here "attacker" plays the honest owner of the state
    start = _client().get(f"/link/google?state={state}", headers=ls.cookies(state, session_user="attacker", link_cookie=False),
                          follow_redirects=False)
    assert start.status_code == 307 and "accounts.google.com" in start.headers["location"]
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"]
    cookie = start.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    nonce = re.search(r"memaix_link_google=([0-9a-f]+)", cookie).group(1)
    h = {"Cookie": f"memaix_board=attacker; memaix_link_google={nonce}"}

    page = _client().get(f"/link/google/callback?code=thecode&state={state}", headers=h)
    assert page.status_code == 200 and "attacker" in page.text

    done = _client().post(
        "/link/google/confirm", content=f"code=thecode&state={state}",
        headers={"Content-Type": "application/x-www-form-urlencoded", **h},
    )
    assert done.status_code == 200
    assert [s[:3] for s in env.stored] == [("attacker", "google", VICTIM_EMAIL)]

    replay = _client().post(
        "/link/google/confirm", content=f"code=thecode&state={state}",
        headers={"Content-Type": "application/x-www-form-urlencoded", **h},
    )
    assert replay.status_code == 400 and len(env.stored) == 1


# ---- Simon's conditions -------------------------------------------------------


def _flow(state, user):
    start = _client().get(f"/link/google?state={state}", headers=ls.cookies(state, session_user=user, link_cookie=False),
                          follow_redirects=False)
    return start


def test_non_ascii_username_legit_flow_succeeds(env):
    ls.mint("uni", user="åsa-ö")
    h = {"Cookie": "memaix_board=åsa-ö; memaix_link_google=" + __import__("memaix_gateway.server_http", fromlist=["x"])._link_nonce("uni")}
    hb = {"Cookie": h["Cookie"].encode("utf-8")}
    page = _client().get("/link/google/callback?code=c&state=uni", headers=hb)
    assert page.status_code == 200, page.text
    done = _client().post("/link/google/confirm", content="code=c&state=uni",
                          headers={"Content-Type": "application/x-www-form-urlencoded", **hb})
    assert done.status_code == 200
    assert [s[0] for s in env.stored] == ["åsa-ö"]


def test_non_ascii_session_user_mismatch_is_403_not_500(env):
    state = _attacker_state()
    hb = {"Cookie": "memaix_board=åäö; memaix_link_google=x".encode("utf-8")}
    assert _client().get(f"/link/google/callback?code=c&state={state}", headers=hb).status_code == 403


def test_non_ascii_broken_flow_cookie_is_403_not_500(env):
    state = _attacker_state()
    hb = {"Cookie": "memaix_board=attacker; memaix_link_google=åäö".encode("utf-8")}
    assert _client().get(f"/link/google/callback?code=c&state={state}", headers=hb).status_code == 403
    r = _client().post("/link/google/confirm", content=f"code=c&state={state}",
                       headers={"Content-Type": "application/x-www-form-urlencoded", **hb})
    assert r.status_code == 403
    assert env.stored == []


def test_disabled_user_is_refused_everywhere(env):
    ls.mint("dis", user="disabled-user")
    r = _client().get("/link/google?state=dis", headers=ls.cookies("dis", session_user="disabled-user", link_cookie=False),
                      follow_redirects=False)
    assert r.status_code == 403 and "set-cookie" not in r.headers
    assert ls.callback(_client(), state="dis", session_user="disabled-user").status_code == 403
    assert ls.confirm(_client(), state="dis", session_user="disabled-user").status_code == 403
    assert env.stored == []


def test_confirmation_page_forbids_framing(env):
    state = _attacker_state()
    r = ls.callback(_client(), state=state, session_user="attacker")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["x-frame-options"] == "DENY"
