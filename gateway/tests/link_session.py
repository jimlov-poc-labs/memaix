# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test helpers for the session-bound OAuth link flow (H3).

The real state store (in-memory) and the real binding gate are used; only the
web-session cookie check is replaced by "the memaix_board cookie value is the
user" so tests need no password/ACL setup.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

from memaix_gateway import server_http
from memaix_gateway.tools import account


def patch_session(monkeypatch) -> None:
    from memaix_gateway.board import routes as board_routes

    monkeypatch.delenv("MEMAIX_STATE_DB", raising=False)
    account._pending_states.clear()
    monkeypatch.setattr(board_routes, "_check_cookie", lambda request: request.cookies.get("memaix_board") or None)


def mint(state: str = "good", user: str = "u1", provider: str = "google", ttl: int = 600) -> str:
    account._put_state(state, {"user_id": user, "provider": provider, "exp": int(time.time()) + ttl})
    return state


def cookies(state: str, provider: str = "google", session_user: str | None = "u1", link_cookie: bool = True) -> dict:
    parts = []
    if session_user is not None:
        parts.append(f"memaix_board={session_user}")
    if link_cookie:
        parts.append(f"memaix_link_{provider}={server_http._link_nonce(state)}")
    return {"Cookie": "; ".join(parts)} if parts else {}


def confirm(client, state="good", code="thecode", provider="google", **cookie_kw):
    return client.post(
        f"/link/{provider}/confirm",
        content=urlencode({"code": code, "state": state}),
        headers={"Content-Type": "application/x-www-form-urlencoded", **cookies(state, provider, **cookie_kw)},
    )


def callback(client, state="good", code="thecode", provider="google", **cookie_kw):
    return client.get(f"/link/{provider}/callback?code={code}&state={state}", headers=cookies(state, provider, **cookie_kw))
