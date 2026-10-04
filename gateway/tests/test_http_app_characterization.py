# SPDX-License-Identifier: AGPL-3.0-or-later
"""Karakteriseringstester för build_http_app (Sonar S3776-refaktorn).

Låser ruttlistan (ordning, path, metoder, namn), middleware-stacken,
lifespan-ordningen och handlers beteende, så att uppdelningen av
build_http_app kan bevisas beteendeoförändrad."""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from memaix_gateway import config as config_mod
from memaix_gateway import server as server_mod

# (path, metoder, namn) i exakt den ordning Starlette-routern har dem.
EXPECTED_HEAD = [
    ("/.well-known/oauth-protected-resource", ["GET", "HEAD"], "protected_resource_handler"),
    ("/", [], "StreamableHTTPASGIApp"),
    ("/health", ["GET", "HEAD"], "health_handler"),
    ("/.well-known/oauth-authorization-server", ["GET", "HEAD"], "as_metadata_handler"),
    ("/oauth2/register", ["POST"], "dcr_handler"),
    ("/oauth2/register/{client_id}", ["DELETE", "GET", "HEAD", "PUT"], "dcr_klient_handler"),
    ("/link/{provider}", ["GET", "HEAD"], "link_start"),
    ("/link/{provider}/callback", ["GET", "HEAD"], "link_callback"),
    ("/hooks", ["POST"], "rule_webhook"),
    ("/hooks/{token}", ["POST"], "rule_webhook"),
    ("/board/auth/login", ["POST"], "board_login"),
]
EXPECTED_BOARD_TAIL = [
    ("/privacy", ["GET", "HEAD"], "privacy_page"),
    ("/terms", ["GET", "HEAD"], "terms_page"),
    ("/app", ["GET", "HEAD"], "app_index"),
]
EXPECTED_END = [
    ("/board", ["GET", "HEAD"], "board_redirect"),
    ("/embed/booking.js", ["GET", "HEAD"], "booking_widget"),
    ("/book/{slug}/config", ["GET", "HEAD"], "booking_config"),
    ("/book/{slug}/config", ["OPTIONS"], "booking_options"),
    ("/book/{slug}/slots", ["GET", "HEAD"], "booking_slots"),
    ("/book/{slug}/slots", ["OPTIONS"], "booking_options"),
    ("/book/{slug}/times", ["GET", "HEAD"], "booking_times"),
    ("/book/{slug}/times", ["OPTIONS"], "booking_options"),
    ("/book/{slug}", ["POST"], "booking_create"),
    ("/book/{slug}", ["OPTIONS"], "booking_options"),
    ("/booking/{token}", ["GET", "HEAD"], "booking_manage_get"),
    ("/booking/{token}", ["OPTIONS"], "booking_options"),
    ("/booking/{token}/reschedule", ["POST"], "booking_reschedule"),
    ("/booking/{token}/reschedule", ["OPTIONS"], "booking_options"),
    ("/booking/{token}/cancel", ["POST"], "booking_cancel"),
    ("/booking/{token}/cancel", ["OPTIONS"], "booking_options"),
]
EXPECTED_ROUTE_COUNT = 80
HYDRA = "http://hydra:4444"
ISSUER = "https://i.example"


@pytest.fixture()
def cfg(monkeypatch, tmp_path):
    """Ersätter config.load; testet muterar den returnerade memaix-dicten."""
    data: dict = {"brand": {}, "memaix": {}, "acl": {}}
    monkeypatch.setattr(config_mod, "load", lambda: data)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    # StreamableHTTPSessionManager.run() får bara köras en gång per instans.
    monkeypatch.setattr(server_mod.mcp, "_session_manager", None)
    return data["memaix"]


def _client():
    return TestClient(server_mod.build_http_app())


def _routes(app):
    return [(r.path, sorted(r.methods or []), r.name) for r in app._plain_app.app.router.routes]


def test_route_list_is_frozen(cfg):
    routes = _routes(server_mod.build_http_app())
    assert len(routes) == EXPECTED_ROUTE_COUNT
    assert routes[: len(EXPECTED_HEAD)] == EXPECTED_HEAD
    assert routes[-len(EXPECTED_END):] == EXPECTED_END
    # board -> web -> booking-blocken ligger i den ordningen
    paths = [r[0] for r in routes]
    assert paths.index("/board/api/outbox/{id}") < paths.index("/privacy") < paths.index("/board")
    i = paths.index("/privacy")
    assert routes[i:i + 3] == EXPECTED_BOARD_TAIL
    assert paths.index("/app/api/memory/notes") < paths.index("/app/{page:str}")


