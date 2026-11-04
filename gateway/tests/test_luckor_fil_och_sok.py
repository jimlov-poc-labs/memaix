# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression: kill-switch on search, symlinks out of the vault, WebDAV paths."""

from __future__ import annotations

import pytest

from memaix_gateway.acl import AccessDenied, Acl
from memaix_gateway.connectors.adapters.files_webdav import WebDavFilesAdapter
from memaix_gateway.search.embedder import FakeEmbedder
from memaix_gateway.search.index import index_upsert, reindex_project
from memaix_gateway.search.query import search_all
from memaix_gateway.search.store import EmbeddingStore
from memaix_gateway.tools import files


# ---- 1. disabled user on every search path --------------------------------

def _acl(disabled: bool) -> Acl:
    return Acl(
        users={"u": {"grants": {"proj": "owner"}, "disabled": disabled}},
        projects={"proj": {"vault": "/v"}},
    )


@pytest.fixture()
def store(tmp_path):
    s = EmbeddingStore.for_path(tmp_path / "i.db")
    index_upsert(s, None, "proj", "memory", "n.md", "n.md", "invoice overdue")
    return s


def test_search_all_denies_disabled(store):
    with pytest.raises(AccessDenied):
        search_all(_acl(True), "u", None, store, None, "invoice")


def test_search_all_active_still_works(store):
    res = search_all(_acl(False), "u", None, store, None, "invoice")
    assert [r["ref"] for r in res["results"]] == ["n.md"]


@pytest.mark.parametrize("val", [True, "true", 1])
def test_visible_projects_empty_for_disabled(val):
    acl = Acl(users={"u": {"grants": {"proj": "owner"}, "disabled": val}}, projects={"proj": {}})
    assert acl.visible_projects("u") == []


def test_visible_projects_active():
    assert _acl(False).visible_projects("u") == ["proj"]


def test_search_status_denies_disabled(monkeypatch):
    from memaix_gateway import server

    monkeypatch.setattr(server, "_user", lambda: "u")
    monkeypatch.setattr(server, "_get_acl", lambda: _acl(True))
    monkeypatch.setattr(server, "_get_search_store", lambda: pytest.fail("index touched"))
    monkeypatch.setattr(server, "_get_search_embedder", lambda: None)
    with pytest.raises(AccessDenied):
        server.search_status()


# ---- 2. symlinks leaving the vault ----------------------------------------

@pytest.fixture()
def vault_with_escape(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    out = tmp_path / "outside"
    out.mkdir()
    (out / "secret.txt").write_text("TOPSECRET outside")
    (v / "ok.md").write_text("TOPSECRET inside")
    (v / "filelink.txt").symlink_to(out / "secret.txt")
    (v / "dirlink").symlink_to(out, target_is_directory=True)
    acl = Acl(users={"c": {"grants": {"p": "owner"}}}, projects={"p": {"vault": str(v)}})
    return acl, v


def test_search_files_ignores_symlinks_out_of_vault(vault_with_escape):
    acl, _ = vault_with_escape
    paths = [h["path"] for h in files.search_files(acl, "c", "p", "topsecret", "/")]
    assert paths == ["ok.md"]


def test_reindex_ignores_symlinks_out_of_vault(vault_with_escape, tmp_path):
    acl, _ = vault_with_escape
    s = EmbeddingStore.for_path(tmp_path / "i.db")
    reindex_project(s, FakeEmbedder(dim=16), acl, "p")
    refs = {h["ref"] for h in s.fts_search(["p"], ["file"], "TOPSECRET", 10)}
    assert refs == {"ok.md"}


# ---- 3. WebDAV path hardening ---------------------------------------------

class _Resp:
    text = "ok"
    content = b""

    def raise_for_status(self):
        pass


class _Http:
    def __init__(self):
        self.urls = []

    def request(self, method, url, **kw):
        self.urls.append(url)
        return _Resp()


@pytest.mark.parametrize("bad", [
    "a\\b", "..\\x", "a/../b", "../a", "a\x00b", "a\nb", "./a", "a/./b", "a//b",
    "x" * 1025, "", "/", "//",
])
def test_webdav_rejects_bad_paths_for_read_and_write(bad):
    http = _Http()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=http)
    for call in (ad.read_file, lambda p: ad.write_file(p, "x")):
        with pytest.raises(ValueError):
            call(bad)
    assert http.urls == []


