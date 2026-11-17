# SPDX-License-Identifier: AGPL-3.0-or-later
"""PUT /api/files/{project}/{path}: bearer-authenticated script upload.

Real JWTs (RSA key generated here) go through a real HydraTokenVerifier whose
JWKS client is replaced by a stub, so the expired / wrong-audience / wrong-key
cases test the production verifier, not a stand-in. The route is mounted on a
bare Starlette app with no auth middleware, exactly the situation custom routes
are in under FastMCP."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.auth.token import HydraTokenVerifier
from memaix_gateway.connectors.adapters.files_webdav import FileExists, WebDavFilesAdapter
from memaix_gateway.safety.audit import AuditLog
from memaix_gateway.web import routes as web_routes_mod
from memaix_gateway.web.api import files_upload as up

ISS = "https://mcp.example.test"
AUD = "https://mcp.example.test"
CLIENT = "svc-uploader-0001"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 64

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mint(sub=CLIENT, *, aud=AUD, iss=ISS, exp=3600, key=_KEY, drop_exp=False):
    claims = {"sub": sub, "iss": iss, "aud": aud, "iat": int(time.time())}
    if not drop_exp:
        claims["exp"] = int(time.time()) + exp
    return jwt.encode(claims, key, algorithm="RS256")


class _StubJwks:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=_KEY.public_key())


class FakeBackend:
    def __init__(self, label="proj"):
        self.label = label
        self.puts: list[tuple[str, bytes, int]] = []
        self.exists = False
        self.error: Exception | None = None

    def put_stream(self, path, fileobj, size):
        if self.error:
            raise self.error
        if self.exists:
            raise FileExists(path)
        fileobj.seek(0)
        self.puts.append((path, fileobj.read(), size))
        return "ok"


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    acl = Acl.from_config(
        {
            "users": {
                "uploader": {"grants": {"proj": "collaborator", "other": "reader"}},
                "viewer": {"grants": {"proj": "reader"}},
                "root": {"admin": True, "grants": {}},
                "alice": {"oauth_subjects": ["alice"], "grants": {"proj": "owner"}},
            },
            "projects": {"proj": {"vault": "/v/p"}, "other": {"vault": "/v/o"}, "third": {"vault": "/v/t"}},
            "service_clients": {
                CLIENT: {"acts_as": "uploader", "tools": [up.TOOL_NAME], "projects": ["proj", "other"]},
                "svc-no-tool": {"acts_as": "uploader", "tools": ["email_search"], "projects": ["proj"]},
                "svc-empty": {"acts_as": "uploader", "tools": [], "projects": []},
                "svc-reader": {"acts_as": "viewer", "tools": [up.TOOL_NAME], "projects": ["proj"]},
                "svc-admin": {"acts_as": "root", "tools": [up.TOOL_NAME], "projects": ["proj"]},
                "svc-third": {"acts_as": "uploader", "tools": [up.TOOL_NAME], "projects": ["third"]},
            },
        }
    )
    AuditLog._clear_instances()
    audit = AuditLog.for_path(tmp_path / "audit.db")
    monkeypatch.setattr(server, "_acl", acl)
    monkeypatch.setattr(server, "_audit", audit)
    monkeypatch.setattr(server, "_get_token_store", lambda: None)
    server._rate_limiter._reset()
    verifier = HydraTokenVerifier("http://jwks.invalid/jwks.json", ISS, [AUD, AUD + "/"])
    verifier._jwks_client = _StubJwks()
    monkeypatch.setattr(server.mcp, "_token_verifier", verifier, raising=False)
    backend = FakeBackend()
    used: list[tuple[str, str]] = []

    class Reg:
        def get(self, acl_, store, project, capability, user):
            used.append((project, user))
            assert capability == "files"
            return backend

    monkeypatch.setattr(up, "_registry", lambda: Reg())
    client = TestClient(Starlette(routes=web_routes_mod.web_routes))
    return SimpleNamespace(client=client, backend=backend, used=used, audit=audit, acl=acl, monkeypatch=monkeypatch)


def put(rig, path="proj/a.png", body=PNG, token=None, headers=None):
    h = {} if token is None else {"Authorization": f"Bearer {token}"}
    h.update(headers or {})
    return rig.client.put(f"/api/files/{path}", content=body, headers=h)


def ok_put(rig, path="proj/a.png", body=PNG, **kw):
    return put(rig, path, body, token=mint(), **kw)


def audit_rows(rig):
    return rig.audit._conn.execute("SELECT user, project, tool, ok, detail FROM audit").fetchall()


# ------------------------------------------------------------------ happy path


def test_upload_ok_returns_size_and_sha256(rig):
    r = ok_put(rig)
    assert r.status_code == 201
    assert r.json() == {"project": "proj", "path": "a.png", "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}
    assert rig.backend.puts == [("a.png", PNG, len(PNG))]
    rows = audit_rows(rig)
    assert rows[-1][:4] == ("uploader", "proj", "nc_files_upload", 1)


@pytest.mark.parametrize(
    "name,body",
    [("a.png", PNG), ("a.JPG", JPG), ("a.jpeg", JPG), ("a.webp", WEBP), ("a.gif", GIF), ("a.PDF", PDF)],
)
def test_all_allowed_types(rig, name, body):
    assert ok_put(rig, f"proj/dir/{name}", body).status_code == 201
    assert rig.backend.puts[-1][0] == f"dir/{name}"


def test_dot_segment_normalised_same_as_plain():
    assert up.normalise_path("./a.png") == up.normalise_path("a.png") == "a.png"
    assert up.normalise_path("d//./e/a.png") == "d/e/a.png"


# ------------------------------------------------------------------ authentication


def test_no_token_401(rig):
    assert put(rig).status_code == 401
    assert rig.backend.puts == []


def test_wrong_scheme_and_empty_bearer_401(rig):
    assert put(rig, headers={"Authorization": "Basic abc"}).status_code == 401
    assert put(rig, headers={"Authorization": "Bearer "}).status_code == 401


def test_garbage_token_401(rig):
    assert put(rig, token="not.a.jwt").status_code == 401


def test_expired_token_401(rig):
    assert put(rig, token=mint(exp=-60)).status_code == 401


def test_token_without_exp_401(rig):
    assert put(rig, token=mint(drop_exp=True)).status_code == 401


def test_wrong_audience_401(rig):
    assert put(rig, token=mint(aud="https://elsewhere.example")).status_code == 401


def test_wrong_issuer_401(rig):
    assert put(rig, token=mint(iss="https://evil.example")).status_code == 401


def test_token_signed_with_other_key_401(rig):
    assert put(rig, token=mint(key=_OTHER_KEY)).status_code == 401


def test_no_verifier_configured_denies(rig):
    rig.monkeypatch.setattr(server.mcp, "_token_verifier", None, raising=False)
    assert put(rig, token=mint()).status_code == 401


def test_verifier_without_audience_denies(rig):
    v = HydraTokenVerifier("http://jwks.invalid/jwks.json", ISS, None)
    v._jwks_client = _StubJwks()
    rig.monkeypatch.setattr(server.mcp, "_token_verifier", v, raising=False)
    assert put(rig, token=mint(aud="anything")).status_code == 401


# ------------------------------------------------------------------ authorisation


def test_plain_user_token_without_service_client_403(rig):
    r = put(rig, token=mint(sub="alice"))
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert rig.backend.puts == []
    assert audit_rows(rig)[-1][3] == 0


def test_unknown_subject_403(rig):
    assert put(rig, token=mint(sub="nobody")).status_code == 403


def test_service_client_without_pseudo_tool_403(rig):
    assert put(rig, token=mint(sub="svc-no-tool")).status_code == 403


def test_service_client_with_empty_lists_403(rig):
    assert put(rig, token=mint(sub="svc-empty")).status_code == 403


def test_project_not_in_client_projects_403(rig):
    # the acts_as user HAS collaborator there; only the client allowlist stops it
    rig.acl.users["uploader"]["grants"]["third"] = "collaborator"
    assert ok_put(rig, "third/a.png").status_code == 403
    assert rig.used == []


def test_unknown_project_403(rig):
    assert ok_put(rig, "nope/a.png").status_code == 403


def test_reader_role_403(rig):
    r = put(rig, token=mint(sub="svc-reader"))
    assert r.status_code == 403 and rig.backend.puts == []


def test_acts_as_reader_in_listed_project_403(rig):
    # svc-uploader is allowed in 'other' but its acts_as user only reads there
    assert ok_put(rig, "other/a.png").status_code == 403
    assert rig.backend.puts == []


def test_disabled_acts_as_403(rig):
    rig.acl.users["uploader"]["disabled"] = True
    assert ok_put(rig).status_code == 403


def test_authorisation_before_body_read_and_audit_has_no_content(rig):
    assert put(rig, token=mint(sub="alice")).status_code == 403
    assert all(PNG.hex() not in str(row) for row in audit_rows(rig))


# ------------------------------------------------------------------ size and framing


def test_content_length_too_large_413_without_reading(rig):
    r = ok_put(rig, body=PNG, headers={"Content-Length": str(up.MAX_UPLOAD_BYTES + 1)})
    assert r.status_code == 413 and rig.used == []


def test_streamed_body_over_cap_413(rig):
    big = PNG + b"\x00" * (up.MAX_UPLOAD_BYTES + 10)
    r = ok_put(rig, body=big, headers={"Content-Length": str(up.MAX_UPLOAD_BYTES)})
    assert r.status_code == 413 and rig.backend.puts == []


def test_body_longer_than_declared_413(rig):
    r = ok_put(rig, body=PNG, headers={"Content-Length": "20"})
    assert r.status_code == 413 and rig.backend.puts == []


def test_body_shorter_than_declared_400(rig):
    r = ok_put(rig, body=PNG, headers={"Content-Length": str(len(PNG) + 50)})
    assert r.status_code == 400 and r.json() == {"error": "bad_length"}
    assert rig.backend.puts == []


def test_missing_content_length_411(rig):
    r = rig.client.put(
        "/api/files/proj/a.png", content=(c for c in [PNG]), headers={"Authorization": f"Bearer {mint()}"}
    )
    assert r.status_code == 411 and rig.backend.puts == []


def test_bad_content_length_400(rig):
    assert ok_put(rig, headers={"Content-Length": "abc"}).status_code == 400


def test_empty_file_400(rig):
    r = ok_put(rig, body=b"")
    assert r.status_code == 400 and r.json() == {"error": "empty_file"}


# ------------------------------------------------------------------ path and content


def test_wrong_magic_415(rig):
    r = ok_put(rig, "proj/a.png", JPG)
    assert r.status_code == 415 and rig.backend.puts == []


def test_short_garbage_with_valid_extension_415(rig):
    assert ok_put(rig, "proj/a.pdf", b"hello").status_code == 415


def test_webp_needs_both_riff_and_webp(rig):
    assert ok_put(rig, "proj/a.webp", b"RIFF\x00\x00\x00\x00WAVEfmt ").status_code == 415


@pytest.mark.parametrize("name", ["a.exe", "a", "a.png.exe", "a.exe.png", "a.tar.pdf", ".png", "a.svg", "a.png.", "a.html"])
def test_bad_extension_400(rig, name):
    r = ok_put(rig, f"proj/{name}", PNG)
    assert r.status_code == 400 and r.json() == {"error": "bad_path"}
    assert rig.backend.puts == []


def test_traversal_percent_encoded_400(rig):
    r = ok_put(rig, "proj/%2e%2e/x.png")
    assert r.status_code == 400 and r.json() == {"error": "bad_path"}


def test_traversal_in_the_middle_400(rig):
    assert ok_put(rig, "proj/a/%2e%2e/%2e%2e/x.png").status_code == 400


def test_absolute_path_400(rig):
    r = ok_put(rig, "proj//etc/x.png")
    assert r.status_code == 400 and r.json() == {"error": "bad_path"}


def test_backslash_nul_and_hidden_400(rig):
    assert ok_put(rig, "proj/a%5Cb.png").status_code == 400
    assert ok_put(rig, "proj/a%00.png").status_code == 400
    assert ok_put(rig, "proj/.hidden/a.png").status_code == 400


def test_only_path_directory_is_not_a_file(rig):
    assert ok_put(rig, "proj/").status_code in (400, 404, 405)
    assert rig.backend.puts == []


# ------------------------------------------------------------------ overwrite, upstream, limits


def test_existing_file_409(rig):
    rig.backend.exists = True
    r = ok_put(rig)
    assert r.status_code == 409 and r.json() == {"error": "exists"}
    assert audit_rows(rig)[-1][3] == 0


def test_upstream_error_502_without_detail(rig):
    rig.backend.error = requests.ConnectionError("http://nc.internal/secret-path user:pw")
    r = ok_put(rig)
    assert r.status_code == 502 and "secret" not in r.text and "internal" not in r.text


def test_unexpected_error_500_without_detail(rig):
    rig.backend.error = RuntimeError("password=hunter2")
    r = ok_put(rig)
    assert r.status_code == 500 and "hunter2" not in r.text
    assert all("hunter2" not in str(row) for row in audit_rows(rig))


def test_project_without_files_area_404(rig):
    rig.monkeypatch.setattr(up, "_files_backend", lambda acl, user, project: None)
    assert ok_put(rig).status_code == 404


def test_rate_limit_after_ten(rig):
    codes = [ok_put(rig, f"proj/f{i}.png").status_code for i in range(12)]
    assert codes[:10] == [201] * 10
    assert codes[10:] == [429, 429]


def test_rate_limit_is_per_client(rig):
    for i in range(10):
        ok_put(rig, f"proj/f{i}.png")
    assert put(rig, "proj/z.png", token=mint(sub="svc-reader")).status_code == 403  # other client, not 429
    assert ok_put(rig, "proj/zz.png").status_code == 429


def test_failed_auth_does_not_use_rate_budget(rig):
    for _ in range(15):
        put(rig, token="junk")
    assert ok_put(rig).status_code == 201


# ------------------------------------------------------------------ isolation through the real registry


def test_isolation_uses_only_the_requested_projects_account(rig, monkeypatch):
    rig.acl.projects["proj"]["files"] = {"type": "webdav", "url": "http://nc.test/dav/proj/", "user": "acct-proj", "password_ref": "env:UPLOAD_TEST_PW_P"}
    rig.acl.projects["other"]["files"] = {"type": "webdav", "url": "http://nc.test/dav/other/", "user": "acct-other", "password_ref": "env:UPLOAD_TEST_PW_O"}
    rig.acl.users["uploader"]["grants"]["other"] = "collaborator"
    monkeypatch.setenv("UPLOAD_TEST_PW_P", "pw-p")
    monkeypatch.setenv("UPLOAD_TEST_PW_O", "pw-o")
    from memaix_gateway.connectors import registry as reg

    monkeypatch.setattr(up, "_registry", reg.default_registry)
    seen: list[tuple[str, str, tuple, dict]] = []

    def fake_request(method, url, **kw):
        seen.append((method, url, kw.get("auth"), {k: v for k, v in kw.items() if k in ("headers", "timeout")}))
        return SimpleNamespace(status_code=201, raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "request", fake_request)
    assert ok_put(rig, "proj/d/a.png").status_code == 201
    assert ok_put(rig, "other/b.png").status_code == 201
    proj = [s for s in seen if "/dav/proj/" in s[1]]
    other = [s for s in seen if "/dav/other/" in s[1]]
    assert proj and other and len(proj) + len(other) == len(seen)
    assert {s[2] for s in proj} == {("acct-proj", "pw-p")}
    assert {s[2] for s in other} == {("acct-other", "pw-o")}
    puts = [s for s in seen if s[0] == "PUT"]
    assert all(s[3]["headers"]["If-None-Match"] == "*" for s in puts)
    assert all(s[3]["timeout"][1] > 15 for s in puts)


def test_backend_resolved_for_acts_as_in_requested_project(rig):
    ok_put(rig, "proj/a.png")
    assert rig.used == [("proj", "uploader")]


# ------------------------------------------------------------------ adapter put_stream


class FakeHttp:
    def __init__(self, put_status=201, mkcol=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.put_status = put_status
        self.mkcol = mkcol or {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        status = self.mkcol.get(url, 201) if method == "MKCOL" else self.put_status

        def raise_for_status():
            if status >= 400:
                raise requests.HTTPError(f"{status}")

        return SimpleNamespace(status_code=status, raise_for_status=raise_for_status)


def _spool(data=b"abc"):
    import io

    return io.BytesIO(data)


def test_put_stream_mkcols_parents_then_puts_without_overwrite():
    http = FakeHttp(mkcol={"http://nc/dav/a": 405})
    ad = WebDavFilesAdapter("http://nc/dav", "u", "p", _http=http)
    ad.put_stream("/a/b/c.png", _spool(), 3)
    assert [(m, u) for m, u, _ in http.calls] == [("MKCOL", "http://nc/dav/a"), ("MKCOL", "http://nc/dav/a/b"), ("PUT", "http://nc/dav/a/b/c.png")]
    put_kw = http.calls[-1][2]
    assert put_kw["headers"] == {"If-None-Match": "*", "Content-Length": "3"}
    assert put_kw["timeout"] != 15


def test_put_stream_412_is_file_exists():
    ad = WebDavFilesAdapter("http://nc/dav", "u", "p", _http=FakeHttp(put_status=412))
    with pytest.raises(FileExists):
        ad.put_stream("a.png", _spool(), 3)


def test_put_stream_other_error_raises_and_bad_path_rejected():
    ad = WebDavFilesAdapter("http://nc/dav", "u", "p", _http=FakeHttp(put_status=500))
    with pytest.raises(requests.HTTPError):
        ad.put_stream("a.png", _spool(), 3)
    with pytest.raises(ValueError):
        ad.put_stream("../a.png", _spool(), 3)


# ------------------------------------------------------------------ review round 1


def test_admin_acts_as_denied_even_with_tools_and_projects(rig):
    r = put(rig, token=mint(sub="svc-admin"))
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert rig.backend.puts == [] and rig.used == []
    assert "admin" in audit_rows(rig)[-1][4] and audit_rows(rig)[-1][3] == 0


def test_alg_none_token_401(rig):
    token = jwt.encode({"sub": CLIENT, "iss": ISS, "aud": AUD, "exp": int(time.time()) + 600}, None, algorithm="none")
    assert put(rig, token=token).status_code == 401


def test_hs256_token_401(rig):
    token = jwt.encode(
        {"sub": CLIENT, "iss": ISS, "aud": AUD, "exp": int(time.time()) + 600}, "x" * 40, algorithm="HS256"
    )
    assert put(rig, token=token).status_code == 401


def test_get_and_post_405(rig):
    h = {"Authorization": f"Bearer {mint()}"}
    assert rig.client.get("/api/files/proj/a.png", headers=h).status_code == 405
    assert rig.client.post("/api/files/proj/a.png", content=PNG, headers=h).status_code == 405


def test_throttled_and_unauthorised_flood_does_not_fill_audit(rig):
    for _ in range(30):
        put(rig, token=mint(sub="alice"))  # valid token, not a service client
    denied = [r for r in audit_rows(rig) if r[3] == 0]
    assert len(denied) == up.RATE_LIMIT  # the rest were throttled and not audited
    assert put(rig, token=mint(sub="alice")).status_code == 429


def _async_client(rig):
    import httpx

    app = Starlette(routes=web_routes_mod.web_routes)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_slow_body_times_out_408_and_is_audited(rig, monkeypatch):
    monkeypatch.setattr(up, "BODY_TIMEOUT_S", 0.2)

    async def hang():
        yield PNG[:10]
        await asyncio.sleep(5)
        yield PNG[10:]

    async def run():
        async with _async_client(rig) as c:
            return await c.put(
                "/api/files/proj/a.png",
                content=hang(),
                headers={"Authorization": f"Bearer {mint()}", "Content-Length": str(len(PNG))},
            )

    r = asyncio.run(run())
    assert r.status_code == 408 and r.json() == {"error": "timeout"}
    assert rig.backend.puts == []
    assert audit_rows(rig)[-1][3:] == (0, "denied: timeout")


def test_concurrency_cap_fifth_gets_503_and_slots_are_released(rig, monkeypatch):
    gate = threading.Event()
    entered = threading.Semaphore(0)
    original = rig.backend.put_stream

    def blocking(path, fileobj, size):
        entered.release()
        gate.wait(10)
        return original(path, fileobj, size)

    rig.backend.put_stream = blocking

    async def one(c, i):
        return await c.put(
            f"/api/files/proj/f{i}.png", content=PNG, headers={"Authorization": f"Bearer {mint()}"}
        )

    async def run():
        async with _async_client(rig) as c:
            tasks = [asyncio.create_task(one(c, i)) for i in range(up.MAX_CONCURRENT_UPLOADS)]
            for _ in range(up.MAX_CONCURRENT_UPLOADS):
                assert await asyncio.to_thread(entered.acquire, True, 5)
            fifth = await one(c, 99)
            gate.set()
            first = await asyncio.gather(*tasks)
            after = await one(c, 100)  # every slot was released
            return fifth, first, after

    fifth, first, after = asyncio.run(run())
    assert fifth.status_code == 503 and fifth.headers["Retry-After"] == "5"
    assert [r.status_code for r in first] == [201] * up.MAX_CONCURRENT_UPLOADS
    assert after.status_code == 201


def test_slot_released_when_backend_raises(rig, monkeypatch):
    rig.backend.error = requests.ConnectionError("down")

    async def run():
        async with _async_client(rig) as c:
            codes = []
            for i in range(up.MAX_CONCURRENT_UPLOADS + 2):
                r = await c.put(
                    f"/api/files/proj/f{i}.png", content=PNG, headers={"Authorization": f"Bearer {mint()}"}
                )
                codes.append(r.status_code)
            return codes

    assert asyncio.run(run()) == [502] * (up.MAX_CONCURRENT_UPLOADS + 2)


class _SpoolSpy:
    """Records every spool the route creates so tests can assert it was closed."""

    def __init__(self, monkeypatch):
        self.spools = []
        real = up.tempfile.SpooledTemporaryFile

        def make(*a, **kw):
            sp = real(*a, **kw)
            self.spools.append(sp)
            return sp

        monkeypatch.setattr(up.tempfile, "SpooledTemporaryFile", make)

    def all_closed(self):
        return bool(self.spools) and all(sp.closed for sp in self.spools)


def test_spool_closed_on_timeout_path(rig, monkeypatch):
    spy = _SpoolSpy(monkeypatch)
    monkeypatch.setattr(up, "BODY_TIMEOUT_S", 0.2)

    async def hang():
        yield PNG[:10]
        await asyncio.sleep(5)

    async def run():
        async with _async_client(rig) as c:
            return await c.put(
                "/api/files/proj/a.png",
                content=hang(),
                headers={"Authorization": f"Bearer {mint()}", "Content-Length": str(len(PNG))},
            )

    assert asyncio.run(run()).status_code == 408
    assert spy.all_closed()


class _Locked:
    def locked(self):
        return True


def test_full_slots_503_before_body_is_read(rig, monkeypatch):
    spy = _SpoolSpy(monkeypatch)
    called = []

    async def forbidden(*a, **kw):
        called.append(1)
        raise AssertionError("body must not be read")

    monkeypatch.setattr(up, "_spool_body", forbidden)
    monkeypatch.setattr(up, "_slot_semaphore", lambda: _Locked())
    r = ok_put(rig)
    assert r.status_code == 503 and r.headers["Retry-After"] == "5" and r.json() == {"error": "busy"}
    assert called == [] and spy.spools == [] and rig.backend.puts == []
    assert audit_rows(rig)[-1][3:] == (0, "denied: busy")


def test_slots_filled_during_streaming_503_and_spool_closed(rig, monkeypatch):
    spy = _SpoolSpy(monkeypatch)
    answers = iter([False, True])  # free at the early check, full at the late one

    class Flip:
        def locked(self):
            return next(answers)

    monkeypatch.setattr(up, "_slot_semaphore", lambda: Flip())
    r = ok_put(rig)
    assert r.status_code == 503 and rig.backend.puts == []
    assert spy.all_closed()
