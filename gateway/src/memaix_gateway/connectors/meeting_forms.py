# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-host enabled meeting forms (video/phone options offered at booking
time) — memaix-src card 85854d2c.

Distinct from meeting_types.py (duration/interval presets, card d0a1f633):
that module answers "how long is the meeting", this one answers "how does
the visitor actually join it" (Google Meet, Zoom, or a phone number). Same
storage convention as meeting_types.py/booking_settings.py — one JSON file
per (project, user) in the project vault — and the same validate/store
split, but the domain rules differ: forms are keyed by *provider*, and a
phone form additionally carries a phone number in its config.

No stored forms (or an unconfigured store) means [] — booking_create
treats that as "this host hasn't opted into the meeting-form feature",
leaving booking behavior identical to what it was before this card
existed. Kept host-scoped, not link-scoped: a meeting form is backed by
the host's own Zoom/Google account, not a per-link communication
preference (unlike reminders.py's per-link reminder_offsets override).

Physical/in-person forms (address + travel-time validation) are explicitly
out of scope here — deferred to a future card.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
KNOWN_PROVIDERS = {"google_meet", "zoom", "phone"}


def _meeting_forms_path(acl, project: str, user: str) -> Path:
    vault = acl.resource(project, "vault")
    if not vault:
        raise ValueError(f"project {project!r} has no vault configured")
    directory = Path(vault) / "meeting_forms"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user}.json"


def _form_config(slug: str, provider: str, f: dict) -> dict:
    """Normalized config for one form: only phone forms carry host config."""
    if provider != "phone":
        # google_meet/zoom generate their link per booking — nothing
        # host-configurable beyond the label.
        return {}
    config = dict(f.get("config") or {})
    phone_number = str(config.get("phone_number") or "").strip()
    if not phone_number:
        raise ValueError(f"{slug!r}: phone form requires config.phone_number")
    config["phone_number"] = phone_number
    return config


def _validate_form(f, slugs: set[str]) -> dict:
    """Validate one entry and return its normalized copy; records its slug."""
    if not isinstance(f, dict):
        raise ValueError(f"each meeting form must be an object, got {f!r}")
    slug = f.get("slug", "")
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid slug {slug!r}: must be lowercase alphanumeric/hyphen")
    if slug in slugs:
        raise ValueError(f"duplicate slug {slug!r}")
    slugs.add(slug)

    provider = f.get("provider", "")
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(f"{slug!r}: unknown provider {provider!r}, must be one of {sorted(KNOWN_PROVIDERS)}")

    label = f.get("label", "")
    if not label:
        raise ValueError(f"{slug!r}: label is required")

    return {
        "slug": slug,
        "provider": provider,
        "label": label,
        "config": _form_config(slug, provider, f),
        "default": bool(f.get("default", False)),
    }


def validate_forms(forms: list[dict]) -> list[dict]:
    """Raise ValueError if *forms* is not a valid list of meeting forms.
    Returns a normalized copy with exactly one entry marked default
    (auto-promoting the first if none was marked), same contract as
    meeting_types.validate_types."""
    slugs: set[str] = set()
    out = [_validate_form(f, slugs) for f in forms]

    defaults = [f for f in out if f["default"]]
    if len(defaults) > 1:
        raise ValueError("at most one meeting form may be marked default")
    if out and not defaults:
        out[0]["default"] = True

    return out


class MeetingFormsStore:
    def __init__(self, acl, project: str, user: str) -> None:
        self._path = _meeting_forms_path(acl, project, user)

    def get(self) -> list[dict]:
        """[] if never configured — no meeting forms offered."""
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text())

    def set(self, forms: list[dict]) -> list[dict]:
        normalized = validate_forms(forms)
        self._path.write_text(json.dumps(normalized))
        return normalized

    def delete(self, slug: str) -> list[dict]:
        """Remove one form by slug and persist the rest. No error if the
        slug isn't present — deleting an already-gone form is a no-op."""
        remaining = [f for f in self.get() if f["slug"] != slug]
        return self.set(remaining)
