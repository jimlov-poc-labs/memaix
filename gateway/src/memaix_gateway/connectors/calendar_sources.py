# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-(project,user) calendar source selection + user-added public .ics
links — memaix-src backlog card 324dd801.

Two things this card adds on top of registry.get_all() (card 4daa20e2):
1. Letting a user exclude some of their linked/configured calendars from
   the aggregate (e.g. a private calendar they don't want to block bookings
   against) — an opt-*out* disabled-set, not an opt-in list. Default is
   "included": a newly linked account counts immediately, because silently
   NOT blocking a source is the dangerous failure for a booking tool
   (double-booking), not the safe one.
2. Letting a user add a public .ics/webcal URL as an extra source, without
   OAuth — reuses tools/calendar.py's existing _ICalAdapter (SSRF-checked),
   registered as connector type "public_ics" in catalog.py.

Storage: one JSON file per (project, user) in the project vault, same
convention as calendar_cache.py and calendar_overrides.py.

Stale entries in the disabled-set (e.g. a label for an account that was
since unlinked) are left in place, not garbage-collected — harmless (an
unresolvable label matches nothing) and desirable if the same account gets
re-linked later, since the user's prior exclusion choice is preserved.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from ..safety.net import validate_external_url
from .registry import ConnectorRegistry


def _sources_path(acl, project: str, user: str) -> Path:
    vault = acl.resource(project, "vault")
    if not vault:
        raise ValueError(f"project {project!r} has no vault configured")
    directory = Path(vault) / "calendar_sources"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user}.json"


def _webcal_to_https(url: str) -> str:
    """webcal:// is just a UA-convention alias for an https:// ics feed — no
    fetcher (_ICalAdapter included) speaks it. Rewrite at the door so both
    the stored URL and validate_external_url see a real http(s) URL."""
    scheme, sep, rest = url.partition("://")
    if sep and scheme.lower() == "webcal":
        return f"https://{rest}"
    return url


def _normalize_url(url: str) -> str:
    """Comparison key for dedupe — host lowercased, no trailing slash. Never
    used for the actual fetch."""
    parsed = urlsplit(_webcal_to_https(url))
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


class SourceSelectionStore:
    """JSON-backed disabled-set + public-links list for one (project, user)."""

    def __init__(self, acl, project: str, user: str) -> None:
        self._path = _sources_path(acl, project, user)

    def _read(self) -> dict:
        if not self._path.exists():
            return {"disabled": [], "public_links": []}
        return json.loads(self._path.read_text())

    def _write(self, data: dict) -> None:
        self._path.write_text(json.dumps(data))

    def list(self) -> dict:
        return self._read()

    def set_enabled(self, label: str, enabled: bool) -> None:
        """enabled=True removes *label* from the disabled-set (source counts
        again); enabled=False adds it. Idempotent either way."""
        data = self._read()
        disabled = set(data["disabled"])
        if enabled:
            disabled.discard(label)
        else:
            disabled.add(label)
        data["disabled"] = sorted(disabled)
        self._write(data)

    def add_public_link(self, url: str, label: str = "") -> dict:
        """Validate *url* (SSRF shape check, resolve=False — same config-time
        check as calendar_setup's ical_secret mode), dedupe by normalized
        URL, and persist. Returns the stored entry (existing one if this URL
        was already added)."""
        url = _webcal_to_https(url)
        validate_external_url(url, resolve=False)
        data = self._read()
        normalized = _normalize_url(url)
        for existing in data["public_links"]:
            if _normalize_url(existing["url"]) == normalized:
                return existing
        entry = {"id": f"pl_{secrets.token_hex(4)}", "label": label, "url": url}
        data["public_links"].append(entry)
        self._write(data)
        return entry

    def remove_public_link(self, link_id: str) -> bool:
        data = self._read()
        links = data["public_links"]
        for i, entry in enumerate(links):
            if entry["id"] == link_id:
                links.pop(i)
                self._write(data)
                return True
        return False


def resolve_effective_sources(
    acl,
    token_store,
    project: str,
    user: str,
    *,
    registry: ConnectorRegistry | None = None,
) -> list[tuple[str, object]]:
    """The source list calendar_cache.sync_user_calendar aggregates:
    registry.get_all(...) minus this user's disabled-set, plus one
    public_ics adapter per stored, non-disabled public link (label
    f"public_ics:{id}"). Never raises for a bad/unreachable link — a
    failing link just contributes an adapter whose list_events() raises,
    surfaced per-source via the existing busy_from_backend/
    CalendarSourceError path at sync time."""
    from .registry import default_registry

    registry = registry or default_registry()
    store = SourceSelectionStore(acl, project, user)
    data = store.list()
    disabled = set(data["disabled"])

    pairs = registry.get_all(acl, token_store, project, "calendar", user)
    results = [(label, adapter) for label, adapter in pairs if label not in disabled]

    if "native" not in disabled and any(
        a["provider"] == "native" for a in token_store.list_accounts(user)
    ):
        from .native_calendar import NativeCalendarAdapter, native_path

        results.append(("native", NativeCalendarAdapter(native_path(acl, project, user))))

    spec = registry.get_spec("calendar", "public_ics")
    if spec is not None:
        for link in data["public_links"]:
            label = f"public_ics:{link['id']}"
            if label in disabled:
                continue
            adapter = spec.factory(acl, project, user, {"type": "public_ics", "url": link["url"]}, None)
            results.append((label, adapter))

    return results
