# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public booking-link registry — memaix-src card 2bef1062.

Maps an opaque, unguessable slug to the (project, host user) it books
against. The slug IS the capability, the same convention rule_webhook uses
for its token (rules/match.py) — knowing it is enough to look up available
slots and attempt a booking, no login required. Provisioned by an operator
dropping a JSON file (config/booking_links/<slug>.json); no MCP tool
creates these yet — deliberately deferred until the booking epic needs
self-service link creation.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import config


def _links_dir() -> Path:
    d = config.CONFIG_DIR / "booking_links"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_link(slug: str) -> dict | None:
    """{"project", "user", "duration_min", "title_template"} for *slug*, or
    None if unknown. Never raises on missing/malformed slugs — callers
    treat both as a 404, not a 500.

    Optional fields an operator may add to the JSON file:
      "host_email": address the host receives their own booking-confirmation
        email at (card 14666e8a). Omitted -> the host simply isn't emailed;
        the visitor's own confirmation is unaffected.
      "host_timezone": IANA name (e.g. "Europe/Stockholm") used to format
        the time shown in the host's copy of the confirmation email, and —
        when the host has no working hours configured — to align the start
        times /book/{slug}/times offers. Omitted -> shown in UTC.
      "granularity_min": spacing of the start times /book/{slug}/times
        offers, e.g. 15 to offer quarter past and quarter to. Omitted -> 30.
        A query parameter of the same name overrides it per request.
      "origins": extra origins the embedded widget may call this link from,
        e.g. ["https://example.com"]. Added to the two hardcoded ones in
        routes.py rather than replacing them, so a typo here can't take
        booking off the air on jimlov.se or memaix.se.
      "turnstile_site_key": the Cloudflare Turnstile *site* key the widget
        renders its captcha with (public — it appears in every page that
        shows one; the secret lives in config under turnstile_secret_ref).
        Omitted -> the gateway-wide memaix.booking.turnstile_site_key.
      "consent_text": the exact wording the visitor agrees to, recorded
        verbatim with the booking. Omitted -> the widget uses its own
        built-in text for the page's language.
    """
    if not slug or "/" in slug or ".." in slug:
        return None
    path = _links_dir() / f"{slug}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("project") or not data.get("user"):
        return None
    return data


def list_links() -> list[tuple[str, dict]]:
    """Every valid (slug, link) pair, via get_link so malformed files are
    skipped the same way a request for them would be."""
    pairs = []
    for path in sorted(_links_dir().glob("*.json")):
        link = get_link(path.stem)
        if link is not None:
            pairs.append((path.stem, link))
    return pairs
