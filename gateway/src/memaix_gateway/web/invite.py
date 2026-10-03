# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public invitation page: the invitee picks their own password.

Unauthenticated by design — possession of the single-use, expiring token is
the credential. The token is in the URL, so every response is no-store and
sends no Referer.
"""

from __future__ import annotations

from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse

from ..access_admin import MIN_PASSWORD_LEN, AccessError

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

_PAGE = """<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memaix</title>
<style>body{{font:16px system-ui,sans-serif;background:#fafafa;color:#222;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:2rem;max-width:26rem;width:100%}}
input,button{{font:inherit;width:100%;box-sizing:border-box;padding:.6rem;margin:.3rem 0 1rem}}
button{{background:#222;color:#fff;border:0;border-radius:6px;cursor:pointer}}.err{{color:#b00020}}</style></head>
<body><main><h1>Memaix</h1>{body}</main></body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status, headers=_HEADERS)


def _form(user: str, error: str = "") -> str:
    err = f'<p class="err">{escape(error)}</p>' if error else ""
    return (
        f"<p>Hej <b>{escape(user)}</b>! Välj ditt lösenord för att komma igång "
        f"(minst {MIN_PASSWORD_LEN} tecken).<br><small>Choose your password to get started.</small></p>{err}"
        '<form method="post">'
        '<label>Lösenord / Password<input type="password" name="password" required '
        f'minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password"></label>'
        '<label>Upprepa / Repeat<input type="password" name="confirm" required '
        f'minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password"></label>'
        "<button>Spara lösenord</button></form>"
    )


_INVALID = "<p class=\"err\">Länken är ogiltig eller har gått ut. Be om en ny inbjudan.<br><small>This link is invalid or has expired.</small></p>"


def _admin():
    from ..tools import access as t_access

    return t_access._admin()


async def invite_page(request: Request) -> HTMLResponse:
    token = request.path_params["token"]
    user = _admin()._invites.peek(token)
    if user is None:
        return _page(_INVALID, 404)
    if request.method == "GET":
        return _page(_form(user))

    form = await request.form()
    password = str(form.get("password") or "")
    if password != str(form.get("confirm") or ""):
        return _page(_form(user, "Lösenorden matchar inte."), 400)
    try:
        accepted = _admin().accept_invite(token, password)
    except AccessError as exc:
        return _page(_form(user, str(exc)), 400)
    from ..server import reload_acl

    reload_acl()
    return _page(
        f"<p>Klart, <b>{escape(accepted)}</b>! Ditt lösenord är sparat. "
        "Du kan nu logga in när du lägger till Memaix i din AI-klient "
        "(se docs/AI-CLIENTS.md).</p>"
    )
