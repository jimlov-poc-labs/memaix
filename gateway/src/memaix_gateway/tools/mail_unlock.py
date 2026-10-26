# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session send unlock for outgoing mail (docs/FEATURE-MAIL-DIALOG.md).

Sending mail from a chat (email_forward, email_reply, and email_send in a
project with `send_unlock: true`) needs the MCP session to be unlocked
first. The flow:

  1. email_send_unlock_request — the server makes a one-time code and sends
     it OUTSIDE the model, through a configured channel (ntfy, webhook, or
     a dedicated SMTP account). The tool result never contains the code.
  2. The owner reads the code on their phone/mail and types it in the chat.
  3. email_send_unlock(code) — unlocks exactly this (user, MCP session) for
     a short TTL (default 4 h).

Why the channel is checked: a code mailed to a mailbox the model can read
through email_list/email_search would let the model unlock itself. A
channel is refused if its sender or recipient is any address a mail tool can
read (every acl.yaml mailbox, every linked account of any user). See
`readable_addresses` and `check_channel`.

State is in memory on purpose: a restart locks every session again, which
is the safe direction, and the MCP streamable-HTTP sessions it is bound to
live in this process too. Codes are kept only as an HMAC with a per-process
key, never in clear and never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

DEFAULT_TTL_HOURS = 4.0
DEFAULT_CODE_TTL_MINUTES = 10.0
DEFAULT_MAX_ATTEMPTS = 5
# Unlock requests per user per window — every request reaches the owner's
# phone/mailbox, so a looping model must not be able to spam it.
DEFAULT_MAX_REQUESTS = 3
DEFAULT_REQUEST_WINDOW_MINUTES = 15.0
_CODE_DIGITS = 8

UNLOCK_SUBJECT = "Memaix: kod för att låsa upp mejlutskick"


class SendLocked(PermissionError):
    """Raised when a send is attempted in a session that is not unlocked."""


@dataclass
class _Pending:
    digest: bytes
    expires: float
    attempts: int = 0


@dataclass
class _UserRequests:
    stamps: list[float] = field(default_factory=list)