@pytest.mark.parametrize("bad", ["a\\b", "a/../b", "a//b", "./a", "a\x00b", "x" * 1025])
def test_webdav_rejects_bad_paths_for_listing(bad):
    http = _Http()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=http)
    with pytest.raises(ValueError):
        ad.list_files(bad)
    assert http.urls == []


@pytest.mark.parametrize("raw,enc", [
    ("a?x=1", "a%3Fx%3D1"), ("a#frag", "a%23frag"), ("100% done/a.md", "100%25%20done/a.md"),
    ("%2e%2e/x", "%252e%252e/x"), ("a%2fb", "a%252fb"),
])
def test_webdav_encodes_literal_special_chars(raw, enc):
    http = _Http()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=http)
    ad.read_file(raw)
    ad.write_file(raw, "x")
    assert http.urls == ["https://h/dav/" + enc] * 2


def test_webdav_encodes_unicode_and_space():
    http = _Http()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=http)
    ad.read_file("/my dir/\u00e5\u00e4\u00f6.txt")
    assert http.urls == ["https://h/dav/my%20dir/%C3%A5%C3%A4%C3%B6.txt"]


def test_webdav_only_listing_may_address_the_root():
    http = _Http()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=http)
    with pytest.raises(Exception):
        ad.list_files("")  # fake body is not XML; the URL is what matters
    assert http.urls == ["https://h/dav/"]


def _multistatus(*items):
    rows = ""
    for href, is_dir, size in items:
        rt = "<d:collection/>" if is_dir else ""
        sz = f"<d:getcontentlength>{size}</d:getcontentlength>" if size is not None else ""
        rows += (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                 f"<d:resourcetype>{rt}</d:resourcetype>{sz}</d:prop></d:propstat></d:response>")
    return f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">{rows}</d:multistatus>'


class _FakeDav:
    """Tiny in-memory WebDAV keyed by the *encoded* URL path."""

    def __init__(self):
        self.listings = {
            "": _multistatus(("/dav/", True, None), ("/dav/100%25%20done/", True, None)),
            "100%25%20done": _multistatus(
                ("/dav/100%25%20done/", True, None), ("/dav/100%25%20done/a%23b%3F.md", False, 11),
                ("/dav/100%25%20done/broken/", True, None),
            ),
        }
        self.files = {"100%25%20done/a%23b%3F.md": "hello needle"}
        self.urls = []

    def request(self, method, url, **kw):
        self.urls.append((method, url))
        key = url.removeprefix("https://h/dav/").rstrip("/")
        resp = _Resp()
        if method == "PROPFIND":
            if key not in self.listings:
                raise ValueError("server error")  # an unreadable directory
            resp.text = self.listings[key]
        elif method == "PUT":
            self.files[key] = kw["data"].decode()
        else:
            resp.text = self.files[key]
        return resp


def test_webdav_roundtrip_literal_specials_list_read_write_search():
    dav = _FakeDav()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=dav)
    assert ad.list_files("/") == [{"name": "100% done", "type": "dir", "size": None}]
    assert ad.list_files("/100% done") == [
        {"name": "broken", "type": "dir", "size": None},
        {"name": "a#b?.md", "type": "file", "size": 11},
    ]
    assert ad.read_file("/100% done/a#b?.md") == "hello needle"
    ad.write_file("/100% done/new#.md", "x")
    assert "100%25%20done/new%23.md" in dav.files
    # _walk: the unreadable 'broken' dir is skipped, the rest is still searched
    hits = ad.search_files("needle", "/")
    assert [h["path"] for h in hits] == ["/100% done/a#b?.md"]


def test_webdav_search_bad_start_path_raises():
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=_FakeDav())
    with pytest.raises(ValueError):
        ad.search_files("x", "a/../b")


# ---- web API: disabled user gets 403, not 500 ------------------------------

def test_web_search_api_disabled_user_is_403(monkeypatch, tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from memaix_gateway import server
    from memaix_gateway.web import routes as web_routes_mod

    acl = _acl(True)
    monkeypatch.setattr(web_routes_mod, "_get_acl", lambda: acl)
    monkeypatch.setattr(web_routes_mod, "_require_user", lambda request: "u")
    store = EmbeddingStore.for_path(tmp_path / "i.db")
    monkeypatch.setattr(server, "_get_search_store", lambda: store)
    monkeypatch.setattr(server, "_get_search_embedder", lambda: None)
    client = TestClient(Starlette(routes=web_routes_mod.web_routes))
    r = client.get("/app/api/search?q=invoice")
    assert r.status_code == 403
    assert r.json() == {"error": "forbidden"}
