# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for the memory explorer — thin layer over tools/memory.py."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ...acl import AccessDenied
from ...tools import memory as t_mem
from .. import routes as w


# Resolved through the routes module at call time (not import-time binding) so
# there is exactly one auth/acl seam — tests patch web.routes and every api
# module follows.
def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def _get_acl():
    return w._get_acl()


def _json_401() -> JSONResponse:
    return w._json_401()


def _project(request: Request) -> str:
    return request.query_params.get("project", "")


def api_memory_notes(request: Request) -> JSONResponse:
    """GET /app/api/memory/notes?project=X → [{path, mtime}]"""
    user = _require_user(request)
    if not user:
        return _json_401()
    try:
        notes = t_mem.memory_list(_get_acl(), user, _project(request))
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(notes)


def api_memory_note(request: Request) -> JSONResponse:
    """GET /app/api/memory/note?project=X&path=Y → {path, content}"""
    user = _require_user(request)
    if not user:
        return _json_401()
    path = request.query_params.get("path", "")
    try:
        note = t_mem.memory_read(_get_acl(), user, _project(request), path)
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    except FileNotFoundError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(note)


def api_memory_search(request: Request) -> JSONResponse:
    """GET /app/api/memory/search?project=X&q=Y → [{path, snippet}]"""
    user = _require_user(request)
    if not user:
        return _json_401()
    query = request.query_params.get("q", "")
    try:
        hits = t_mem.memory_search(_get_acl(), user, _project(request), query)
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(hits)


def api_memory_history(request: Request) -> JSONResponse:
    """GET /app/api/memory/history?project=X&path=Y → [{hash, author, date, message}]"""
    user = _require_user(request)
    if not user:
        return _json_401()
    path = request.query_params.get("path") or None
    try:
        entries = t_mem.memory_history(_get_acl(), user, _project(request), path)
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(entries)


async def api_memory_revert(request: Request) -> JSONResponse:
    """POST /app/api/memory/revert {project, sha} → {reverted_to, new_commit}

    Web policy requires owner (stricter than the MCP tool's collaborator):
    reverting from a browser click is a bigger footgun than an agent doing it
    deliberately, so the web layer gates harder before delegating.
    """
    user = _require_user(request)
    if not user:
        return _json_401()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    project = body.get("project", "")
    sha = body.get("sha", "")
    acl = _get_acl()
    try:
        acl.enforce(user, project, "owner")
        result = t_mem.memory_revert(acl, user, project, sha)
    except AccessDenied:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)