def test_middleware_stack_shape(cfg):
    app = server_mod.build_http_app()
    assert type(app).__name__ == "_BookingCorsBypass"
    cors = app._cors_app
    assert type(cors).__name__ == "CORSMiddleware"
    assert cors.app is app._plain_app
    assert type(app._plain_app).__name__ == "BrowserRootRedirect"
    assert type(app._plain_app.app).__name__ == "Starlette"
    assert cors.allow_origins == ["https://claude.ai", "https://api.claude.ai"]
    assert sorted(cors.allow_methods) == ["DELETE", "GET", "OPTIONS", "POST"]
    assert "mcp-session-id" in cors.allow_headers
    assert cors.simple_headers["Access-Control-Expose-Headers"] == "mcp-session-id"


def test_booking_paths_bypass_cors_others_do_not(cfg):
    client = _client()
    hdr = {"Origin": "https://claude.ai", "Access-Control-Request-Method": "POST"}
    r = client.options("/health", headers=hdr)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://claude.ai"
    # /book/* och /booking/* går förbi app-CORS och får inte claude.ai-svaret
    r = client.options("/book/x/config", headers=hdr)
    assert r.headers.get("access-control-allow-origin") != "https://claude.ai"
    r = client.options("/booking/tok", headers=hdr)
    assert r.headers.get("access-control-allow-origin") != "https://claude.ai"


def test_transport_security_and_path(cfg):
    cfg["auth"] = {"resource_server_url": "https://mcp.test.example/x"}
    server_mod.build_http_app()
    ts = server_mod.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == ["mcp.test.example"]
    assert server_mod.mcp.settings.streamable_http_path == "/"


def test_transport_security_default_host(cfg):
    server_mod.build_http_app()
    assert server_mod.mcp.settings.transport_security.allowed_hosts == ["mcp.example.com"]


def test_auth_configured_when_issuer_set(cfg, monkeypatch):
    monkeypatch.setattr(server_mod.mcp.settings, "auth", None)
    monkeypatch.setattr(server_mod.mcp, "_token_verifier", None, raising=False)
    cfg["auth"] = {"issuer": "https://mcp.test.example"}
    server_mod.build_http_app()
    auth = server_mod.mcp.settings.auth
    assert str(auth.issuer_url).startswith("https://mcp.test.example")
    assert str(auth.resource_server_url).startswith("https://mcp.test.example")
    assert type(server_mod.mcp._token_verifier).__name__ == "HydraTokenVerifier"


def test_auth_untouched_without_issuer(cfg, monkeypatch):
    monkeypatch.setattr(server_mod.mcp.settings, "auth", None)
    server_mod.build_http_app()
    assert server_mod.mcp.settings.auth is None


def _patch_loops(monkeypatch, make):
    from memaix_gateway.booking import purge, reminders
    from memaix_gateway.connectors import calendar_cache
    from memaix_gateway.notify import scheduler

    monkeypatch.setattr(scheduler, "scheduler_loop", make("brief"))
    monkeypatch.setattr(calendar_cache, "calendar_sync_loop", make("calendar"))
    monkeypatch.setattr(purge, "consent_purge_loop", make("purge"))
    monkeypatch.setattr(reminders, "reminder_loop", make("reminders"))


def _record_loops(monkeypatch, started):
    import asyncio

    def make(name):
        async def loop(*_a, **_k):
            started.append(name)
            await asyncio.sleep(3600)
        return loop

    _patch_loops(monkeypatch, make)


def test_lifespan_loops_all_enabled_in_order(cfg, monkeypatch):
    started: list[str] = []
    _record_loops(monkeypatch, started)
    with _client() as client:
        assert client.get("/health").status_code == 200
    # senast tillagda wrappern är ytterst och startar först
    assert started == ["reminders", "purge", "calendar", "brief"]


@pytest.mark.parametrize(
    ("section", "key", "gone"),
    [
        ("brief", "enabled", "brief"),
        ("calendar_sync", "enabled", "calendar"),
        ("booking", "purge_enabled", "purge"),
        ("booking", "reminders_enabled", "reminders"),
    ],
)
def test_lifespan_loop_can_be_disabled(cfg, monkeypatch, section, key, gone):
    started: list[str] = []
    _record_loops(monkeypatch, started)
    cfg[section] = {key: False}
    with _client():
        pass
    assert gone not in started
    assert len(started) == 3


def test_lifespan_cancels_tasks_on_exit(cfg, monkeypatch):
    import asyncio

    cancelled: list[str] = []

    def make(name):
        async def loop(*_a, **_k):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(name)
                raise
        return loop

    _patch_loops(monkeypatch, make)
    with _client():
        pass
    assert sorted(cancelled) == ["brief", "calendar", "purge", "reminders"]


