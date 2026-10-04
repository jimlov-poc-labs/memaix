# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for the project's Nextcloud files (/app/files) — read-only browse
and download over the same backend and ACL check as the nc_files_* tools.

The project's Nextcloud account is the real isolation boundary; the path
check here is defence in depth so a crafted path can never climb out of it."""

from __future__ import annotations

from urllib.parse import quote, unquote

import requests
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...acl import AccessDenied
from ...connectors.adapters.files_webdav import FileTooLarge
from ...tools import nc_files as t_nc
from .. import routes as w


def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _get_acl():
    return w._get_acl()


def _json_401() -> JSONResponse:
    return w._json_401()


def _token_store():
    from ...server import _get_token_store

    return _get_token_store()


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _unsafe(text: str) -> bool:
    return "\x00" in text or "\\" in text or any(seg == ".." for seg in text.split("/"))


def clean_path(raw) -> str | None:
    """Normalised absolute path, or None when the input is not acceptable.
    Checked both as sent and percent-decoded, since a '%2e%2e' segment would
    become '..' once the WebDAV server decodes it."""
    if not isinstance(raw, str) or not raw.startswith("/"):
        return None
    if _unsafe(raw) or _unsafe(unquote(raw)):
        return None
    segments = [s for s in raw.split("/") if s not in ("", ".")]
    return "/" + "/".join(segments)


def _files_backend(acl, user: str, project: str):
    """The project's files backend, or None when it has none configured.
    The ACL check comes first so non-members cannot probe which projects
    have files."""
    acl.enforce(user, project, "reader")
    from ...connectors.registry import default_registry

    try:
        return default_registry().get(acl, _token_store(), project, "files", user)
    except ValueError:
        return None


def _open(request: Request):
    """(acl, user, project, path, backend) for a valid request, otherwise the
    error response to return."""
    user = _require_user(request)
    if not user:
        return _json_401()
    path = clean_path(request.query_params.get("path", "/"))
    if path is None:
        return _error("bad_path", 400)
    project = request.query_params.get("project", "")
    acl = _get_acl()
    try:
        backend = _files_backend(acl, user, project)
    except AccessDenied:
        return _error("forbidden", 403)
    if backend is None:
        return _error("no_files", 404)
    return acl, user, project, path, backend


def _upstream_error(exc: requests.RequestException) -> JSONResponse:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 404:
        return _error("not_found", 404)
    return _error("upstream_error", 502)


def api_files_list(request: Request) -> JSONResponse:
    """GET /app/api/files?project=X&path=/sub → {path, entries: [{name, type, size}]}"""
    opened = _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    acl, user, project, path, backend = opened
    try:
        entries = t_nc.nc_files_list(acl, user, project, path, _files=backend)
    except AccessDenied:
        return _error("forbidden", 403)
    except ValueError:  # the adapter's own path validation
        return _error("bad_path", 400)
    except requests.RequestException as exc:
        return _upstream_error(exc)
    return JSONResponse({"path": path, "entries": entries})


def _attachment_header(name: str) -> str:
    fallback = "".join(c if 32 < ord(c) < 127 and c not in '"\\%' else "_" for c in name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


def api_files_download(request: Request) -> Response:
    """GET /app/api/files/download?project=X&path=/a/b.txt → the file as an attachment"""
    opened = _open(request)
    if isinstance(opened, JSONResponse):
        return opened
    acl, user, project, path, backend = opened
    if path == "/":
        return _error("bad_path", 400)
    try:
        data = t_nc.nc_files_download(acl, user, project, path, _files=backend)
    except AccessDenied:
        return _error("forbidden", 403)
    except FileTooLarge:
        return _error("too_large", 413)
    except ValueError:  # the adapter's own path validation
        return _error("bad_path", 400)
    except requests.RequestException as exc:
        return _upstream_error(exc)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _attachment_header(path.rsplit("/", 1)[-1]),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
