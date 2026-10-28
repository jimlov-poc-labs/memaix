# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-link Google capabilities (mail = gmail.readonly), scope merge on
re-link, and visible source failures in the brief."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from memaix_gateway import config as config_mod
from memaix_gateway import server as server_mod
from memaix_gateway.acl import Acl
from memaix_gateway.notify import brief as brief_mod
from memaix_gateway.notify.brief import build
from memaix_gateway.tools import account
from tests.test_http_app_characterization import _client, _fake_async_client, _FakeResp, cfg  # noqa: F401

BASE = ["openid", "email", "https://www.googleapis.com/auth/calendar.events"]
GMAIL_RO = "https://www.googleapis.com/auth/gmail.readonly"


def _google_cfg(cfg):  # noqa: F811
    cfg["oauth_providers"] = {"google": {
        "client_id": "cid", "scopes": BASE, "capability_scopes": {"mail": [GMAIL_RO]},
    }}
    cfg["server"] = {"public_url": "https://pub.example/"}


def _q(resp):
    return {k: v[0] for k, v in parse_qs(urlparse(resp.headers["location"]).query).items()}


def test_base_only_scopes(cfg):  # noqa: F811
    _google_cfg(cfg)
    q = _q(_client().get("/link/google?state=s", follow_redirects=False))
    assert q["scope"].split() == BASE
    assert q["include_granted_scopes"] == "true"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"


def test_base_plus_mail_scopes_and_never_compose(cfg):  # noqa: F811
    _google_cfg(cfg)
    q = _q(_client().get("/link/google?state=s&capabilities=mail", follow_redirects=False))
    assert q["scope"].split() == [*BASE, GMAIL_RO]
    assert "compose" not in q["scope"] and "send" not in q["scope"] and "modify" not in q["scope"]


def test_unknown_capability_rejected_at_start(cfg):  # noqa: F811
    _google_cfg(cfg)
    r = _client().get("/link/google?state=s&capabilities=compose")
    assert r.status_code == 400 and r.json() == {"error": "unknown_capability"}


def test_capability_not_configured_rejected(cfg):  # noqa: F811
    _google_cfg(cfg)
    del cfg["oauth_providers"]["google"]["capability_scopes"]
    r = _client().get("/link/google?state=s&capabilities=mail")
    assert r.status_code == 400


def test_microsoft_has_no_include_granted_scopes(cfg):  # noqa: F811
    r = _client().get("/link/microsoft", follow_redirects=False)
    assert "include_granted_scopes" not in r.headers["location"]


def test_account_link_capabilities():
    out = account.account_link(None, "u", "google", "https://pub.example", ["mail"])
    assert out["link_url"].endswith("&capabilities=mail")
    plain = account.account_link(None, "u", "google", "https://pub.example")
    assert "capabilities" not in plain["link_url"]
    assert "capabilities" not in account.account_link(None, "u", "google", "https://p", [])["link_url"]


@pytest.mark.parametrize("provider,caps", [
    ("google", ["compose"]), ("google", ["mail", "x"]), ("microsoft", ["mail"]), ("imap", ["mail"]),
])
def test_account_link_rejects_unknown(provider, caps):
    with pytest.raises(ValueError, match="unknown capabilities"):
        account.account_link(None, "u", provider, "https://p", caps)


class _Store:
    def __init__(self, existing=None):
        self.existing, self.stored = existing, []

    def store(self, *a):
        self.stored.append(a)

    def load_one(self, *a):
        return self.existing


def _relink(cfg, monkeypatch, existing, token):  # noqa: F811
    store = _Store(existing)
    monkeypatch.setattr(account, "validate_state", lambda s: {"user_id": "u1"})
    monkeypatch.setattr(server_mod, "_get_token_store", lambda: store)
    monkeypatch.setattr(config_mod, "secret", lambda ref: "x")
    cfg["oauth_providers"] = {"google": {"client_id": "cid", "client_secret_ref": "env:X"}}
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(200, token), [])
    r = _client().get("/link/google/callback?code=c&state=good")
    assert r.status_code == 200
    return store.stored[0][3]


