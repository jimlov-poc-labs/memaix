# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification channel adapters — email / webhook / ntfy.

See docs/FEATURE-PROACTIVE-BRIEF.md §4. Each channel's send() is called
independently by notify.deliver so one broken channel never blocks the others.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


def mask_channel(spec: dict) -> dict:
    """A channel spec safe to show: ntfy topics, webhook URLs and tokens
    are masked (a topic or URL is all it takes to read or post to it)."""
    return {key: _masked(key, value) for key, value in (spec or {}).items()}


def _masked(key: str, value):
    """One channel field as mask_channel shows it."""
    if key in ("topic", "url") and isinstance(value, str) and value:
        return _masked_url(value) if key == "url" else f"{value[:2]}***"
    if key.endswith("_ref") or key in ("token", "password"):
        return "***"
    return value


def _masked_url(url: str) -> str:
    """Scheme and host only: the path and query can carry a token."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if not parts.scheme:
        return "***"
    return f"{parts.scheme}://{parts.hostname or ''}/***"


def mask_prefs(prefs: dict | None) -> dict | None:
    if prefs is None:
        return None
    return {**prefs, "channels": [mask_channel(c) for c in prefs.get("channels") or []]}


class NotificationChannel(Protocol):
    def send(self, subject: str, markdown: str, text: str) -> None: ...


class EmailChannel:
    """Sends via a project's own mailbox/SMTP config (spec must include
    'project' — a brief spans multiple projects, so the sending identity is
    explicit rather than inferred)."""

    def __init__(self, acl, spec: dict, *, _smtp=None) -> None:
        self._acl = acl
        self._to = spec["to"]
        self._project = spec.get("project")
        self._smtp = _smtp

    # channel interface fixes this signature
    def send(self, subject: str, markdown: str, text: str) -> None:  # NOSONAR
        cfg = self._acl.resource(self._project, "mailbox") if self._project else None
        if not cfg:
            raise ValueError(
                f"email channel needs a 'project' with a configured mailbox (got {self._project!r})"
            )
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["To"] = self._to
        msg["Subject"] = subject
        msg["From"] = cfg.get("user", "")
        msg.set_content(text)

        if self._smtp is not None:
            self._smtp.send_message(msg)
            return

        from .. import config as cfg_mod
        smtp_cfg: dict = self._acl.resource(self._project, "smtp") or {}
        host = smtp_cfg.get("host", cfg.get("host", "localhost"))
        port = int(smtp_cfg.get("port", 587))
        password = cfg_mod.secret(cfg.get("password_ref"))
        if password is None:
            raise ValueError(f"no password configured for mailbox (project {self._project!r})")
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(cfg.get("user", ""), password)
            s.send_message(msg)


_DEFAULT_PORTS = {"https": 443, "http": 80}


class ChannelRefused(ValueError):
    """A channel Memaix will not send through. The message is shown to the
    user, so it never contains the URL, topic or token."""


_NTFY_HTTPS = "ntfy måste vara https: ämnet är lika hemligt som en Discord-token"


def _destination(url: str) -> tuple[str, str, int, str, str]:
    """(scheme, host, port, path, query) the HTTP client will really use.

    Raises BlockedURLError if the URL is not a plain external http(s) URL
    or its host is ambiguous between parsers (safety/net.py). Parsed with
    urllib3, the parser requests sends with, so an identity built from it
    describes where the POST goes — not where urlparse thinks it goes."""
    from urllib3.util import parse_url

    from ..safety.net import sent_host, validate_external_url

    validate_external_url(url, resolve=False)
    parts = parse_url(url)
    scheme = (parts.scheme or "").lower()
    return scheme, sent_host(url), parts.port or _DEFAULT_PORTS.get(scheme, 0), parts.path or "", parts.query or ""


def _discord_id(url: str) -> str | None:
    """Webhook id if `url` is a Discord webhook Memaix may post to, else None.

    Raises ValueError for a URL on a Discord host that is not https on the
    standard port: the path carries the webhook token, so plain http or an
    odd port would send it somewhere it should not go."""
    from ..tools.mail_unlock import _DISCORD_HOSTS, _DISCORD_PATH

    scheme, host, port, path, _query = _destination(url)
    if host.rstrip(".") not in _DISCORD_HOSTS:
        return None
    if scheme != "https" or port != 443:
        raise ChannelRefused("Discord-webhooks måste vara https på standardport")
    m = _DISCORD_PATH.match(path)
    return m.group(1) if m else None


def ntfy_url(server: str | None, topic: str) -> str:
    """The URL NtfyChannel posts to (one place, so identity and send agree)."""
    return f"{(server or 'https://ntfy.sh').rstrip('/')}/{topic}"


def check_ntfy_url(url: str) -> None:
    """Raise ChannelRefused unless `url` (from ntfy_url) is https.

    The topic is the only thing between the world and the messages on it,
    so like a Discord token it never travels in the clear (Jimmy 2026-10-26).
    Any port: a self-hosted server may serve https elsewhere than 443."""
    scheme, *_rest = _destination(url)
    if scheme != "https":
        raise ChannelRefused(_NTFY_HTTPS)


class WebhookChannel:
    def __init__(self, url: str, fmt: str = "raw", *, _http=None) -> None:
        # Refuse ambiguous / non-https Discord URLs at construction, so an
        # already stored bad spec is skipped by build_channels on every send.
        _discord_id(url)
        self._url = url
        self._fmt = fmt
        self._http = _http

    _DISCORD_DESC_LIMIT = 4096

    def send(self, subject: str, markdown: str, text: str) -> None:
        http = self._http
        if http is None:
            import requests
            http = requests
            from ..safety.net import validate_external_url
            validate_external_url(self._url)  # authoritative SSRF check before the real request
        payload: dict[str, object]
        if self._fmt == "slack":
            payload = {"text": f"*{subject}*\n{text}"}
        elif self._fmt == "discord":
            desc = markdown
            if len(desc) > self._DISCORD_DESC_LIMIT:
                desc = desc[: self._DISCORD_DESC_LIMIT - 1] + "…"
            payload = {"embeds": [{"title": subject, "description": desc, "color": 0x2ECC71}]}
        else:
            payload = {"subject": subject, "text": text, "markdown": markdown}
        # allow_redirects=False — se kommentaren i safety/net.py. 307 bevarar POST-body,
        # så en omdirigering skickar hela nyttolasten vidare till målet.
        resp = http.post(self._url, json=payload, timeout=10, allow_redirects=False)
        raise_for_status = getattr(resp, "raise_for_status", None)
        if raise_for_status:
            raise_for_status()


def _header_safe(value: str) -> str:
    """HTTP/1.1 header values are latin-1 only (requests/http.client will
    raise UnicodeEncodeError on anything outside that range). ntfy.sh reads
    non-ASCII Title values via RFC 2047 encoded-words, so encode only when
    the value actually needs it — pure-ASCII text passes through untouched."""
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        from email.header import Header
        return Header(value, "utf-8").encode()
    return value


class NtfyChannel:
    def __init__(self, topic: str, server: str = "https://ntfy.sh", *, _http=None) -> None:
        self._topic = topic
        self._server = server.rstrip("/")
        self._http = _http
        # Refused at construction, so an already stored http spec is skipped
        # by build_channels on every send.
        check_ntfy_url(ntfy_url(self._server, self._topic))

    # channel interface fixes this signature
    def send(self, subject: str, markdown: str, text: str) -> None:  # NOSONAR
        http = self._http
        url = ntfy_url(self._server, self._topic)
        if http is None:
            import requests
            http = requests
            from ..safety.net import validate_external_url
            validate_external_url(url)  # authoritative SSRF check before the real request
        headers = {"Title": _header_safe(subject)}
        # allow_redirects=False — se kommentaren i safety/net.py.
        resp = http.post(
            url,
            data=text.encode("utf-8"),
            headers=headers,
            timeout=10,
            allow_redirects=False,
        )
        raise_for_status = getattr(resp, "raise_for_status", None)
        if raise_for_status:
            raise_for_status()


def channel_identity(spec: dict) -> str:
    """Comparable identity of a brief channel's destination.

    Used to decide whether a destination is already stored, so it must not
    match loosely: two specs share an identity only if they reach the same
    place. Email is (sending project, recipient). A Discord webhook is its
    id, but only over https on the standard port to a Discord host (aliases
    reach the same webhook). Any other webhook, and ntfy, is scheme + host +
    port + path + query as the HTTP client parses it. A URL that
    safety/net.py refuses gets an identity that matches nothing. Unlike the
    unlock check's webhook_identity, which matches loosely on purpose
    because there a match means "refuse".
    """
    import secrets

    from ..safety.net import BlockedURLError

    ctype = (spec or {}).get("type")
    if ctype == "email":
        return f"email:{spec.get('project') or ''}:{str(spec.get('to') or '').strip().lower()}"
    if ctype not in ("webhook", "ntfy"):
        return f"other:{ctype}"
    try:
        if ctype == "webhook":
            url = str(spec.get("url") or "")
            hook_id = _discord_id(url)
            if hook_id:
                return f"discord:{hook_id}"
            prefix = "url"
        else:
            url = ntfy_url(spec.get("server"), str(spec.get("topic") or ""))
            check_ntfy_url(url)
            prefix = "ntfy"
        scheme, host, port, path, query = _destination(url)
    except (BlockedURLError, ValueError):
        return f"invalid:{secrets.token_hex(8)}"
    return f"{prefix}:{scheme}://{host}:{port}{path}?{query}"


def may_send_from(acl, user: str | None, project: str | None) -> bool:
    """An email channel sends from `project`'s mailbox: only its owner may
    point that mailbox anywhere (RBAC, AGENTS.md §2)."""
    if acl is None or not user or not project:
        return False
    try:
        acl.enforce(user, project, "owner")
    except Exception:  # noqa: BLE001
        # AccessDenied, or anything else: fail closed
        return False
    return True


def _refusal_reason(exc: Exception) -> str:
    """Why a channel was skipped, safe to show: never the URL or topic."""
    from ..safety.net import BlockedURLError

    if isinstance(exc, ChannelRefused):
        return str(exc)
    if isinstance(exc, BlockedURLError):
        return "URL:en vägras (SSRF-skydd eller tvetydig adress)"
    return f"kanalen kunde inte byggas ({type(exc).__name__})"


_KNOWN_TYPES = ("email", "webhook", "ntfy")


def build_channels(
    specs: list[dict], *, acl=None, user: str | None = None, _http=None, _smtp=None,
    rejected: list[dict] | None = None,
) -> list[NotificationChannel]:
    """Build channel adapters from JSON specs, skipping (and logging) any
    that fail to construct — one bad spec must not disable the others.

    Every skipped spec is also appended to `rejected` (when given) as
    {index, type, reason}, so the caller can tell the user instead of
    failing silently. Never the spec itself: it holds the URL / topic.

    An email channel is built only when `user` owns the channel's project.
    This is checked here, at delivery, and not only when the channel is
    configured: every sender (brief_send_now, the scheduled brief, rule
    notify) goes through this function, and a stored spec may predate a
    grant being revoked."""
    channels: list[NotificationChannel] = []
    for index, spec in enumerate(specs or []):
        ctype = spec.get("type")
        try:
            built = _build_channel(ctype, spec, acl=acl, user=user, _http=_http, _smtp=_smtp)
        except Exception as exc:
            # Not the spec itself: it holds the webhook URL / ntfy topic.
            logger.warning("failed to build notification channel of type %r", ctype)
            _reject(rejected, index, ctype, _refusal_reason(exc))
            continue
        if isinstance(built, str):
            _reject(rejected, index, ctype, built)
        else:
            channels.append(built)
    return channels


def _reject(rejected: list[dict] | None, index: int, ctype: object, reason: str) -> None:
    """Record a skipped spec for build_channels' caller: never the spec itself."""
    if rejected is not None:
        shown = ctype if ctype in _KNOWN_TYPES else "okänd"
        rejected.append({"index": index, "type": shown, "reason": reason})


