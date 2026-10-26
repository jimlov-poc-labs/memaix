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
    from urllib.parse import urlsplit

    out: dict = {}
    for key, value in (spec or {}).items():
        if key in ("topic", "url") and isinstance(value, str) and value:
            if key == "url":
                parts = urlsplit(value)
                out[key] = f"{parts.scheme}://{parts.hostname or ''}/***" if parts.scheme else "***"
            else:
                out[key] = f"{value[:2]}***"
        elif key.endswith("_ref") or key in ("token", "password"):
            out[key] = "***"
        else:
            out[key] = value
    return out


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


class WebhookChannel:
    def __init__(self, url: str, fmt: str = "raw", *, _http=None) -> None:
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

    # channel interface fixes this signature
    def send(self, subject: str, markdown: str, text: str) -> None:  # NOSONAR
        http = self._http
        url = f"{self._server}/{self._topic}"
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
    id (globally unique; host aliases and the token cannot point elsewhere).
    Any other webhook is scheme + host + port + path + query — unlike the
    unlock check's webhook_identity, which matches loosely on purpose
    because there a match means "refuse".
    """
    from urllib.parse import urlsplit

    from ..tools.mail_unlock import ntfy_identity, webhook_identity

    ctype = (spec or {}).get("type")
    if ctype == "email":
        return f"email:{spec.get('project') or ''}:{str(spec.get('to') or '').strip().lower()}"
    if ctype == "webhook":
        url = str(spec.get("url") or "").strip()
        loose = webhook_identity(url)
        if loose.startswith("discord:"):
            return loose
        parts = urlsplit(url)
        return f"url:{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{parts.port}{parts.path}?{parts.query}"
    if ctype == "ntfy":
        return ntfy_identity(spec.get("server"), spec.get("topic"))
    return f"other:{ctype}"


def may_send_from(acl, user: str | None, project: str | None) -> bool:
    """An email channel sends from `project`'s mailbox: only its owner may
    point that mailbox anywhere (RBAC, AGENTS.md §2)."""
    if acl is None or not user or not project:
        return False
    try:
        acl.enforce(user, project, "owner")
    except Exception:  # noqa: BLE001 - AccessDenied, or anything else: fail closed
        return False
    return True


def build_channels(
    specs: list[dict], *, acl=None, user: str | None = None, _http=None, _smtp=None,
) -> list[NotificationChannel]:
    """Build channel adapters from JSON specs, skipping (and logging) any
    that fail to construct — one bad spec must not disable the others.

    An email channel is built only when `user` owns the channel's project.
    This is checked here, at delivery, and not only when the channel is
    configured: every sender (brief_send_now, the scheduled brief, rule
    notify) goes through this function, and a stored spec may predate a
    grant being revoked."""
    from .. import config as cfg_mod

    channels: list[NotificationChannel] = []
    for spec in specs or []:
        ctype = spec.get("type")
        try:
            if ctype == "email":
                if not may_send_from(acl, user, spec.get("project")):
                    logger.warning("email channel skipped: user is not owner of its project")
                    continue
                channels.append(EmailChannel(acl, spec, _smtp=_smtp))
            elif ctype == "webhook":
                url = spec.get("url") or cfg_mod.secret(spec.get("url_ref"))
                if not url:
                    raise ValueError("webhook channel needs 'url' or 'url_ref'")
                channels.append(WebhookChannel(url, spec.get("format", "raw"), _http=_http))
            elif ctype == "ntfy":
                channels.append(NtfyChannel(spec["topic"], spec.get("server", "https://ntfy.sh"), _http=_http))
            else:
                logger.warning("unknown notification channel type: %r", ctype)
        except Exception:
            # Not the spec itself: it holds the webhook URL / ntfy topic.
            logger.warning("failed to build notification channel of type %r", ctype)
    return channels
