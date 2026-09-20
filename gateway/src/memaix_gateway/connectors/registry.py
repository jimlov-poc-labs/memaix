# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connector registry — maps a project resource's `type` to an adapter
factory (FEATURE-CONNECTOR-FRAMEWORK.md §5).

Deliberate simplification vs. the design doc's illustrative factory
signature: factories here receive `(acl, project, user, resource_cfg,
token)` rather than a bare `resource_cfg` + resolved `secret`, because the
adapters being wrapped (`_make_mailbox`, `_RealDavAdapter`) already resolve
their own `*_ref` secrets from `resource_cfg` via `config.secret` — handing
them `acl`/`project` lets them do that themselves instead of duplicating
field-name knowledge in the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Capability -> the type assumed when a project resource doesn't set `type`
# explicitly, preserving today's acl.yaml files (mailbox/calendar configs
# have never carried a `type` key — imap/caldav were always implied).
DEFAULT_TYPES: dict[str, str] = {
    "mail": "imap",
    "calendar": "caldav",
    "contacts": "carddav",
    "files": "webdav",
    "tasks": "caldav",
    "deck": "nextcloud",
    "notes": "nextcloud",
}

# Capability name -> the acl.yaml resource key it actually reads today.
# 'mail' predates this framework under a different resource name
# ('mailbox'); keeping the capability name generic (matching the design
# doc) while mapping it to the real key avoids renaming every project's
# acl.yaml. 'files' has no such legacy — it's a brand-new resource key
# ('files' in acl.yaml), deliberately NOT aliased to 'vault': the local
# vault (tools/files.py) isn't behind this framework and has a completely
# different resource shape (a bare path string, not a {type, url, ...}
# dict) — see FEATURE-NEXTCLOUD-BACKEND.md, this is an *additional* files
# source (nc_files_* tools), not a replacement for the vault.
RESOURCE_KEYS: dict[str, str] = {
    "mail": "mailbox",
}


class ConnectorAuthRequired(Exception):
    """Raised when an auth='per_user' connector has no linked account for this user."""

    def __init__(self, capability: str, type_: str) -> None:
        self.capability = capability
        self.type = type_
        super().__init__(f"auth_required: no {type_!r} account linked for capability {capability!r}")


@dataclass(frozen=True)
class ConnectorSpec:
    type: str
    capability: str            # 'mail' | 'calendar' | 'files' | 'contacts' | 'chat' | 'issues'
    auth: str                  # 'shared' | 'per_user'
    factory: Callable           # (acl, project, user, resource_cfg, token) -> adapter
    provider: str | None = None  # token_store provider name for auth='per_user'; defaults to `type`


