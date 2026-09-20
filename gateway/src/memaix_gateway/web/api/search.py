# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web API for unified search (MEX-025 Fas D) — thin over search/query.py.

Calls the pure search_all(acl, user, …) directly (the MCP tool wrapper reads
identity from the MCP context, which doesn't exist here). Snippets are already
truncated by the search layer; no live email fold-in from the web (no
_email_search) — the browser surface stays read-only against the index."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import routes as w


def _require_user(request: Request) -> str | None:
    return w._require_user(request)


def api_search(request: Request) -> JSONResponse:
    """GET /app/api/search?q=…&project=…&limit=8 → {results, semantic, projects_searched}"""
    user = _require_user(request)
    if not user:
        return w._json_401()
    query = request.query_params.get("q", "").strip()
    if not query:
        return JSONResponse({"results": [], "semantic": False, "projects_searched": []})
    project = request.query_params.get("project") or None
    try:
        limit = min(int(request.query_params.get("limit", 8)), 50)
    except ValueError:
        return JSONResponse({"error": "bad_request"}, status_code=400)

    from ... import config
    from ...search.query import search_all as _search_all
    from ...server import _get_search_embedder, _get_search_store

    result = _search_all(
        w._get_acl(), user, config.load(), _get_search_store(), _get_search_embedder(),
        query, [project] if project else None, limit,
    )
    return JSONResponse(result)