def settings(cfg: dict | None) -> dict:
    """memaix.mail_send_unlock from config, with defaults filled in."""
    raw = (((cfg or {}).get("memaix") or {}).get("mail_send_unlock") or {})
    return {
        "enabled": bool(raw.get("enabled", False)),
        "ttl_hours": float(raw.get("ttl_hours", DEFAULT_TTL_HOURS)),
        "code_ttl_minutes": float(raw.get("code_ttl_minutes", DEFAULT_CODE_TTL_MINUTES)),
        "max_attempts": int(raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
        "max_requests": int(raw.get("max_requests", DEFAULT_MAX_REQUESTS)),
        "request_window_minutes": float(raw.get("request_window_minutes", DEFAULT_REQUEST_WINDOW_MINUTES)),
        "channels": list(raw.get("channels") or []),
    }


class SendUnlockStore:
    """Pending codes and unlocked sessions, keyed by (user, session_id)."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._unlocked: dict[tuple[str, str], float] = {}
        self._requests: dict[str, _UserRequests] = {}

    def _digest(self, user: str, session: str, code: str) -> bytes:
        msg = f"{user}\x00{session}\x00{code.strip()}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).digest()

    def new_code(
        self, user: str, session: str, *, code_ttl_minutes: float, max_requests: int, window_minutes: float,
    ) -> str:
        """Make a fresh code for (user, session); replaces any pending one.

        Raises RuntimeError when the user has asked too often. The returned
        code must only ever go to a delivery channel.
        """
        if not user or not session:
            raise ValueError("user and session are required")
        now = self._clock()
        with self._lock:
            req = self._requests.setdefault(user, _UserRequests())
            req.stamps = [t for t in req.stamps if now - t < window_minutes * 60]
            if len(req.stamps) >= max_requests:
                raise RuntimeError(
                    f"rate_limited: högst {max_requests} upplåsningskoder per {int(window_minutes)} minuter"
                )
            req.stamps.append(now)
            code = f"{secrets.randbelow(10 ** _CODE_DIGITS):0{_CODE_DIGITS}d}"
            self._pending[(user, session)] = _Pending(
                self._digest(user, session, code), now + code_ttl_minutes * 60
            )
            return code

    def verify(self, user: str, session: str, code: str, *, ttl_hours: float, max_attempts: int) -> dict:
        """Check `code` for (user, session). On success the session is unlocked.

        A wrong code counts against the pending code; at `max_attempts` the
        code is burnt and a new request is needed. Returns {ok, ...}; never
        raises for a wrong code so the tool can report attempts left.
        """
        now = self._clock()
        key = (user, session)
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                return {"ok": False, "error": "ingen giltig kod — begär en ny med email_send_unlock_request"}
            if now >= pending.expires:
                del self._pending[key]
                return {"ok": False, "error": "koden har gått ut — begär en ny"}
            if hmac.compare_digest(pending.digest, self._digest(user, session, str(code or ""))):
                del self._pending[key]
                self._unlocked[key] = now + ttl_hours * 3600
                return {"ok": True, "unlocked_until": self._unlocked[key]}
            pending.attempts += 1
            left = max_attempts - pending.attempts
            if left <= 0:
                del self._pending[key]
                return {"ok": False, "error": "fel kod — för många försök, koden är förbrukad", "attempts_left": 0}
            return {"ok": False, "error": "fel kod", "attempts_left": left}

    def unlocked_until(self, user: str, session: str) -> float | None:
        """Epoch seconds the session stays unlocked, or None if it is locked."""
        if not user or not session:
            return None
        now = self._clock()
        with self._lock:
            until = self._unlocked.get((user, session))
            if until is None:
                return None
            if now >= until:
                del self._unlocked[(user, session)]
                return None
            return until

    def is_unlocked(self, user: str, session: str) -> bool:
        return self.unlocked_until(user, session) is not None

    def lock(self, user: str, session: str) -> bool:
        with self._lock:
            self._pending.pop((user, session), None)
            return self._unlocked.pop((user, session), None) is not None


# ------------------------------------------------------------------
# Delivery channel
# ------------------------------------------------------------------


def _norm(addr: str) -> str:
    from email.utils import parseaddr

    return parseaddr(addr or "")[1].strip().lower()


def readable_addresses(acl, token_store, *, users: list[str] | None = None) -> set[str]:
    """Every mailbox address some mail tool can read.

    acl.yaml mailboxes (and their smtp from_addr) in every project, plus
    every account linked in the token store by any acl user, whatever the
    provider or project scope — scopes can change later without the
    channel config being looked at again.
    """
    out: set[str] = set()
    for project, cfg in (getattr(acl, "projects", {}) or {}).items():
        mailbox = (cfg or {}).get("mailbox") or {}
        smtp = (cfg or {}).get("smtp") or {}
        for value in (mailbox.get("user"), smtp.get("user"), smtp.get("from_addr")):
            if value:
                out.add(_norm(value))
    names = users if users is not None else list((getattr(acl, "users", {}) or {}).keys())
    if token_store is not None:
        for user in names:
            for account in token_store.list_accounts(user):
                if account.get("account"):
                    out.add(_norm(account["account"]))
    out.discard("")
    return out


def check_channel(spec: dict, readable: set[str]) -> None:
    """Raise ValueError if `spec` could put the code where a mail tool reads it."""
    ctype = spec.get("type")
    if ctype in ("ntfy", "webhook"):
        return
    if ctype != "smtp":
        raise ValueError(
            f"kanaltyp {ctype!r} stöds inte för upplåsningskoder (använd ntfy, webhook eller smtp)"
        )
    for field_name in ("to", "from", "user"):
        value = _norm(str(spec.get(field_name) or ""))
        if field_name in ("to", "from") and not value:
            raise ValueError(f"smtp-kanalen saknar {field_name!r}")
        if value and value in readable:
            raise ValueError(
                f"smtp-kanalens {field_name} ({value}) är en låda som Memaix mejlverktyg kan läsa — "
                "då kan modellen läsa koden själv. Välj en låda utanför Memaix, eller ntfy."
            )


class SmtpCodeChannel:
    """Sends the code from a dedicated SMTP account (not a Memaix mailbox)."""

    def __init__(self, spec: dict, *, _smtp_factory=None) -> None:
        self._spec = spec
        self._smtp_factory = _smtp_factory

    def send(self, subject: str, markdown: str, text: str) -> None:  # NOSONAR - channel interface
        import smtplib
        from email.message import EmailMessage

        from .. import config

        msg = EmailMessage()
        msg["From"] = self._spec["from"]
        msg["To"] = self._spec["to"]
        msg["Subject"] = subject
        msg.set_content(text)
        if self._smtp_factory is not None:
            self._smtp_factory(self._spec).send_message(msg)
            return
        password = config.secret(self._spec.get("password_ref"))
        if password is None:
            raise ValueError("smtp-kanalen saknar password_ref")
        host = self._spec["host"]
        port = int(self._spec.get("port", 587))
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as s:
                s.login(self._spec.get("user") or self._spec["from"], password)
                s.send_message(msg)
            return
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=context)
            s.login(self._spec.get("user") or self._spec["from"], password)
            s.send_message(msg)


def build_code_channels(specs: list[dict], readable: set[str], *, _http=None, _smtp_factory=None) -> list:
    """Channel objects for `specs`, each checked against `readable` first.

    Raises ValueError on the first unsafe or unknown channel: a half-safe
    channel list is not something to quietly work around.
    """
    from ..notify.channels import NtfyChannel, WebhookChannel

    channels: list = []
    for spec in specs:
        check_channel(spec, readable)
        ctype = spec["type"]
        if ctype == "ntfy":
            channels.append(NtfyChannel(spec["topic"], spec.get("server", "https://ntfy.sh"), _http=_http))
        elif ctype == "webhook":
            from .. import config

            url = spec.get("url") or config.secret(spec.get("url_ref"))
            if not url:
                raise ValueError("webhook-kanalen behöver 'url' eller 'url_ref'")
            channels.append(WebhookChannel(url, spec.get("format", "raw"), _http=_http))
        else:
            channels.append(SmtpCodeChannel(spec, _smtp_factory=_smtp_factory))
    return channels


def code_message(code: str, ttl_minutes: float) -> str:
    return (
        f"Din kod för att låsa upp mejlutskick i Memaix: {code}\n\n"
        f"Koden gäller i {int(ttl_minutes)} minuter och bara för den chattsession som bad om den. "
        "Skriv in den i chatten om det var du som bad om den. Har du inte bett om någon kod: "
        "skriv inte in den någonstans."
    )


def channel_hint(spec: dict) -> str:
    """A non-secret description of a channel for the tool result."""
    ctype = spec.get("type", "?")
    if ctype == "smtp":
        to = _norm(str(spec.get("to") or ""))
        local, _, domain = to.partition("@")
        return f"mejl till {local[:1]}***@{domain}" if domain else "mejl"
    if ctype == "ntfy":
        return "ntfy-notis"
    return str(ctype)