class ConnectorRegistry:
    """type->factory lookup per capability. Empty until `register()`d."""

    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], ConnectorSpec] = {}

    def register(self, *specs: ConnectorSpec) -> None:
        for spec in specs:
            self._specs[(spec.capability, spec.type)] = spec

    @staticmethod
    def _scoped_accounts(
        token_store, user: str, provider: str, capability: str, project: str
    ) -> list[dict]:
        """This user's linked accounts for `provider` that `project` may use.

        The single project-scope gate. Every mail and calendar tool resolves
        its sources through get()/get_all(), so filtering here covers the
        whole surface without touching a single tool. Shared acl.yaml
        resources deliberately bypass it — those belong to the project, not
        to the user, and were never the user's to scope.

        Accounts flagged `needs_relink` are dropped here too. That flag is
        set when a refresh has already been tried and failed (see server.py's
        _ensure_fresh_*_mail_token), so the credential is known-dead, not
        merely suspect — handing it to a factory buys a guaranteed 401 at
        request time instead of a clear answer now.

        It matters most in get_all(), where sources are merged: one revoked
        Google token would otherwise take down the listing for every healthy
        mailbox alongside it, turning one account's expiry into a total mail
        outage. In get() the effect is a better error — ConnectorAuthRequired
        ("re-link this account"), which the UI already knows how to act on,
        rather than a bare upstream 401.

        Dropping is not hiding: the flag itself is the durable record, and
        the settings page renders it as 🟡 "behöver kopplas om" next to the
        account.
        """
        return [
            a
            for a in token_store.list_accounts(user)
            if a["provider"] == provider
            and a.get("status") != "needs_relink"
            and token_store.is_allowed(user, provider, a["account"], capability, project)
        ]

    def get(self, acl, token_store, project: str, capability: str, user: str):
        """Resolve `acl.resource(project, capability)`'s `type` to a spec, resolve
        credentials per its `auth` mode, and build the adapter via its factory.

        Raises ValueError if the resource isn't configured or `type` is
        unregistered; ConnectorAuthRequired if auth='per_user' and the user
        has no linked account for it.
        """
        resource_cfg = acl.resource(project, RESOURCE_KEYS.get(capability, capability))
        if not resource_cfg:
            raise ValueError(f"project {project!r} has no {capability} configured")

        type_ = resource_cfg.get("type", DEFAULT_TYPES.get(capability, capability))
        spec = self._specs.get((capability, type_))
        if spec is None:
            raise ValueError(f"unknown connector type {type_!r} for capability {capability!r}")

        token = None
        if spec.auth == "per_user":
            provider = spec.provider or spec.type
            accounts = self._scoped_accounts(token_store, user, provider, capability, project)
            match = next(iter(accounts), None)
            if match is None:
                raise ConnectorAuthRequired(capability, spec.type)
            token = token_store.load_one(user, provider, match["account"])
            if token is None:
                raise ConnectorAuthRequired(capability, spec.type)

        return spec.factory(acl, project, user, resource_cfg, token)

    def capabilities_for_provider(self, provider: str) -> list[str]:
        """Which capabilities a linked account of `provider` can serve, as the
        registry stands right now — the set of checkboxes the settings UI
        offers for that account.

        Deliberately derived from the LIVE registry, which is the exact
        opposite of what catalog.LEGACY_PER_USER_CAPABILITIES must do. That
        map answers "what could this provider do when scoping was
        introduced?" and has to stay frozen, or registering a new adapter
        would retroactively widen grants that were never opted into. This
        answers "what can it do now?", where a newly registered adapter
        SHOULD appear — as an unchecked box the owner may tick.

        Only auth='per_user' specs count: shared acl.yaml resources belong to
        the project, not to the user, and are never scoped here.
        """
        return sorted(
            {
                spec.capability
                for spec in self._specs.values()
                if spec.auth == "per_user" and (spec.provider or spec.type) == provider
            }
        )

    def get_spec(self, capability: str, type_: str) -> "ConnectorSpec | None":
        """Look up a registered spec directly, bypassing acl.resource(...)
        resolution — for callers (e.g. calendar_sources.py) building an
        adapter from a resource_cfg that isn't the project's configured
        resource (a user-added public link, not acl.yaml)."""
        return self._specs.get((capability, type_))

    def get_all(self, acl, token_store, project: str, capability: str, user: str) -> list[tuple[str, object]]:
        """Resolve EVERY source configured for this capability, not just one
        (get()'s single-adapter shape). Added for memaix-src card 4daa20e2
        (calendar aggregation) — a person/project can have more than one
        calendar (multiple linked Google accounts, or a shared source
        alongside a personal one).

        Two origins, combined:
        1. The base resource_cfg (today's single-resource shape). For
           auth='per_user' specs this now yields ONE adapter PER linked
           account of that provider (get() only ever used the first) — this
           is what makes "aggregate my three Google calendars" possible.
           For auth='shared' it yields the same single adapter get() would.
        2. Resource_cfg['sources'] — an optional list of additional shared
           source configs (each shaped like a resource_cfg), for e.g. a
           second CalDAV calendar alongside a per-user Google chain.

        Per-user accounts from either origin are filtered through
        _scoped_accounts: linking an account no longer makes it visible
        everywhere, only in the projects it's been scoped to for this
        capability.

        Never raises ConnectorAuthRequired or ValueError for missing/unlinked
        sources — those just contribute zero adapters. An empty result means
        "nothing configured/linked yet", which callers (calendar_cache.py)
        treat as zero sources to sync, not an error.

        Returns [(label, adapter), ...]. label is for cache/debugging only,
        never surfaced as a stable identifier."""
        resource_cfg = acl.resource(project, RESOURCE_KEYS.get(capability, capability))
        results: list[tuple[str, object]] = []
        handled_types: set[str] = set()

        if resource_cfg:
            base_type = resource_cfg.get("type", DEFAULT_TYPES.get(capability, capability))
            base_spec = self._specs.get((capability, base_type))
            handled_types.add(base_type)

            if base_spec is not None:
                if base_spec.auth == "per_user":
                    provider = base_spec.provider or base_spec.type
                    accounts = self._scoped_accounts(
                        token_store, user, provider, capability, project
                    )
                    for account in accounts:
                        token = token_store.load_one(user, provider, account["account"])
                        if token is None:
                            continue
                        label = f"{base_spec.type}:{account['account']}"
                        results.append((label, base_spec.factory(acl, project, user, resource_cfg, token)))
                else:
                    label = f"{base_spec.type}:{project}"
                    results.append((label, base_spec.factory(acl, project, user, resource_cfg, None)))

            for i, extra_cfg in enumerate(resource_cfg.get("sources") or []):
                extra_type = extra_cfg.get("type", base_type)
                spec = self._specs.get((capability, extra_type))
                if spec is None:
                    continue
                token = None
                if spec.auth == "per_user":
                    provider = spec.provider or spec.type
                    accounts = self._scoped_accounts(
                        token_store, user, provider, capability, project
                    )
                    match = next(iter(accounts), None)
                    if match is None:
                        continue
                    token = token_store.load_one(user, provider, match["account"])
                    if token is None:
                        continue
                label = extra_cfg.get("label") or f"{extra_type}:{i}"
                results.append((label, spec.factory(acl, project, user, extra_cfg, token)))

        # Per-user sweep: pick up any linked per-user accounts not already
        # covered by the base type or extra sources above.  This makes Google
        # OAuth and iCal-secret tokens appear as calendar sources automatically
        # without requiring a matching `type:` in acl.yaml — the token_store
        # is the source of truth for "which accounts has this user linked?"
        for (cap, type_), spec in self._specs.items():
            if cap != capability or type_ in handled_types:
                continue
            if spec.auth != "per_user":
                continue
            handled_types.add(type_)
            provider = spec.provider or spec.type
            for account in self._scoped_accounts(
                token_store, user, provider, capability, project
            ):
                token = token_store.load_one(user, provider, account["account"])
                if token is None:
                    continue
                label = f"{type_}:{account['account']}"
                results.append((label, spec.factory(acl, project, user, resource_cfg or {}, token)))

        return results


_registry: ConnectorRegistry | None = None


def default_registry() -> ConnectorRegistry:
    """Process-wide registry populated with the built-in catalog (lazy singleton,
    same pattern as outbox.queue.default_queue())."""
    global _registry
    if _registry is None:
        from .catalog import register_defaults

        _registry = ConnectorRegistry()
        register_defaults(_registry)
    return _registry
