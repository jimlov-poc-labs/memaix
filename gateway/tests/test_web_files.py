# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for the project's Nextcloud files (/app/api/files): auth, project
isolation, path hardening, binary-safe download. Same auth-bypass pattern as
test_web_booking.py — _require_user is patched, the Acl does the real work."""

from __future__ import annotations

import pytest
import requests
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.web import routes as web_routes_mod
from memaix_gateway.web.api import files as files_api

BINARY = bytes(range(256))


class _FakeBackend:
    def __init__(self):
        self.listed: list[str] = []
        self.read: list[str] = []

    def list_files(self, path="/"):
        self.listed.append(path)
        return [
            {"name": "Docs", "type": "dir", "size": None},
            {"name": "a.bin", "type": "file", "size": 256},
        ]

    def read_binary(self, path):
        self.read.append(path)
        return BINARY


@pytest.fixture()
def rig(monkeypatch):
    acl = Acl(
        users={
            "alice": {"grants": {"proj": "collaborator"}},
            "reader": {"grants": {"proj": "reader"}},
            "mallory": {"grants": {"other": "owner"}},
        },
        projects={
            "proj": {"vault": "/v/proj", "files": {"url": "http://nc/dav/", "user": "u", "password_ref": "env:X"}},
            "other": {"vault": "/v/other"},
        },
    )
    backend = _FakeBackend()
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    current = {"user": "alice"}
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: current["user"])
    monkeypatch.setattr(files_api, "_files_backend", _backend_for(acl, backend))
    client = TestClient(Starlette(routes=web_routes_mod.web_routes))
    return client, current, backend


def _backend_for(acl, backend):
    def build(acl_, user, project):
        acl_.enforce(user, project, "collaborator")
        return backend

    return build


# ------------------------------------------------------------------
# Auth + isolation
# ------------------------------------------------------------------


def test_401_without_login(rig, monkeypatch):
    client, _, _ = rig
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: None)
    assert client.get("/app/api/files?project=proj").status_code == 401
    assert client.get("/app/api/files/download?project=proj&path=/a.bin").status_code == 401


def test_member_of_another_project_is_forbidden(rig):
    client, current, backend = rig
    current["user"] = "mallory"
    assert client.get("/app/api/files?project=proj").status_code == 403
    assert client.get("/app/api/files/download?project=proj&path=/a.bin").status_code == 403
    assert backend.listed == [] and backend.read == []


def test_unknown_project_is_forbidden(rig):
    client, _, _ = rig
    assert client.get("/app/api/files?project=nope").status_code == 403


def test_reader_is_refused_like_the_nc_files_tools(rig):
    client, current, _ = rig
    current["user"] = "reader"
    assert client.get("/app/api/files?project=proj").status_code == 403


# ------------------------------------------------------------------
# Listing + download
# ------------------------------------------------------------------


def test_list_root_and_subfolder(rig):
    client, _, backend = rig
    body = client.get("/app/api/files?project=proj").json()
    assert body["path"] == "/"
    assert body["entries"][0] == {"name": "Docs", "type": "dir", "size": None}
    assert client.get("/app/api/files?project=proj&path=/Docs//sub/./x").json()["path"] == "/Docs/sub/x"
    assert backend.listed == ["/", "/Docs/sub/x"]


def test_download_is_binary_safe_attachment(rig):
    client, _, _ = rig
    resp = client.get("/app/api/files/download?project=proj&path=/Docs/a.bin")
    assert resp.status_code == 200
    assert resp.content == BINARY
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith('attachment; filename="a.bin"')


def test_download_filename_is_escaped(rig):
    client, _, _ = rig
    resp = client.get('/app/api/files/download?project=proj&path=/x"y%C3%A5.txt')
    disposition = resp.headers["content-disposition"]
    assert 'filename="x_y_.txt"' in disposition
    assert "filename*=UTF-8''x%22y%C3%A5.txt" in disposition


def test_download_of_root_is_rejected(rig):
    client, _, _ = rig
    assert client.get("/app/api/files/download?project=proj&path=/").status_code == 400


# ------------------------------------------------------------------
# Path hardening
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/..", "/a/../b", "/a/..", "/../etc/passwd", "a/b", "", "/a\\b", "\\..\\x",
        "/a%00b", "/%2e%2e/x", "/a/%2E%2E", "/a%5cb",
    ],
)
def test_hostile_paths_are_rejected_before_the_backend(rig, path):
    client, _, backend = rig
    for url in ("/app/api/files", "/app/api/files/download"):
        resp = client.get(url, params={"project": "proj", "path": path})
        assert resp.status_code == 400, (url, path)
        assert resp.json() == {"error": "bad_path"}
    assert backend.listed == [] and backend.read == []


def test_literal_nul_in_path_is_rejected(rig):
    client, _, _ = rig
    resp = client.get("/app/api/files", params={"project": "proj", "path": "/a\x00b"})
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "raw, expected",
    [("/", "/"), ("/a/b", "/a/b"), ("//a///b/", "/a/b"), ("/a/./b", "/a/b"), ("/..a/b..", "/..a/b..")],
)
def test_clean_path_normalises(raw, expected):
    assert files_api.clean_path(raw) == expected


@pytest.mark.parametrize("raw", [None, 5, "rel", "/a/../b", "/a\\b", "/a\x00"])
def test_clean_path_rejects(raw):
    assert files_api.clean_path(raw) is None


# ------------------------------------------------------------------
# Backend wiring + failures (real registry, faked HTTP)
# ------------------------------------------------------------------


def test_project_without_files_gets_no_files(rig, monkeypatch):
    client, current, _ = rig
    monkeypatch.undo()  # drop the fake backend: use the real registry lookup
    acl = Acl(
        users={"alice": {"grants": {"other": "collaborator"}}},
        projects={"other": {"vault": "/v/other"}},
    )
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: "alice")
    monkeypatch.setattr(files_api, "_token_store", lambda: None)
    resp = client.get("/app/api/files?project=other")
    assert resp.status_code == 404
    assert resp.json() == {"error": "no_files"}


class _Resp:
    def __init__(self, status=200, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content
        self.headers = {}

    def iter_content(self, size):
        yield self.content

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


@pytest.fixture()
def real_backend(monkeypatch):
    acl = Acl(
        users={"alice": {"grants": {"proj": "collaborator"}}},
        projects={"proj": {"vault": "/v", "files": {"url": "http://nc/dav/", "user": "u", "password_ref": "env:NC_PW"}}},
    )
    monkeypatch.setenv("NC_PW", "pw")
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: "alice")
    monkeypatch.setattr(files_api, "_token_store", lambda: None)
    return TestClient(Starlette(routes=web_routes_mod.web_routes))


def test_download_through_real_adapter_keeps_bytes(real_backend, monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["call"] = (method, url)
        return _Resp(content=BINARY)

    monkeypatch.setattr(requests, "request", fake_request)
    resp = real_backend.get("/app/api/files/download?project=proj&path=/d/a.bin")
    assert resp.content == BINARY
    assert seen["call"] == ("GET", "http://nc/dav/d/a.bin")


@pytest.mark.parametrize("status, expected", [(404, 404), (500, 502)])
def test_upstream_errors_are_mapped(real_backend, monkeypatch, status, expected):
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(status=status))
    assert real_backend.get("/app/api/files?project=proj&path=/x").status_code == expected
    assert real_backend.get("/app/api/files/download?project=proj&path=/x").status_code == expected


def test_unreachable_nextcloud_is_502(real_backend, monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "request", boom)
    resp = real_backend.get("/app/api/files?project=proj")
    assert resp.status_code == 502
    assert resp.json() == {"error": "upstream_error"}


# ------------------------------------------------------------------
# Page
# ------------------------------------------------------------------


def test_page_is_served_and_nav_present(rig):
    client, _, _ = rig
    page = client.get("/app/files")
    assert page.status_code == 200
    assert 'id="files-list"' in page.text and 'href="/app/files"' in page.text
    assert "/app/static/files.js" in page.text