# ---------------------------------------------------------------- handlers


def test_health(cfg):
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "memaix"}


def test_protected_resource_metadata(cfg):
    cfg["auth"] = {"issuer": "https://mcp.test.example/", "resource_server_url": "https://mcp.test.example/x/"}
    r = _client().get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    assert r.json() == {
        "resource": "https://mcp.test.example/x",
        "authorization_servers": ["https://mcp.test.example/"],
        "bearer_methods_supported": ["header"],
    }
    assert r.headers["cache-control"] == "public, max-age=3600"


def test_protected_resource_metadata_defaults(cfg):
    r = _client().get("/.well-known/oauth-protected-resource")
    assert r.json()["resource"] == "https://mcp.example.com"
    assert r.json()["authorization_servers"] == ["https://mcp.example.com/"]


class _FakeResp:
    def __init__(self, status=200, data=None, content=b"", ctype="application/json"):
        self.status_code = status
        self._data = data if data is not None else {}
        self.content = content
        self.headers = {"content-type": ctype}

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


def _boom(*_a):
    raise RuntimeError("down")


def _fake_async_client(monkeypatch, responder, calls):
    class _Client:
        def __init__(self, *a, **kw):
            calls.append(("init", a, kw))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            calls.append(("get", url, kw))
            return responder("GET", url, kw)

        async def post(self, url, **kw):
            calls.append(("post", url, kw))
            return responder("POST", url, kw)

        async def request(self, method, url, **kw):
            calls.append(("request", method, url, kw))
            return responder(method, url, kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_as_metadata_injects_registration_endpoint(cfg, monkeypatch):
    calls: list = []
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(data={"issuer": "https://i.example/"}), calls)
    r = _client().get("/.well-known/oauth-authorization-server")
    assert r.json() == {"issuer": "https://i.example/", "registration_endpoint": f"{ISSUER}/oauth2/register"}
    assert calls[1] == ("get", f"{HYDRA}/.well-known/openid-configuration", {"timeout": 5.0})


def test_as_metadata_fallback_when_hydra_down(cfg, monkeypatch):
    _fake_async_client(monkeypatch, _boom, [])
    cfg["auth"] = {"issuer": "https://fallback.example/"}
    r = _client().get("/.well-known/oauth-authorization-server")
    assert r.json() == {
        "issuer": "https://fallback.example/",
        "registration_endpoint": "https://fallback.example/oauth2/register",
    }


def test_as_metadata_fallback_default_issuer(cfg, monkeypatch):
    _fake_async_client(monkeypatch, _boom, [])
    r = _client().get("/.well-known/oauth-authorization-server")
    assert r.json()["registration_endpoint"] == "https://mcp.example.com/oauth2/register"


GOOD_DCR = {"client_name": "c", "redirect_uris": ["https://claude.ai/cb"], "grant_types": ["authorization_code"]}
BAD_DCR = {**GOOD_DCR, "grant_types": ["client_credentials"]}


def test_dcr_post_injects_audience(cfg, monkeypatch):
    calls: list = []
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(201, {"client_id": "abc"}), calls)
    cfg["auth"] = {"issuer": "https://i.example/"}
    r = _client().post("/oauth2/register", json={**GOOD_DCR, "audience": ["x"]})
    assert r.status_code == 201
    method, url, kw = calls[1][1], calls[1][2], calls[1][3]
    assert (method, url) == ("POST", f"{HYDRA}/oauth2/register")
    assert sorted(kw["json"]["audience"]) == [ISSUER, ISSUER + "/", "x"]
    assert kw["headers"] == {"Content-Type": "application/json"}
    assert kw["timeout"] == 10.0


def test_dcr_post_rejects_bad_grant(cfg, monkeypatch):
    calls: list = []
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(), calls)
    r = _client().post("/oauth2/register", json=BAD_DCR)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"
    assert "client_credentials" in r.json()["error_description"]
    assert calls == []


def test_dcr_post_bad_json_body_treated_as_empty(cfg, monkeypatch):
    calls: list = []
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(201, {"client_id": "abc"}), calls)
    r = _client().post("/oauth2/register", content=b"not json")
    assert r.status_code == 201
    assert calls[1][3]["json"]["audience"]


def test_dcr_post_upstream_error_is_500(cfg, monkeypatch):
    _fake_async_client(monkeypatch, _boom, [])
    r = _client().post("/oauth2/register", json=GOOD_DCR)
    assert r.status_code == 500
    assert r.json() == {"error": "server_error"}


