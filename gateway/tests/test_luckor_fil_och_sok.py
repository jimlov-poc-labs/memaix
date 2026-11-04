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
    "a?x=1", "a#frag", "%2e%2e/x", "a%2fb", "a%2Fb", "a\\b", "..\\x", "a/../b", "a\x00b", "a\nb",
])
def test_webdav_rejects_url_structure_chars(bad):
    http = _Http()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=http)
    for call in (ad.read_file, lambda p: ad.write_file(p, "x"), ad.list_files):
        with pytest.raises(ValueError):
            call(bad)
    assert http.urls == []


def test_webdav_encodes_safe_but_special_chars():
    http = _Http()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=http)
    ad.read_file("/my dir/åäö.txt")
    assert http.urls == ["https://h/dav/my%20dir/%C3%A5%C3%A4%C3%B6.txt"]


def test_webdav_empty_path_is_root_for_listing_only():
    http = _Http()
    ad = WebDavFilesAdapter("https://h/dav/", "u", "p", _http=http)
    # empty/"/" means the collection root: no extra path appended
    try:
        ad.list_files("")
    except Exception:
        pass
    assert http.urls == ["https://h/dav/"]
