# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project creation, project-scoped membership and invitations.

Model: a global admin (``admin: true``) is implicit owner everywhere and the
only one who may create projects. A project owner may manage members of that
project and nothing else. Every method takes the *actor* explicitly and
re-checks the permission against a fresh ACL snapshot, so callers (MCP tools,
web routes) cannot forget to.

Callers must call server.reload_acl() after a successful write.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import gitvault
from .acl import ROLES, AccessDenied, Acl
from .cli import hash_password
from .invites import DEFAULT_TTL_S, InviteStore
from .web.acl_writer import AclWriter

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_RESERVED_USERS = {"admin", "root", "system", "memaix", "anonymous"}
MIN_PASSWORD_LEN = 12


class AccessError(Exception):
    """Invalid request (bad name, unknown target, rule violation)."""


def _live_admin(acl: Acl, actor: str) -> bool:
    return actor in acl.users and not acl.is_disabled(actor) and acl.is_admin(actor)


class AccessAdmin:
    def __init__(self, writer: AclWriter, invites: InviteStore, vaults_dir: Path) -> None:
        self._writer = writer
        self._invites = invites
        self._vaults = Path(vaults_dir)

    # ----------------------------------------------------------- permissions

    def _require_project_owner(self, acl: Acl, actor: str, project: str) -> None:
        if actor not in acl.users or acl.is_disabled(actor):
            raise AccessDenied(f"{actor} may not manage access")
        if project not in acl.projects:
            if acl.is_admin(actor):
                raise AccessError(f"unknown project: {project}")
            raise AccessDenied(f"{actor} has no access to {project}")
        acl.enforce(actor, project, "owner")

    # -------------------------------------------------------------- projects

    def create_project(self, acl: Acl, actor: str, name: str) -> dict:
        if not _live_admin(acl, actor):
            raise AccessDenied("only a system admin may create projects")
        if not isinstance(name, str) or not _NAME.match(name):
            raise AccessError("project name must be 2-32 chars of a-z, 0-9, '-' or '_'")
        vault = self._vaults / name
        if vault.exists():
            raise AccessError(f"vault directory already exists for {name}")

        def check_and_add(data: dict) -> None:
            projects = data.setdefault("projects", {})
            if name in projects:
                raise AccessError(f"project already exists: {name}")
            projects[name] = {"vault": str(vault), "allow_send": False}
            grants = data["users"][actor].setdefault("grants", {})
            grants[name] = "owner"

        # Write ACL first under the lock (rejects duplicates), then build the
        # vault; roll the ACL entry back if the vault cannot be created.
        self._writer.update(check_and_add)
        try:
            self._build_vault(vault)
        except BaseException:
            def undo(data: dict) -> None:
                data.get("projects", {}).pop(name, None)
                data["users"][actor].get("grants", {}).pop(name, None)

            self._writer.update(undo)
            raise
        return {"project": name, "vault": str(vault), "owner": actor}

    @staticmethod
    def _build_vault(vault: Path) -> None:
        vault.mkdir(parents=True)
        for sub in ("memory", "backlog"):
            (vault / sub).mkdir()
            (vault / sub / ".gitkeep").write_text("")
        (vault / ".gitignore").write_text(".memaix.db*\n")
        gitvault.init(vault)
        gitvault.commit(vault, [".gitignore", "memory/.gitkeep", "backlog/.gitkeep"], "Initial vault")

    # --------------------------------------------------------------- members

    def list_members(self, acl: Acl, actor: str, project: str) -> list[dict]:
        self._require_project_owner(acl, actor, project)
        members = [
            {"user": uid, "role": u["grants"][project], "disabled": bool(u.get("disabled"))}
            for uid, u in acl.users.items()
            if project in (u.get("grants") or {})
        ]
        return sorted(members, key=lambda m: m["user"])

    def set_member(self, acl: Acl, actor: str, project: str, user: str, role: str | None) -> dict:
        self._require_project_owner(acl, actor, project)
        if role is not None and role not in ROLES:
            raise AccessError(f"unknown role: {role}")
        if user not in acl.users:
            raise AccessError(f"unknown user: {user}")
        if acl.is_admin(user):
            raise AccessError("a system admin's access cannot be changed here")
        actor_is_admin = acl.is_admin(actor)

        def apply(data: dict) -> None:
            users = data["users"]
            if user not in users or project not in data.get("projects", {}):
                raise AccessError("user or project vanished")
            if users[user].get("admin") is True:
                raise AccessError("a system admin's access cannot be changed here")
            current = (users[user].get("grants") or {}).get(project)
            if not actor_is_admin and current == "owner" and role != "owner":
                others = [
                    uid for uid, u in users.items()
                    if uid != user and u.get("admin") is not True
                    and (u.get("grants") or {}).get(project) == "owner"
                ]
                if not others:
                    raise AccessError("cannot remove the last project owner; add another owner first")
            grants = users[user].setdefault("grants", {})
            if role is None:
                grants.pop(project, None)
            else:
                grants[project] = role

        self._writer.update(apply)
        return {"project": project, "user": user, "role": role}

    # ------------------------------------------------------------ invitations

    def invite(
        self,
        acl: Acl,
        actor: str,
        project: str,
        user: str,
        role: str,
        *,
        email: str | None = None,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> dict:
        self._require_project_owner(acl, actor, project)
        if role not in ROLES:
            raise AccessError(f"unknown role: {role}")
        if not isinstance(user, str) or not _NAME.match(user) or user in _RESERVED_USERS:
            raise AccessError("user id must be 2-32 chars of a-z, 0-9, '-' or '_' and not reserved")
        if acl.is_admin(user):
            raise AccessError("a system admin's access cannot be changed here")

        def apply(data: dict) -> str:
            users = data.setdefault("users", {})
            existing = users.get(user)
            if existing is not None and existing.get("admin") is True:
                raise AccessError("a system admin's access cannot be changed here")
            if existing is not None and existing.get("password_hash"):
                existing.setdefault("grants", {})[project] = role
                return "granted"
            entry = existing if existing is not None else {"oauth_subjects": [user]}
            entry.setdefault("grants", {})[project] = role
            if email:
                entry["email"] = email
            users[user] = entry
            return "invited"

        status = self._writer.update(apply)
        if status == "granted":
            return {"status": status, "project": project, "user": user, "role": role}
        token = self._invites.issue(user, actor, ttl_s)
        return {"status": status, "project": project, "user": user, "role": role, "token": token}

    def accept_invite(self, token: str, password: str) -> str:
        user = self._invites.peek(token)
        if user is None:
            raise AccessError("invalid or expired invitation")
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
            raise AccessError(f"password must be at least {MIN_PASSWORD_LEN} characters")
        hashed = hash_password(password)

        def apply(data: dict) -> None:
            u = data.get("users", {}).get(user)
            if u is None or u.get("disabled") or u.get("admin") is True or u.get("password_hash"):
                raise AccessError("invalid or expired invitation")
            u["password_hash"] = hashed

        self._writer.update(apply)
        self._invites.revoke_user(user)
        return user
