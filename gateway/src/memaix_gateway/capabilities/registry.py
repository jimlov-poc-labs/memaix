# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capability registry — the source of truth for "what can Memaix do".

Every user-facing function (onboarding tour, memaix_help, board panel, nudges)
reads from this registry instead of hardcoding a list, so a new MCP tool is
only discoverable once someone deliberately registers a Capability for it.
A companion coverage test (tests/test_capabilities_coverage.py) fails CI if a
tool is neither registered nor explicitly marked internal — see
docs/FEATURE-DISCOVERABILITY.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Mirrors acl.ROLES or the role hierarchy in acl.Acl; duplicated here to keep
# this module import-independent of acl.py (no circular import risk).
_ROLES = ("reader", "collaborator", "owner")


def _rank(role: str | None) -> int:
    if role is None:
        return -1
    try:
        return _ROLES.index(role)
    except ValueError:
        return -1


AREAS: tuple[str, ...] = (
    "memory", "mail", "calendar", "backlog", "pm",
    "brief", "search", "automation", "undo", "outbox", "contacts", "access",
)


@dataclass(frozen=True)
class Capability:
    """One discoverable "thing Memaix can do", grouped by outcome area."""

    key: str
    area: str
    title_key: str
    summary_key: str
    tools: tuple[str, ...]
    example_prompts_key: str
    needs_role: str = "reader"
    needs_resource: str | None = None   # 'mailbox' | 'calendar' | 'vault' | None
    needs_account: str | None = None    # 'google' | 'microsoft' | None
    tags: tuple[str, ...] = field(default_factory=tuple)


_REGISTRY: list[Capability] = []


def register(*caps: Capability) -> None:
    """Add one or more capabilities to the registry (idempotent by key)."""
    existing = {c.key for c in _REGISTRY}
    for cap in caps:
        if cap.key not in existing:
            _REGISTRY.append(cap)
            existing.add(cap.key)


def all_capabilities() -> list[Capability]:
    return list(_REGISTRY)


def clear_registry() -> None:
    """For testing only — reset the module-level registry."""
    _REGISTRY.clear()


def _account_providers(accounts: list[dict] | None) -> set[str]:
    providers: set[str] = set()
    for a in accounts or []:
        provider = a.get("provider")
        if provider:
            providers.add(provider)
    return providers


def available_for(
    acl, user_id: str, accounts: list[dict] | None = None
) -> tuple[list[Capability], list[dict]]:
    """Split the registry into (available, locked) for this user.

    A capability is available if the user has >= needs_role on at least one
    visible project, AND (if needs_resource) that project (or another
    qualifying one) has the resource configured, AND (if needs_account) the
    account provider is linked. Locked entries carry a machine-readable reason
    so callers can render an upgrade path ("link_google", "no_mailbox",
    "no_role") without leaking which specific project was checked.
    """
    grants = acl.grants(user_id)
    providers = _account_providers(accounts)

    available: list[Capability] = []
    locked: list[dict] = []

    for cap in all_capabilities():
        qualifying = [p for p, role in grants.items() if _rank(role) >= _rank(cap.needs_role)]
        if not qualifying:
            locked.append({"capability": cap, "reason": "no_role"})
            continue

        if cap.needs_account and cap.needs_account not in providers:
            locked.append({"capability": cap, "reason": f"link_{cap.needs_account}"})
            continue

        if cap.needs_resource:
            has_resource = any(acl.resource(p, cap.needs_resource) for p in qualifying)
            if not has_resource:
                locked.append({"capability": cap, "reason": f"no_{cap.needs_resource}"})
                continue

        available.append(cap)

    return available, locked


def group_by_area(capabilities: list[Capability]) -> dict[str, list[Capability]]:
    """Group a capability list by area, preserving registration order."""
    grouped: dict[str, list[Capability]] = {}
    for cap in capabilities:
        grouped.setdefault(cap.area, []).append(cap)
    return grouped
