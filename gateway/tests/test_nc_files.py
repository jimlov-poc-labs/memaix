# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the WebDAV FilesBackend adapter — FEATURE-NEXTCLOUD-BACKEND.md §4."""

from __future__ import annotations

import pytest

from memaix_gateway.connectors.adapters.files_webdav import WebDavFilesAdapter

ROOT_PROPFIND = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/dav/files/alice/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/files/alice/notes.txt</d:href>
    <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>13</d:getcontentlength></d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/files/alice/Contracts/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
  </d:response>
</d:multistatus>
"""

SUBDIR_PROPFIND = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/dav/files/alice/Contracts/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/files/alice/Contracts/acme.txt</d:href>
    <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>20</d:getcontentlength></d:prop></d:propstat>
  </d:response>
</d:multistatus>
"""


class _FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.headers = {}
        self.content = b""

    def iter_content(self, size):
        yield self.content

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttp:
    def __init__(self, propfind_by_path=None, files=None):
        self.propfind_by_path = propfind_by_path or {}
        self.files = files or {}
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        if method == "PROPFIND":
            # Most-specific (longest, non-empty) key wins so "Contracts" isn't
            # shadowed by the root ("") entry.
            for suffix in sorted(self.propfind_by_path, key=len, reverse=True):
                if suffix and url.rstrip("/").endswith(suffix.rstrip("/")):
                    return _FakeResponse(self.propfind_by_path[suffix])
            return _FakeResponse(self.propfind_by_path.get("", ROOT_PROPFIND))
        if method == "GET":
            for suffix, content in self.files.items():
                if url.endswith(suffix):
                    return _FakeResponse(content)
            return _FakeResponse("", status_code=404)
        if method == "PUT":
            return _FakeResponse("")
        return _FakeResponse("", status_code=405)


@pytest.fixture()
def http():
    return _FakeHttp(
        propfind_by_path={"": ROOT_PROPFIND, "Contracts": SUBDIR_PROPFIND},
        files={"notes.txt": "hello world!", "Contracts/acme.txt": "the ACME budget is 100k"},
    )


@pytest.fixture()
def adapter(http):
    return WebDavFilesAdapter("https://nc.example.com/dav/files/alice/", "alice", "secret", _http=http)


def test_list_files_root(adapter):
    entries = adapter.list_files("/")
    names = {e["name"] for e in entries}
    assert names == {"notes.txt", "Contracts"}
    by_name = {e["name"]: e for e in entries}
    assert by_name["notes.txt"]["type"] == "file"
    assert by_name["notes.txt"]["size"] == 13
    assert by_name["Contracts"]["type"] == "dir"


def test_read_file(adapter):
    assert adapter.read_file("notes.txt") == "hello world!"


def test_write_file_returns_ok(adapter, http):
    result = adapter.write_file("notes.txt", "updated content")
    assert result == "ok: notes.txt"
    assert ("PUT", "https://nc.example.com/dav/files/alice/notes.txt") in http.requests


def test_write_binary_returns_ok(adapter, http):
    result = adapter.write_binary("report.odt", b"\x50\x4b\x03\x04not really a zip but bytes")
    assert result == "ok: report.odt"
    assert ("PUT", "https://nc.example.com/dav/files/alice/report.odt") in http.requests


def test_write_binary_rejects_traversal(adapter):
    with pytest.raises(ValueError):
        adapter.write_binary("../escape.odt", b"pwned")


def test_search_files_finds_match_in_subdir(adapter):
    results = adapter.search_files("budget")
    assert len(results) == 1
    assert results[0]["path"] == "/Contracts/acme.txt"


def test_search_files_no_match(adapter):
    assert adapter.search_files("nonexistent-term") == []


def test_search_files_skips_large_files(http):
    http.propfind_by_path[""] = ROOT_PROPFIND.replace("13", str(300_000))
    adapter = WebDavFilesAdapter("https://nc.example.com/dav/files/alice/", "alice", "secret", _http=http)
    results = adapter.search_files("hello")
    assert results == []


def test_path_traversal_rejected(adapter):
    with pytest.raises(ValueError):
        adapter.read_file("../../etc/passwd")


def test_path_traversal_rejected_on_write(adapter):
    with pytest.raises(ValueError):
        adapter.write_file("../escape.txt", "pwned")


def test_list_files_empty_response_returns_empty_list():
    http = _FakeHttp(propfind_by_path={"": '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>'})
    adapter = WebDavFilesAdapter("https://nc.example.com/dav/", "alice", "secret", _http=http)
    assert adapter.list_files("/") == []


class _BinaryHttp:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content, self.status_code, self.requests = content, status_code, []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        resp = _FakeResponse("", status_code=self.status_code)
        resp.content = self.content
        return resp


def _binary_adapter(http):
    return WebDavFilesAdapter("https://nc.example.com/dav/files/alice/", "alice", "secret", _http=http)


def test_read_binary_returns_raw_bytes_unchanged():
    payload = bytes(range(256))
    http = _BinaryHttp(payload)
    assert _binary_adapter(http).read_binary("/Contracts/a.bin") == payload
    assert http.requests == [("GET", "https://nc.example.com/dav/files/alice/Contracts/a.bin")]


def test_read_binary_rejects_traversal_and_http_errors():
    with pytest.raises(ValueError):
        _binary_adapter(_BinaryHttp(b"")).read_binary("../../etc/passwd")
    with pytest.raises(RuntimeError):
        _binary_adapter(_BinaryHttp(b"", status_code=404)).read_binary("gone.bin")


def test_nc_files_download_allows_readers_and_denies_non_members():
    from memaix_gateway.acl import AccessDenied, Acl
    from memaix_gateway.tools.nc_files import nc_files_download

    class Backend:
        def read_binary(self, path):
            return b"\x00\xff" + path.encode()

    acl = Acl(
        users={"alice": {"grants": {"p": "collaborator"}}, "rita": {"grants": {"p": "reader"}}, "eve": {"grants": {}}},
        projects={"p": {"vault": "/v"}},
    )
    for user in ("alice", "rita"):
        assert nc_files_download(acl, user, "p", "/x", _files=Backend()) == b"\x00\xff/x"
    with pytest.raises(AccessDenied):
        nc_files_download(acl, "eve", "p", "/x", _files=Backend())


def test_read_binary_refuses_oversized_downloads(monkeypatch):
    from memaix_gateway.connectors.adapters import files_webdav as mod

    monkeypatch.setattr(mod, "MAX_DOWNLOAD_BYTES", 10)
    declared = _BinaryHttp(b"x")
    orig = declared.request

    def with_length(method, url, **kw):
        resp = orig(method, url, **kw)
        resp.headers = {"Content-Length": "11"}
        return resp

    declared.request = with_length
    with pytest.raises(mod.FileTooLarge):
        _binary_adapter(declared).read_binary("big.bin")
    with pytest.raises(mod.FileTooLarge):
        _binary_adapter(_BinaryHttp(b"y" * 11)).read_binary("big.bin")
    assert _binary_adapter(_BinaryHttp(b"z" * 10)).read_binary("ok.bin") == b"z" * 10