def test_dcr_klient_get_strips_and_returns(cfg, monkeypatch):
    calls: list = []
    raw = {"client_id": "abc", "client_secret": "s"}
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(200, raw), calls)
    r = _client().get("/oauth2/register/abc", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.json() == server_mod._stada_dcr_svar(raw)
    assert calls[1][1:3] == ("GET", f"{HYDRA}/oauth2/register/abc")
    assert calls[1][3]["headers"] == {"Authorization": "Bearer t"}
    assert calls[1][3]["timeout"] == 10.0


def test_dcr_klient_delete_passthrough(cfg, monkeypatch):
    calls: list = []
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(204, content=b"", ctype="text/plain"), calls)
    r = _client().delete("/oauth2/register/abc")
    assert r.status_code == 204
    assert calls[1][1] == "DELETE"
    assert calls[1][3]["headers"] == {}


def test_dcr_klient_get_non_200_passthrough(cfg, monkeypatch):
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(404, content=b'{"e":1}'), [])
    r = _client().get("/oauth2/register/zzz")
    assert r.status_code == 404
    assert r.content == b'{"e":1}'
    assert r.headers["content-type"] == "application/json"


def test_dcr_klient_put_goes_through_vidare(cfg, monkeypatch):
    calls: list = []
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(200, {"client_id": "abc"}), calls)
    r = _client().put("/oauth2/register/abc", json=GOOD_DCR, headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    assert calls[1][1:3] == ("PUT", f"{HYDRA}/oauth2/register/abc")
    assert calls[1][3]["headers"] == {"Content-Type": "application/json", "Authorization": "Bearer t"}


def test_dcr_klient_put_rejected_like_post(cfg, monkeypatch):
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(), [])
    r = _client().put("/oauth2/register/abc", json=BAD_DCR)
    assert r.status_code == 400


def test_dcr_klient_upstream_error_is_500(cfg, monkeypatch):
    _fake_async_client(monkeypatch, _boom, [])
    r = _client().get("/oauth2/register/abc")
    assert r.status_code == 500
    assert r.json() == {"error": "server_error"}


def test_link_start_google_redirect(cfg):
    from urllib.parse import parse_qs, urlparse

    cfg["oauth_providers"] = {"google": {"client_id": "cid", "scopes": ["a", "b"]}}
    cfg["server"] = {"public_url": "https://pub.example/"}
    r = _client().get("/link/google?state=st1", follow_redirects=False)
    assert r.status_code == 307
    u = urlparse(r.headers["location"])
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q == {
        "client_id": "cid",
        "response_type": "code",
        "redirect_uri": "https://pub.example/link/google/callback",
        "state": "st1",
        "scope": "a b",
        "access_type": "offline",
        "prompt": "consent",
    }


def test_link_start_microsoft_default_public_url(cfg):
    r = _client().get("/link/microsoft", follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Flink%2Fmicrosoft%2Fcallback" in loc
    assert "state=&" in loc


def test_link_start_unknown_provider(cfg):
    r = _client().get("/link/dropbox")
    assert r.status_code == 400
    assert r.json() == {"error": "unknown_provider"}


class _Store:
    def __init__(self):
        self.stored = []

    def store(self, *args):
        self.stored.append(args)


@pytest.fixture()
def link_env(cfg, monkeypatch):
    from memaix_gateway.tools import account

    store = _Store()
    monkeypatch.setattr(account, "validate_state", lambda s: {"user_id": "u1"} if s == "good" else None)
    monkeypatch.setattr(server_mod, "_get_token_store", lambda: store)
    monkeypatch.setattr(config_mod, "secret", lambda ref: "sekret")
    cfg["oauth_providers"] = {"google": {"client_id": "cid", "client_secret_ref": "env:X"}}
    cfg["server"] = {"public_url": "https://pub.example/"}
    return store


CALLBACK = "/link/google/callback?code=thecode&state=good"


def test_link_callback_error_param(link_env):
    r = _client().get("/link/google/callback?error=denied&state=good")
    assert r.status_code == 400
    assert r.json() == {"error": "denied"}


def test_link_callback_invalid_state(link_env):
    r = _client().get("/link/google/callback?code=c&state=bad")
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_or_expired_state"}


def test_link_callback_token_exchange_fails(link_env, monkeypatch):
    _fake_async_client(monkeypatch, _boom, [])
    r = _client().get(CALLBACK)
    assert r.status_code == 500
    assert r.json() == {"error": "token_exchange_failed"}
    assert link_env.stored == []


def test_link_callback_success_stores_and_renders(link_env, monkeypatch):
    calls: list = []
    token = {"email": "a<b>@x.se", "access_token": "tok", "expires_in": 100}
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(200, token), calls)
    r = _client().get(CALLBACK)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Google</span> kopplat" in r.text
    assert "a&lt;b&gt;@x.se" in r.text
    assert "<b>" not in r.text
    assert 'href="https://pub.example/board"' in r.text
    assert 'lang="sv"' in r.text
    post = calls[1]
    assert post[1] == server_mod._GOOGLE_TOKEN_URI
    assert post[2]["data"] == {
        "grant_type": "authorization_code",
        "code": "thecode",
        "redirect_uri": "https://pub.example/link/google/callback",
        "client_id": "cid",
        "client_secret": "sekret",
    }
    assert post[2]["data"] is not None
    uid, provider, email, data = link_env.stored[0]
    assert (uid, provider, email) == ("u1", "google", "a<b>@x.se")
    assert data == server_mod._stamp_expiry(token) or data["access_token"] == "tok"