def test_relink_merges_scopes_and_keeps_refresh_token(cfg, monkeypatch):  # noqa: F811
    old = {"scope": f"openid {GMAIL_RO}", "refresh_token": "old-r"}
    new = {"email": "a@x.se", "access_token": "t", "scope": "openid email calendar"}
    data = _relink(cfg, monkeypatch, old, new)
    assert set(data["scope"].split()) == {"openid", "email", "calendar", GMAIL_RO}
    assert data["refresh_token"] == "old-r"


def test_relink_new_refresh_token_wins(cfg, monkeypatch):  # noqa: F811
    data = _relink(cfg, monkeypatch, {"scope": "a", "refresh_token": "old"},
                   {"email": "a@x.se", "scope": "b", "refresh_token": "new"})
    assert data["refresh_token"] == "new"


def test_first_link_unchanged(cfg, monkeypatch):  # noqa: F811
    tok = {"email": "a@x.se", "scope": "b", "access_token": "t"}
    data = _relink(cfg, monkeypatch, None, dict(tok))
    assert data["scope"] == "b" and "refresh_token" not in data


# --- brief --------------------------------------------------------------

NOW = datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)


def _acl():
    return Acl(
        users={"alice": {"grants": {"proj": "owner"}}},
        projects={"proj": {"vault": "/v", "mailbox": {"host": "h"}, "calendar": {"url": "x"}}},
    )


def _prefs():
    return {"enabled": True, "timezone": "UTC", "brief_time": "07:00", "channels": [], "projects": ["proj"]}


class _HttpErr(Exception):
    def __init__(self, status):
        super().__init__("403 Client Error: https://gmail.googleapis.com/x?access_token=SECRET")
        self.response = type("R", (), {"status_code": status})()


def test_brief_mail_403_shows_warning_not_inget_nytt(caplog):
    def boom(*a, **k):
        raise _HttpErr(403)
    res = build(_acl(), "alice", None, _prefs(), now=NOW, tools={"email_list": boom})
    assert "⚠ [proj] mejl otillgänglig: HTTP 403" in res["markdown"]
    assert "Inget nytt" not in res["markdown"]
    assert "SECRET" not in res["markdown"] and "SECRET" not in caplog.text
    assert res["source_warnings"] == ["⚠ [proj] mejl otillgänglig: HTTP 403"]
    assert not res["empty"]
    assert brief_mod.LAST_SOURCE_WARNINGS["alice"] == res["source_warnings"]


def test_brief_insufficient_permissions_reason():
    def boom(*a, **k):
        raise RuntimeError("403 insufficientPermissions token=SECRET")
    res = build(_acl(), "alice", None, _prefs(), now=NOW, tools={"email_list": boom})
    assert "mejl otillgänglig: insufficientPermissions" in res["markdown"]
    assert "SECRET" not in res["markdown"]


def test_brief_calendar_failure_warns():
    def boom(*a, **k):
        raise ValueError("secret detail")
    res = build(_acl(), "alice", None, _prefs(), now=NOW, tools={"calendar_events": boom})
    assert "kalender otillgänglig: ValueError" in res["markdown"]
    assert "secret detail" not in res["markdown"]


def test_brief_ok_and_empty_still_inget_nytt():
    res = build(_acl(), "alice", None, _prefs(), now=NOW, tools={"email_list": lambda *a, **k: []})
    assert "_Inget nytt._" in res["markdown"]
    assert res["source_warnings"] == [] and res["empty"]


def test_brief_warning_sent_even_if_send_when_empty_false():
    def boom(*a, **k):
        raise _HttpErr(403)
    cfg_ = {"memaix": {"brief": {"send_when_empty": False}}}
    res = build(_acl(), "alice", cfg_, _prefs(), now=NOW, tools={"email_list": boom})
    assert res["markdown"] and res["source_warnings"]
