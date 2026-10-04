# SPDX-License-Identifier: AGPL-3.0-or-later
"""nc_files_* tools — Nextcloud (WebDAV) file access via the connector
framework (FEATURE-NEXTCLOUD-BACKEND.md §4).

Deliberately a separate tool surface from files_* (tools/files.py, the
local vault) rather than a second backend behind the same tools — the two
have different resource keys and there's no ambiguity about which storage
a call reaches. `_files` accepts a connectors.base.FilesBackend duck type,
built by server.py via the connector registry.
"""

from __future__ import annotations

from ..acl import Acl


def nc_files_list(acl: Acl, user_id: str, project: str, path: str = "/", *, _files) -> list[dict]:
    acl.enforce(user_id, project, "collaborator")
    return _files.list_files(path)


def nc_files_read(acl: Acl, user_id: str, project: str, path: str, *, _files) -> str:
    acl.enforce(user_id, project, "collaborator")
    return _files.read_file(path)


def nc_files_download(acl: Acl, user_id: str, project: str, path: str, *, _files) -> bytes:
    """Raw file bytes for the web UI's download — not an MCP tool. Any member
    may download, same as reading the project's files in the web UI."""
    acl.enforce(user_id, project, "reader")
    return _files.read_binary(path)


def nc_files_write(acl: Acl, user_id: str, project: str, path: str, content: str, *, _files) -> str:
    acl.enforce(user_id, project, "collaborator")
    return _files.write_file(path, content)


def nc_files_search(acl: Acl, user_id: str, project: str, query: str, path: str = "/", *, _files) -> list[dict]:
    acl.enforce(user_id, project, "collaborator")
    return _files.search_files(query, path)