def _build_channel(ctype: object, spec: dict, *, acl, user: str | None, _http, _smtp) -> NotificationChannel | str:
    """The adapter for one spec, or the reason (shown to the user) it is
    skipped. Raises if the spec cannot be built."""
    from .. import config as cfg_mod

    if ctype == "email":
        if not may_send_from(acl, user, spec.get("project")):
            logger.warning("email channel skipped: user is not owner of its project")
            return "du är inte ägare av projektet som e-posten skickas från"
        return EmailChannel(acl, spec, _smtp=_smtp)
    if ctype == "webhook":
        url = spec.get("url") or cfg_mod.secret(spec.get("url_ref"))
        if not url:
            raise ValueError("webhook channel needs 'url' or 'url_ref'")
        return WebhookChannel(url, spec.get("format", "raw"), _http=_http)
    if ctype == "ntfy":
        return NtfyChannel(spec["topic"], spec.get("server", "https://ntfy.sh"), _http=_http)
    logger.warning("unknown notification channel type: %r", ctype)
    return "okänd kanaltyp"


def refused_channels(specs: list[dict], *, acl=None, user: str | None = None) -> list[dict]:
    """The stored channels a send would skip, with type and reason. Builds
    the adapters and throws them away; nothing is sent."""
    rejected: list[dict] = []
    build_channels(specs, acl=acl, user=user, rejected=rejected)
    return rejected
