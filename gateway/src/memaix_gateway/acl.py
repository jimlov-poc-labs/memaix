"""Access control — the core that makes one connector safe for many people.

SPDX-License-Identifier: AGPL-3.0-or-later

Every tool call passes through `enforce(user, project, need)` before touching a backend.
This module is the security boundary; keep it simple and well-tested.
"""

from __future__ import annotations

from dataclasses import dataclass

# Role hierarchy: higher index = more privilege.
ROLES = ("reader", "collaborator", "owner")


class AccessDenied(Exception):
    """Raised when a user lacks the required role on a project."""


def _rank(role: str) -> int:
    try:
        return ROLES.index(role)
    except ValueError:
        return -1


@dataclass(frozen=True)
class ServiceClient:
    """A headless OAuth client (client_credentials) restricted to a subset of
    what its ``acts_as`` user may do: only the listed tools, only in the listed
    projects. Empty sets mean nothing is allowed (default deny)."""

    client_id: str
    acts_as: str
    tools: frozenset[str]
    projects: frozenset[str]


def _str_list(client_id: str, key: str, value) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"service_clients.{client_id}.{key} must be a list of names")
    return frozenset(value)


def _user_subjects(users: dict) -> set[str]:
    subjects: set[str] = set()
    for u in users.values():
        if u.get("oauth_sub"):
            subjects.add(u["oauth_sub"])
        subjects.update(u.get("oauth_subjects") or [])
    return subjects


def _parse_service_client(client_id: str, spec, users: dict, projects: dict, user_subjects: set[str]) -> ServiceClient:
    if not isinstance(spec, dict):
        raise ValueError(f"service_clients.{client_id} must be a mapping")
    acts_as = spec.get("acts_as")
    if not acts_as or not isinstance(acts_as, str):
        raise ValueError(f"service_clients.{client_id}: acts_as is required")
    if acts_as not in users:
        raise ValueError(f"service_clients.{client_id}: acts_as {acts_as!r} is not a user in acl.yaml")
    if client_id in user_subjects:
        raise ValueError(
            f"service_clients.{client_id} is also a user oauth_sub/oauth_subjects entry; "
            "a subject must be either a user login or a service client, not both"
        )
    unknown = set(spec) - {"acts_as", "tools", "projects"}
    if unknown:
        raise ValueError(f"service_clients.{client_id}: unknown keys {sorted(unknown)}")
    tools = _str_list(client_id, "tools", spec.get("tools"))
    client_projects = _str_list(client_id, "projects", spec.get("projects"))
    missing = client_projects - set(projects)
    if missing:
        raise ValueError(f"service_clients.{client_id}: unknown projects {sorted(missing)}")
    return ServiceClient(client_id, acts_as, tools, client_projects)


def _parse_service_clients(raw, users: dict, projects: dict) -> dict[str, ServiceClient]:
    """Validate the ``service_clients`` section of acl.yaml.

    Raises ValueError on anything ambiguous, so a bad block stops the gateway
    at load instead of granting something by accident."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("service_clients must be a mapping of client id -> settings")
    user_subjects = _user_subjects(users)
    return {
        str(cid): _parse_service_client(str(cid), spec, users, projects, user_subjects)
        for cid, spec in raw.items()
    }


class Acl:
    """Loaded from config/acl.yaml. See config/acl.example.yaml for shape."""

    def __init__(self, users: dict, projects: dict, service_clients: dict[str, ServiceClient] | None = None):
        self.users = users
        self.projects = projects
        self.service_clients = service_clients or {}

    @classmethod
    def from_config(cls, cfg: dict) -> "Acl":
        users = cfg.get("users", {})
        projects = cfg.get("projects", {})
        return cls(
            users=users,
            projects=projects,
            service_clients=_parse_service_clients(cfg.get("service_clients"), users, projects),
        )

    def service_client(self, oauth_sub: str) -> ServiceClient | None:
        """Return the restriction for a service-client subject, else None.

        Deliberately separate from user_by_subject: a caller that only maps
        subject -> user must never pick up ``acts_as`` without the
        restriction that comes with it (enforced in server._MemaixMCP)."""
        return self.service_clients.get(oauth_sub)

    def user_by_subject(self, oauth_sub: str) -> str | None:
        """Map an authenticated OAuth subject to an internal user id.

        login-app mints the Hydra subject as the plain username (see
        login-app/app.py:login_post, `"subject": username`) — it is not a
        separate opaque id. That convention is why `oauth_sub` and
        `oauth_subjects` entries in acl.yaml are almost always just the
        user's own id again. Keep this in sync if login-app's subject
        minting ever changes (memaix-src 4c8f32fe)."""
        for uid, u in self.users.items():
            if u.get("oauth_sub") == oauth_sub:
                return uid
            if oauth_sub in u.get("oauth_subjects", []):
                return uid
        return None

    def grants(self, user_id: str) -> dict:
        return self.users.get(user_id, {}).get("grants", {})

    def is_admin(self, user_id: str) -> bool:
        """Return True if the user has the global admin flag set in acl.yaml.

        Strict identity check, deliberately not truthiness. ``bool()`` grants
        admin on any non-empty value: ``admin: "false"``, ``admin: "no"`` and
        ``admin: 0.1`` all coerce to True. Quoting a YAML boolean is an easy
        slip, and admin is an implicit owner on *every* project (see enforce),
        so a typo here must not widen access.

        Contrast is_disabled below: identical shape, opposite risk direction.
        This one grants, so it fails closed on anything but a real ``True``.
        """
        return self.users.get(user_id, {}).get("admin", False) is True

    def is_disabled(self, user_id: str) -> bool:
        """Return True if the user is disabled (kill-switch, users.<id>.disabled).

        Truthiness is correct here, and is not an oversight. This check *denies*
        access, so a malformed value should still lock the user out:
        ``disabled: "true"``, ``disabled: "yes"`` and ``disabled: 1`` all mean
        the operator wanted this account off. Tightening this to ``is True``
        would silently re-enable an account someone believed was disabled.

        The rule across both methods: granting checks fail closed, denying
        checks fail open. Same code, opposite direction, on purpose.
        """
        return bool(self.users.get(user_id, {}).get("disabled", False))

    def enforce(self, user_id: str, project: str, need: str = "reader") -> None:
        """Raise AccessDenied unless `user_id` has at least `need` on `project`."""
        # Kill-switch: a disabled user is denied everything, admin included. This
        # is the boundary the admin UI's per-user disable toggle relies on; the
        # lockout-prevention guard (can't disable yourself / the last admin) lives
        # in the write path, not here — enforce must fail closed regardless.
        if self.is_disabled(user_id):
            raise AccessDenied(f"{user_id} is disabled")
        # Unknown project is an error for everyone, including admin — an admin
        # acting on a typo'd/nonexistent project should get a clear failure, not
        # a silent pass that masks the mistake.
        if project not in self.projects:
            raise AccessDenied(f"unknown project: {project}")
        if self.is_admin(user_id):
            return  # admin has implicit owner on every (existing) project
        role = self.grants(user_id).get(project)
        if role is None:
            raise AccessDenied(f"{user_id} has no access to {project}")
        if _rank(role) < _rank(need):
            raise AccessDenied(f"{user_id} needs {need} on {project} (has {role})")

    def resource(self, project: str, key: str):
        """Look up a project resource (mailbox/calendar/files/vault). Returns None if absent."""
        return self.projects.get(project, {}).get(key)

    def visible_projects(self, user_id: str) -> list[str]:
        if self.is_admin(user_id):
            return sorted(self.projects.keys())
        return sorted(self.grants(user_id).keys())