def test_link_callback_unknown_account_label(link_env, monkeypatch):
    _fake_async_client(monkeypatch, lambda m, u, kw: _FakeResp(200, {"access_token": "tok"}), [])
    monkeypatch.setattr(server_mod, "_get_account_email", lambda p, d: "")
    r = _client().get(CALLBACK)
    assert r.status_code == 200
    assert "okänt konto" in r.text


def test_link_callback_http_error_status_is_generic_500(link_env, monkeypatch):
    class _Bad(_FakeResp):
        def raise_for_status(self):
            raise RuntimeError("401")

    _fake_async_client(monkeypatch, lambda m, u, kw: _Bad(401, {}), [])
    r = _client().get(CALLBACK)
    assert r.status_code == 500


class _Hooks:
    def __init__(self):
        self.events = []
        self.results = [object()]


@pytest.fixture()
def hook_env(cfg, monkeypatch):
    import memaix_gateway.rules.engine as engine

    rig = _Hooks()

    def run_rules(rules, acl, event):
        assert (rules, acl) == ("RULES", "ACL")
        rig.events.append(event)
        return rig.results

    monkeypatch.setattr(engine, "evaluate", run_rules)
    monkeypatch.setattr(server_mod, "_get_rules", lambda: "RULES")
    monkeypatch.setattr(server_mod, "_get_acl", lambda: "ACL")
    return rig


def test_hook_header_token_preferred_over_path(hook_env):
    r = _client().post("/hooks/pathtok", json={"b": 1}, headers={"X-Webhook-Token": "hdrtok"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "matched": 1}
    ev = hook_env.events[0]
    assert ev["type"] == "webhook"
    assert ev["project"] is None
    assert ev["payload"] == {"b": 1, "token": "hdrtok"}
    assert ev["id"].startswith("webhook:hdrtok:")
    assert len(ev["id"].split(":")[2]) == 16


def test_hook_path_token_and_idempotent_id(hook_env):
    client = _client()
    client.post("/hooks/t1", json={"a": 1, "b": 2})
    client.post("/hooks/t1", json={"b": 2, "a": 1})
    assert hook_env.events[0]["id"] == hook_env.events[1]["id"]
    assert hook_env.events[0]["payload"]["token"] == "t1"


def test_hook_missing_token_401(hook_env):
    r = _client().post("/hooks", json={})
    assert r.status_code == 401
    assert r.json() == {"error": "missing webhook token"}
    assert hook_env.events == []


def test_hook_bad_body_treated_as_empty(hook_env):
    r = _client().post("/hooks/t1", content=b"zzz")
    assert r.status_code == 200
    assert hook_env.events[0]["payload"] == {"token": "t1"}


def test_hook_no_matching_rule_404(hook_env):
    hook_env.results = []
    r = _client().post("/hooks/t1", json={})
    assert r.status_code == 404
    assert r.json() == {"error": "no matching enabled rule for this token"}


def test_hook_rate_limited_429(hook_env, monkeypatch):
    seen = []

    class _Limiter:
        def check(self, key, *, limit, window_s):
            seen.append((key, limit, window_s))
            return False

    monkeypatch.setattr(server_mod, "_rate_limiter", _Limiter())
    r = _client().post("/hooks/t1", json={})
    assert r.status_code == 429
    assert r.json() == {"error": "rate_limited"}
    assert seen == [("webhook:testclient", 30, 60)]
    assert hook_env.events == []


def test_hook_get_not_allowed(hook_env):
    assert _client().get("/hooks/t1").status_code == 405
