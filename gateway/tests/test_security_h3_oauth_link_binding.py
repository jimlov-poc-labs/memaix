# SPDX-License-Identifier: AGPL-3.0-or-later
"""H3: the OAuth link flow must be bound to the browser session that started it.

Attack: attacker A calls account_link("google") and sends the link to victim B.
B approves at Google; Google redirects B's browser to /link/google/callback.
The callback resolves user_id from the state alone, so B's tokens are stored
under A. The state is a bearer token: nobody checks WHO redeems it.

These tests assert the desired (fail-closed) behaviour and FAIL on code that
does not bind the callback to the initiating session.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from memaix_gateway import config as config_mod
from memaix_gateway import server as server_mod
from memaix_gateway.tools import account
from tests.test_http_app_characterization import _client, _fake_async_client, _FakeResp, cfg  # noqa: F401

VICTIM_EMAIL = "victim@gmail.com"


class _Store:
    def __init__(self):
        self.stored = []

    def store(self, *a):
        self.stored.append(a)

    def load_one(self, *a):
        return None


def _setup(cfg, monkeypatch):  # noqa: F811
    store = _Store()
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


def _attacker_link() -> str:
    res = account.account_link(None, "attacker", "google", "https://mx.example")
    return parse_qs(urlparse(res["link_url"]).query)["state"][0]


def test_callback_from_other_browser_is_refused(cfg, monkeypatch):  # noqa: F811
    store = _setup(cfg, monkeypatch)
    state = _attacker_link()
    victim_browser = _client()  # fresh client: no cookie from attacker's session
    r = victim_browser.get(f"/link/google/callback?code=victim-code&state={state}")
    assert r.status_code in (400, 401, 403), f"callback accepted foreign browser: {r.status_code}"
    assert store.stored == [], f"victim tokens stored under attacker: {[s[:3] for s in store.stored]}"


def test_link_start_without_web_session_is_refused(cfg, monkeypatch):  # noqa: F811
    _setup(cfg, monkeypatch)
    state = _attacker_link()
    r = _client().get(f"/link/google?state={state}", follow_redirects=False)
    assert r.status_code in (401, 403) or (
        r.status_code in (302, 307) and "accounts.google.com" not in r.headers.get("location", "")
    ), f"link_start redirected to Google without any session: {r.status_code}"
