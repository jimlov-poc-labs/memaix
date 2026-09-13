# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-user IMAP mail adapter (FEATURE-CONNECTOR-FRAMEWORK.md — IMAP as a
per-user provider, multiple mailboxes of the same type per user).

Today's IMAP connector (`type="imap"`, `auth="shared"`, catalog.py's
`_imap_factory`) builds one project-wide `imap_tools.MailBox` from
`acl.yaml`'s static `mailbox` resource — one shared mailbox per project,
credentials from `config.secret`. This adapter is the per-user counterpart:
it builds the exact same kind of object (`imap_tools.MailBox`), but from a
token-store token dict (`{host, user, password, port?}`) instead of from
acl.yaml, so a user can link their own personal IMAP mailbox (or several —
`user_tokens` is already unique on `(memaix_user, provider, account_email)`,
so each linked mailbox is its own row/token).

`tools/email.py`'s `_make_mailbox` is untouched — it still builds the
shared-IMAP case. This module is only ever reached through the registry's
`imap_user` connector spec (catalog.py), never imported by tools/email.py.

The object returned is a real `imap_tools.MailBox` (not a wrapper), so it
satisfies `tools/email.py`'s `_imap` duck type (`.folder.set`, `.fetch`,
`.append`, `.logout`, and message objects with `.flags`) exactly — no
translation layer needed, unlike the Microsoft Graph adapter.
"""

from __future__ import annotations


def build_mailbox(token: dict):
    """Build and log into an `imap_tools.MailBox` from a per-user IMAP token.

    `token` is whatever was stored via `account_link_imap` /
    `TokenStore.store(user, "imap", account_email, {...})`:
    `{host, user, password, port?}`. Raises ValueError if a required field
    is missing — never logs or echoes the password.
    """
    from imap_tools import MailBox

    host = token.get("host")
    user = token.get("user")
    password = token.get("password")
    if not host or not user or not password:
        raise ValueError("imap token missing required field(s): host, user, password")

    port = token.get("port")
    mb = MailBox(host, port=int(port)) if port else MailBox(host)
    mb.login(user, password)
    return mb
