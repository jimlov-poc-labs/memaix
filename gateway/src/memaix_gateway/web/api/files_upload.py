# SPDX-License-Identifier: AGPL-3.0-or-later
"""Script upload of binary files to a project's Nextcloud area:
PUT /api/files/{project}/{path} with the raw file as the request body.

Unlike the /app/api/* routes (signed session cookie) this one is for headless
scripts, so it is authenticated with the same Hydra JWT as the MCP surface.
Custom routes get NO bearer enforcement from FastMCP (its auth middleware only
parses a token; RequireAuthMiddleware wraps just the MCP endpoint), so every
check is made here, in this order, and each one fails closed:

  1. valid JWT (signature, issuer, audience, exp present and in the future)
  2. subject is a registered service client (acl.yaml service_clients)
  3. the client's ``tools`` contain the pseudo-tool ``nc_files_upload`` and its
     ``projects`` contain the project (empty list = nothing)
  4. acts_as user has at least collaborator on the project
  5. rate limit per client
  6. Content-Length present, > 0 and <= MAX_UPLOAD_BYTES (before any body read)
  7. path and extension allowlist, then the streamed body: byte count, sha256,
     magic bytes against the extension
  8. the project's own Nextcloud account (registry, same as the download
     route), never overwriting (If-None-Match: *)

The file is spooled to a temporary file, never held whole in memory. Errors
carry a short code only: no paths, no upstream detail, no secrets."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
import weakref
from urllib.parse import unquote

import requests
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from ...acl import AccessDenied, Acl, ServiceClient
from ...connectors.adapters.files_webdav import FileExists
from ...paths import validate_relative_path

logger = logging.getLogger(__name__)

TOOL_NAME = "nc_files_upload"  # pseudo-tool: only a value in service_clients.<id>.tools
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
RATE_LIMIT = 10  # uploads per window per client
RATE_WINDOW_S = 60
ALLOWED_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "gif", "pdf"})
MAX_PATH_CHARS = 1024
BODY_TIMEOUT_S = 120  # total time allowed for streaming the body in (slow-body guard)
MAX_CONCURRENT_UPLOADS = 4  # simultaneous uploads to Nextcloud (each holds a pool thread)
_HEAD_BYTES = 16
_SPOOL_MEMORY = 1024 * 1024


def _err(code: str, status: int, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})})


# One semaphore per running event loop, created on first use (an asyncio
# primitive must not be shared between loops, and import time has no loop).
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _slot_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _slots.get(loop)
    if sem is None:
        sem = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
    return sem


class _Deny(Exception):
    def __init__(self, code: str, status: int, audit: str = "", *, log_audit: bool = True, headers: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.audit = audit or code
        self.log_audit = log_audit
        self.headers = headers


# ---------------------------------------------------------------------------
# Small, separately testable pieces
# ---------------------------------------------------------------------------


def normalise_path(raw) -> str:
    """Relative path with empty and '.' segments dropped, so './a.png' and
    'a.png' are the same file. Raises ValueError for anything unacceptable:
    absolute (leading '/'), '..', backslash, control chars, hidden segments,
    too long."""
    if not isinstance(raw, str) or not raw or raw.startswith("/"):
        raise ValueError("bad path")
    if len(raw) > MAX_PATH_CHARS or "\\" in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("bad path")
    segments = [s for s in raw.split("/") if s not in ("", ".")]
    if not segments or any(s.startswith(".") or s.rstrip() != s for s in segments):
        raise ValueError("bad path")  # includes '..' and hidden names
    rel = "/".join(segments)
    validate_relative_path(rel)
    # A '%2e%2e' that survived decoding once would become '..' in the WebDAV
    # server; refuse a path that changes meaning when decoded again.
    if any(s in ("..", ".") for s in unquote(rel).split("/")):
        raise ValueError("bad path")
    return rel


def extension_of(rel: str) -> str:
    """The one allowed extension of the file name, lower-case. Raises
    ValueError for no extension, a second extension (x.png.exe, x.exe.png) or
    one outside the allowlist."""
    name = rel.rsplit("/", 1)[-1]
    if name.count(".") != 1 or name.endswith("."):
        raise ValueError("bad extension")
    stem, ext = name.rsplit(".", 1)
    ext = ext.lower()
    if not stem or ext not in ALLOWED_EXTENSIONS:
        raise ValueError("bad extension")
    return ext


def magic_matches(ext: str, head: bytes) -> bool:
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if ext == "webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == "pdf":
        return head.startswith(b"%PDF-")
    return False


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _server():
    from ... import server

    return server


def _audit(user: str, project: str, ok: bool, msg: str) -> None:
    try:
        _server()._get_audit().log(user, project, TOOL_NAME, ok, msg)
    except Exception:  # noqa: BLE001 - audit trouble must not turn a denial into an allow or a 500
        logger.warning("audit write failed for %s", TOOL_NAME)


def _registry():
    from ...connectors.registry import default_registry

    return default_registry()


def _files_backend(acl: Acl, user: str, project: str):
    """The project's own files backend (registry, same as the download route),
    or None when the project has none."""
    from ...server import _get_token_store

    try:
        return _registry().get(acl, _get_token_store(), project, "files", user)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Authentication and authorisation (steps 1-4)
# ---------------------------------------------------------------------------


async def _authenticate(request: Request):
    """Verified AccessToken, or raise _Deny(401)."""
    token = _bearer(request)
    if token is None:
        raise _Deny("not_authenticated", 401)
    verifier = getattr(_server().mcp, "_token_verifier", None)
    # No verifier, or one that would skip the audience check, means we cannot
    # prove the token was minted for this server: refuse rather than guess.
    if verifier is None or not getattr(verifier, "_audiences", None):
        logger.error("files upload refused: token verifier or audience not configured")
        raise _Deny("not_authenticated", 401)
    try:
        access = await verifier.verify_token(token)
    except Exception:  # noqa: BLE001
        access = None
    if access is None or not access.subject or not isinstance(access.subject, str):
        raise _Deny("not_authenticated", 401)
    if not access.expires_at or access.expires_at <= int(time.time()):
        raise _Deny("not_authenticated", 401)
    return access


def _authorise(acl: Acl, access, project: str) -> ServiceClient:
    sc = acl.service_client(access.subject)
    if sc is None:
        # A plain user token (or unknown subject) is not allowed to upload here.
        raise _Deny("forbidden", 403, f"subject {access.subject} is not a service client")
    if not project or TOOL_NAME not in sc.tools:
        raise _Deny("forbidden", 403, f"service client {sc.client_id} lacks {TOOL_NAME}")
    if project not in sc.projects:
        raise _Deny("forbidden", 403, f"service client {sc.client_id} not allowed in this project")
    # Admin is an implicit owner on every project, so an admin acts_as would
    # make the project allowlist the only barrier. Refuse it outright.
    if acl.is_admin(sc.acts_as):
        raise _Deny("forbidden", 403, f"service client {sc.client_id} acts as an admin")
    try:
        acl.enforce(sc.acts_as, project, "collaborator")
    except AccessDenied as exc:
        raise _Deny("forbidden", 403, str(exc)) from exc
    return sc


# ---------------------------------------------------------------------------
# Body handling (step 6-7)
# ---------------------------------------------------------------------------


def _declared_length(request: Request) -> int:
    if request.headers.get("transfer-encoding"):
        raise _Deny("length_required", 411)
    raw = request.headers.get("content-length")
    if raw is None:
        raise _Deny("length_required", 411)
    raw = raw.strip()
    if not raw.isascii() or not raw.isdigit():
        raise _Deny("bad_length", 400)
    length = int(raw)
    if length == 0:
        raise _Deny("empty_file", 400)
    if length > MAX_UPLOAD_BYTES:
        raise _Deny("too_large", 413)
    return length


async def _spool_body(request: Request, declared: int, ext: str):
    """Stream the body to a temp file. Returns (spool, size, sha256 hex).
    Aborts at once on more bytes than declared (Content-Length can lie), on
    a short body, or on the wrong magic bytes. Caller closes the spool."""
    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY)  # noqa: SIM115 - closed by the caller
    digest = hashlib.sha256()
    head = b""
    total = 0
    try:
        async with asyncio.timeout(BODY_TIMEOUT_S):
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > declared or total > MAX_UPLOAD_BYTES:
                    raise _Deny("too_large", 413, "body longer than declared")
                if len(head) < _HEAD_BYTES:
                    head += chunk[: _HEAD_BYTES - len(head)]
                    if len(head) >= _HEAD_BYTES and not magic_matches(ext, head):
                        raise _Deny("bad_content", 415, "content does not match extension")
                digest.update(chunk)
                spool.write(chunk)
        if total == 0:
            raise _Deny("empty_file", 400)
        if total != declared:
            raise _Deny("bad_length", 400, "body shorter than declared")
        if not magic_matches(ext, head):
            raise _Deny("bad_content", 415, "content does not match extension")
    except TimeoutError as exc:
        spool.close()
        raise _Deny("timeout", 408, "denied: timeout") from exc
    except BaseException:
        spool.close()
        raise
    return spool, total, digest.hexdigest()


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


async def api_files_upload(request: Request) -> JSONResponse:
    """PUT /api/files/{project}/{path} — raw body, new files only."""
    project = request.path_params.get("project", "")
    raw_path = request.path_params.get("path", "")
    user = "-"
    try:
        access = await _authenticate(request)
        user = f"sub:{access.subject}"
        # Per verified subject and BEFORE authorise: a valid but unauthorised
        # token can then not flood the audit log. Throttled attempts are not
        # audited for the same reason (server log only).
        if not _server()._rate_limiter.check(f"upload:{access.subject}", RATE_LIMIT, RATE_WINDOW_S):
            raise _Deny("rate_limited", 429, log_audit=False)
        sc = _authorise(_server()._get_acl(), access, project)
        user = sc.acts_as
        declared = _declared_length(request)
        try:
            rel = normalise_path(raw_path)
            ext = extension_of(rel)
        except ValueError as exc:
            raise _Deny("bad_path", 400, "rejected path or extension") from exc
        return await _store(request, sc, project, rel, ext, declared)
    except _Deny as deny:
        if user != "-" and deny.log_audit:  # unauthenticated/throttled noise stays out of the audit log
            _audit(user, project, False, deny.audit)
        else:
            logger.info("files upload refused: %s", deny.code)
        return _err(deny.code, deny.status, deny.headers)
    except Exception:  # noqa: BLE001 - fail closed with a bare code, detail only in the server log
        logger.exception("files upload failed unexpectedly")
        if user != "-":
            _audit(user, project, False, "internal error")
        return _err("internal_error", 500)


def _busy() -> _Deny:
    return _Deny("busy", 503, "denied: busy", headers={"Retry-After": "5"})


async def _store(request: Request, sc: ServiceClient, project: str, rel: str, ext: str, declared: int):
    acl = _server()._get_acl()
    backend = _files_backend(acl, sc.acts_as, project)
    if backend is None:
        raise _Deny("no_files", 404, "project has no files area")
    sem = _slot_semaphore()
    # First line of defence: refuse before reading a single body byte.
    if sem.locked():
        raise _busy()
    spool, size, sha256 = await _spool_body(request, declared, ext)
    # Second line: the slots may have filled while the body streamed in. No
    # await between locked() and acquire(), so a free slot is taken atomically.
    if sem.locked():
        spool.close()
        raise _busy()
    await sem.acquire()
    try:
        try:
            await run_in_threadpool(backend.put_stream, rel, spool, size)
        except FileExists as exc:
            raise _Deny("exists", 409, f"refused to overwrite {rel}") from exc
        except ValueError as exc:
            raise _Deny("bad_path", 400, "adapter rejected path") from exc
        except requests.RequestException as exc:
            logger.warning("files upload upstream error: %s", type(exc).__name__)
            raise _Deny("upstream_error", 502, "upstream error") from exc
    finally:
        sem.release()
        spool.close()
    _audit(sc.acts_as, project, True, f"uploaded {rel} ({size} bytes, sha256 {sha256[:12]})")
    return JSONResponse(
        {"project": project, "path": rel, "size": size, "sha256": sha256},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )
