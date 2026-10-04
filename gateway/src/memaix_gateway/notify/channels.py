# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification channel adapters — email / webhook / ntfy.

See docs/FEATURE-PROACTIVE-BRIEF.md §4. Each channel's send() is called
independently by notify.deliver so one broken channel never blocks the others.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


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

    def send(self, subject: str, markdown: str, text: str) -> None:  # NOSONAR: fixed by the channel interface
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

    def send(self, subject: str, markdown: str, text: str) -> None:  # NOSONAR: fixed by the channel interface
        http = self._http
        url = f"{self._server}/{self._topic}"
        if http is None:
            import requests
            http = requests
            from ..safety.net import validate_external_url
            validate_external_url(url)  # authoritative SSRF check before the real request
        # allow_redirects=False — se kommentaren i safety/net.py.
        resp = http.post(
            url,
            data=text.encode("utf-8"),
            headers={"Title": _header_safe(subject)},
            timeout=10,
            allow_redirects=False,
        )
        raise_for_status = getattr(resp, "raise_for_status", None)
        if raise_for_status:
            raise_for_status()


def build_channels(specs: list[dict], *, acl=None, _http=None, _smtp=None) -> list[NotificationChannel]:
    """Build channel adapters from JSON specs, skipping (and logging) any
    that fail to construct — one bad spec must not disable the others."""
    from .. import config as cfg_mod

    channels: list[NotificationChannel] = []
    for spec in specs or []:
        ctype = spec.get("type")
        try:
            if ctype == "email":
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
            logger.warning("failed to build notification channel %r", spec, exc_info=True)
    return channels
