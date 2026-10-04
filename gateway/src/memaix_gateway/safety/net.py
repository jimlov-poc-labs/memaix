# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSRF guard for user-supplied outbound URLs.

Some tools accept a URL from an authenticated user (or an agent acting as
them) that the server then fetches/posts to: an iCal secret URL
(calendar_setup) and notification channel webhook/ntfy URLs
(brief_configure). Without a guard, a user — or a prompt-injected agent —
can point those at internal targets (cloud metadata at 169.254.169.254,
localhost services, RFC1918 hosts) and turn the gateway into a confused
deputy (SSRF).

validate_external_url() rejects anything that isn't a plain http(s) URL to a
publicly-routable host. It's applied twice on purpose: at configuration time
(fast, clear rejection when the user sets the URL) and again immediately
before the actual request in the adapter (authoritative — narrows the
DNS-rebinding TOCTOU window, since a name that resolved public at set-time
could later resolve to a private IP). The residual rebind race between this
check and connect() is documented, not eliminated; fully closing it would
require pinning the resolved IP into the socket, which the stdlib HTTP
clients don't expose cleanly.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class BlockedURLError(ValueError):
    """Raised when a user-supplied URL targets a non-public / disallowed host."""


# Carrier-grade NAT (also Tailscale): neither "private" nor "reserved" in the stdlib.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → block
    v4 = addr.ipv4_mapped if addr.version == 6 else addr
    if v4 is not None and v4 in _CGNAT:
        return True
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _is_literal_ip(host: str) -> bool:
    # Parse in a try (ValueError = "not a literal IP, it's a hostname"); the
    # block decision is made by the caller, OUTSIDE the try, because
    # BlockedURLError subclasses ValueError and would be swallowed here.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _require_http_host(url: str):
    """Parse ``url`` and return (parsed, hostname); raise BlockedURLError for
    an empty/non-string URL, a non-http(s) scheme or a missing host."""
    if not url or not isinstance(url, str):
        raise BlockedURLError("empty or non-string URL")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError(f"URL scheme must be http/https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise BlockedURLError("URL has no host")
    return parsed, host


def _check_resolved_addresses(host: str, parsed) -> None:
    """Resolve ``host`` and raise BlockedURLError if ANY address is non-public."""
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise BlockedURLError(f"could not resolve host {host!r}: {exc}") from exc
    for info in infos:
        ip = str(info[4][0])
        if _is_blocked_ip(ip):
            raise BlockedURLError(f"URL host {host!r} resolves to a non-public address: {ip}")


def validate_external_url(url: str, *, resolve: bool = True) -> str:
    """Return the URL unchanged if it's a safe external http(s) target, else
    raise BlockedURLError.

    resolve=True (the fetch-time check) additionally resolves the hostname
    and blocks if ANY resolved address is non-public. resolve=False
    (config-time) skips DNS and only validates scheme/host shape — so setting
    a URL doesn't depend on the name resolving right then.

    CALLERS MUST DISABLE REDIRECTS.

    This function validates the URL you are about to request. It cannot see
    where a 3xx sends you afterwards. ``requests`` follows redirects by default
    on every verb except HEAD, so a validated public host can answer::

        302 Location: http://169.254.169.254/latest/meta-data/...

    and the guard never runs against the real target. A 307 additionally
    preserves the POST body, forwarding the whole payload.

    Every call site that fetches must therefore pass ``allow_redirects=False``
    (requests) or ``follow_redirects=False`` (httpx). Current call sites:

        tools/calendar.py       iCal fetch
        notify/channels.py      webhook + ntfy POST
        llm/client.py           already correct

    If a caller genuinely needs to follow a redirect, it has to re-validate the
    Location header before making the next request. Nothing here does that
    today, which is why the blanket rule is "no redirects".
    """
    parsed, host = _require_http_host(url)

    # A literal IP in the URL is checked directly (no DNS needed).
    if _is_literal_ip(host):
        if _is_blocked_ip(host):
            raise BlockedURLError(f"URL host is a non-public address: {host}")
        return url

    if resolve:
        _check_resolved_addresses(host, parsed)
    return url
