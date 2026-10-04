# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebDAV FilesBackend — Nextcloud (or any WebDAV server) file storage,
behind the connector framework (FEATURE-NEXTCLOUD-BACKEND.md §4).

An *additional* files source, not a replacement for the local vault
(tools/files.py) — see connectors/registry.py's RESOURCE_KEYS note. Exposed
via the nc_files_* MCP tools, never the existing files_* ones.

Listing/search use PROPFIND + local filtering (the same "fetch, then filter
in Python" shape connectors/adapters/contacts_carddav.py uses) rather than
a server-side search REPORT — simple and testable without a real Nextcloud
in CI. Every path is validated with paths.validate_relative_path before it
reaches an HTTP call — traversal ("../") is rejected the same way it would
be for the local vault.
"""

from __future__ import annotations

from urllib.parse import unquote

import defusedxml.ElementTree as ET

from ...paths import validate_relative_path

# Search skips any file above this size — avoids downloading huge binaries
# just to grep them (FEATURE-NEXTCLOUD-BACKEND.md §4 acceptance criteria).
SEARCH_MAX_BYTES = 200_000


MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


class FileTooLarge(ValueError):
    """Download exceeds MAX_DOWNLOAD_BYTES."""


def _safe_rel(path: str) -> str:
    p = (path or "/").strip("/")
    return validate_relative_path(p) if p else ""


class WebDavFilesAdapter:
    """Implements connectors.base.FilesBackend against a WebDAV collection."""

    def __init__(self, base_url: str, username: str, password: str, *, _http=None) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._username = username
        self._password = password
        self._http = _http  # injected for tests: object with .request(method, url, **kw)

    def _request(self, method: str, path: str = "", **kwargs):
        url = self._base_url + path if path else self._base_url
        if self._http is not None:
            return self._http.request(method, url, **kwargs)
        import requests

        return requests.request(method, url, auth=(self._username, self._password), timeout=15, **kwargs)

    def list_files(self, path: str = "/") -> list[dict]:
        rel = _safe_rel(path)
        body = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
            "<d:prop><d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>"
        )
        resp = self._request("PROPFIND", rel, headers={"Depth": "1", "Content-Type": "application/xml"}, data=body)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)

        parsed = []
        for response in root:
            if not response.tag.endswith("response"):
                continue
            href_el = next((e for e in response.iter() if e.tag.endswith("href")), None)
            if href_el is None or not href_el.text:
                continue
            href = unquote(href_el.text.rstrip("/"))
            is_dir = any(e.tag.endswith("collection") for e in response.iter())
            size_el = next((e for e in response.iter() if e.tag.endswith("getcontentlength")), None)
            size = int(size_el.text) if size_el is not None and size_el.text else None
            parsed.append((href, is_dir, size))

        if not parsed:
            return []
        # Depth:1 always returns the collection itself plus its direct
        # children; the collection is the shortest href (fewest segments).
        self_href = min(parsed, key=lambda t: t[0].count("/"))[0]
        entries = [
            {"name": href.rsplit("/", 1)[-1], "type": "dir" if is_dir else "file", "size": None if is_dir else size}
            for href, is_dir, size in parsed
            if href != self_href
        ]
        return sorted(entries, key=lambda e: (e["type"] == "file", e["name"]))

    def read_file(self, path: str) -> str:
        rel = _safe_rel(path)
        resp = self._request("GET", rel)
        resp.raise_for_status()
        return resp.text

    def read_binary(self, path: str) -> bytes:
        """Raw bytes of a file — read_file's text decode would corrupt
        non-text downloads (used by the web UI's file download)."""
        rel = _safe_rel(path)
        resp = self._request("GET", rel, stream=True)
        try:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                raise FileTooLarge(rel)
            data = b"".join(self._chunks(resp))
        finally:
            resp.close()
        return data

    @staticmethod
    def _chunks(resp):
        total = 0
        for chunk in resp.iter_content(65536):
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise FileTooLarge("download")
            yield chunk

    def write_file(self, path: str, content: str) -> str:
        rel = _safe_rel(path)
        resp = self._request("PUT", rel, data=content.encode("utf-8"))
        resp.raise_for_status()
        return f"ok: {path}"

    def write_binary(self, path: str, data: bytes) -> str:
        """Same as write_file but for raw bytes (e.g. a generated .odt) —
        write_file's `content: str` would corrupt non-text data by forcing
        a UTF-8 encode. Used by nextcloud/docgen.py; not part of the
        FilesBackend text-only surface nc_files_write exposes."""
        rel = _safe_rel(path)
        resp = self._request("PUT", rel, data=data)
        resp.raise_for_status()
        return f"ok: {path}"

    def _walk(self, path: str):
        for entry in self.list_files(path):
            child = f"{path.rstrip('/')}/{entry['name']}" if path not in ("", "/") else f"/{entry['name']}"
            if entry["type"] == "dir":
                yield from self._walk(child)
            else:
                yield {"path": child, "size": entry["size"]}

    def search_files(self, query: str, path: str = "/") -> list[dict]:
        lq = query.lower()
        results = []
        for entry in self._walk(path):
            if entry["size"] is not None and entry["size"] > SEARCH_MAX_BYTES:
                continue
            try:
                text = self.read_file(entry["path"])
            except Exception:
                continue
            hits = [
                {"line": i + 1, "text": line}
                for i, line in enumerate(text.splitlines())
                if lq in line.lower()
            ]
            if hits:
                results.append({"path": entry["path"], "matches": hits})
        return results
