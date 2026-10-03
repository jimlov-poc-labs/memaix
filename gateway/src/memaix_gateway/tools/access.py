# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP-facing access administration: create projects, manage members, invite.

Granting access is never executed straight from a model's tool call: member
changes and invitations are queued in the outbox and only run once a human
approves them (AGENTS.md: no auto-chain from read content to write actions).
``project_create`` is admin-only and grants nothing to anyone else, so it runs
directly.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import config
from ..access_admin import AccessAdmin
from ..acl import Acl
from ..invites import InviteStore
from ..paths import data_dir
from ..web.acl_writer import AclWriter

ACCESS_TOOLS = ("project_member_set", "user_invite", "user_reset_link")


def _admin() -> AccessAdmin:
    vaults = Path(os.environ.get("MEMAIX_VAULTS_DIR", "/srv/vaults"))
    invites = InviteStore(Path(os.environ.get("MEMAIX_INVITES_DB", str(data_dir() / "memaix-invites.db"))))
    return AccessAdmin(AclWriter(config.CONFIG_DIR / "acl.yaml"), invites, vaults)


def _fresh_acl() -> Acl:
    return Acl.from_config(config.load()["acl"])


def _reload() -> None:
    from .. import server

    server.reload_acl()


def _queue(user: str, project: str, tool: str, args: dict, outbox=None) -> dict:
    from ..outbox.preview import render_preview
    from ..outbox.queue import default_queue

    queue = outbox if outbox is not None else default_queue()
    action_id = queue.enqueue(user, project, tool, args, render_preview(tool, args))
    return {"pending": True, "action_id": action_id, "note": "Väntar på godkännande i utkorgen"}


def project_create(acl: Acl, user: str, name: str, *, _admin_obj: AccessAdmin | None = None) -> dict:
    out = (_admin_obj or _admin()).create_project(acl, user, name)
    _reload()
    return out


def project_members(acl: Acl, user: str, project: str, *, _admin_obj: AccessAdmin | None = None) -> list:
    return (_admin_obj or _admin()).list_members(acl, user, project)


def project_member_set(
    acl: Acl, user: str, project: str, member: str, role: str | None,
    *, _confirmed: bool = False, _outbox=None, _admin_obj: AccessAdmin | None = None,
) -> dict:
    adm = _admin_obj or _admin()
    adm._require_project_owner(acl, user, project)
    if not _confirmed:
        return _queue(user, project, "project_member_set", {"member": member, "role": role}, _outbox)
    out = adm.set_member(_fresh_acl(), user, project, member, role)
    _reload()
    return out


def user_invite(
    acl: Acl, user: str, project: str, invitee: str, role: str, email: str | None = None,
    *, _confirmed: bool = False, _outbox=None, _admin_obj: AccessAdmin | None = None,
) -> dict:
    adm = _admin_obj or _admin()
    adm._require_project_owner(acl, user, project)
    if not _confirmed:
        args = {"invitee": invitee, "role": role, "email": email}
        return _queue(user, project, "user_invite", args, _outbox)
    out = adm.invite(_fresh_acl(), user, project, invitee, role, email=email)
    _reload()
    token = out.pop("token", None)
    if token:
        base = (config.load().get("memaix", {}).get("server", {}) or {}).get("public_url", "").rstrip("/")
        out["invite_url"] = f"{base}/app/invite/{token}"
    return out


def user_reset_link(
    acl: Acl, user: str, project: str, member: str,
    *, _confirmed: bool = False, _outbox=None, _admin_obj: AccessAdmin | None = None,
) -> dict:
    adm = _admin_obj or _admin()
    adm._require_project_owner(acl, user, project)
    if not _confirmed:
        return _queue(user, project, "user_reset_link", {"member": member}, _outbox)
    out = adm.reset_link(_fresh_acl(), user, project, member)
    token = out.pop("token")
    base = (config.load().get("memaix", {}).get("server", {}) or {}).get("public_url", "").rstrip("/")
    out["reset_url"] = f"{base}/app/invite/{token}"
    return out
