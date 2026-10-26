# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session send unlock for outgoing mail (docs/FEATURE-MAIL-DIALOG.md).

Sending mail from a chat (email_forward, email_reply, and email_send in a
project with `send_unlock: true`) needs the MCP session to be unlocked
first. The flow:

  1. email_send_unlock_request — the server makes a one-time code and sends
     it OUTSIDE the model, through a configured channel (a Discord webhook,
     ntfy, a webhook, or a dedicated SMTP account). The tool result never
     contains the code.
  2. The owner reads the code on their phone/mail and types it in the chat.
  3. email_send_unlock(code) — unlocks exactly this (user, MCP session) for
     a short TTL (default 4 h).

Why the channel is checked: a code mailed to a mailbox the model can read
through email_list/email_search would let the model unlock itself. A
channel is refused if its sender or recipient is any address a mail tool can
read (every acl.yaml mailbox, every linked account of any user, aliases
and same-domain addresses included). An ntfy topic or webhook that is also
a brief channel is refused too: the model sets and reads those itself. The
webhook URL and the topic are never returned, logged or audited. See
`readable_addresses`, `exposed_identities` and `check_channel`.

State is in memory on purpose: a restart locks every session again, which
is the safe direction, and the MCP streamable-HTTP sessions it is bound to
live in this process too. Codes are kept only as an HMAC with a per-process
key, never in clear and never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import re
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

    def drop_pending(self, user: str, session: str) -> None:
        """Forget a pending code (the request is still charged)."""
        with self._lock:
            self._pending.pop((user, session), None)

    def lock(self, user: str, session: str) -> bool:
        with self._lock:
            self._pending.pop((user, session), None)
            return self._unlocked.pop((user, session), None) is not None


# ------------------------------------------------------------------
# Delivery channel
# ------------------------------------------------------------------

# Every error message below is shown to the model (deny()) and written to
# the audit log, so none of them may contain a webhook URL, an ntfy topic or
# a token. Only field names and channel types.

DEFAULT_NTFY_SERVER = "https://ntfy.sh"
# An ntfy topic is the only thing between the world and the messages on it,
# so the code topic must be unguessable (and should come from topic_ref).
MIN_NTFY_TOPIC_LEN = 24
_DISCORD_HOSTS = frozenset({
    "discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com",
    "ptb.discordapp.com", "canary.discordapp.com",
})
_DISCORD_PATH = re.compile(r"^/api/(?:v\d+/)?webhooks/(\d+)/[A-Za-z0-9_-]+/?$")
# Domains where every alias is derivable by normalisation (dots, +tag,
# googlemail.com), so a readable address on them does not have to block the
# whole domain. Other providers (Outlook, iCloud, Proton, own domains with
# catch-all) allow aliases we cannot derive: there the whole domain is out.
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def _norm(addr: str) -> str:
    from email.utils import parseaddr

    return parseaddr(addr or "")[1].strip().lower()


def canonical_address(addr: str) -> str:
    """Mailbox an address really lands in: lower case, no +tag, and for
    Gmail no dots and googlemail.com -> gmail.com."""
    value = _norm(addr)
    local, sep, domain = value.rpartition("@")
    if not sep:
        return value
    local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        domain = "gmail.com"
        local = local.replace(".", "")
    return f"{local}@{domain}"


def readable_addresses(acl, token_store, *, users: list[str] | None = None) -> set[str]:
    """Every mailbox address some mail tool can read.

    acl.yaml mailboxes (and their smtp from_addr) in every project, plus
    every account linked in the token store by any acl user, whatever the
    provider or project scope — scopes can change later without the
    channel config being looked at again.
    """
    out = _project_addresses(acl)
    names = users if users is not None else list((getattr(acl, "users", {}) or {}).keys())
    out |= _linked_addresses(token_store, names)
    out.discard("")
    return out


def _project_addresses(acl) -> set[str]:
    """acl.yaml mailbox user, smtp user and smtp from_addr of every project."""
    out: set[str] = set()
    for cfg in (getattr(acl, "projects", {}) or {}).values():
        mailbox = (cfg or {}).get("mailbox") or {}
        smtp = (cfg or {}).get("smtp") or {}
        values = (mailbox.get("user"), smtp.get("user"), smtp.get("from_addr"))
        out.update(_norm(value) for value in values if value)
    return out


def _linked_addresses(token_store, names: list[str]) -> set[str]:
    """Every account linked by any of `names`, whatever provider or scope."""
    if token_store is None:
        return set()
    return {
        _norm(account["account"])
        for user in names
        for account in token_store.list_accounts(user)
        if account.get("account")
    }


def _readable_hit(value: str, readable: set[str]) -> str | None:
    """Why `value` may land in a readable mailbox, or None."""
    canon = canonical_address(value)
    if canon in {canonical_address(r) for r in readable}:
        return "är (ett alias för) en låda"
    domain = canon.rpartition("@")[2]
    domains = {canonical_address(r).rpartition("@")[2] for r in readable}
    if domain and domain not in _GMAIL_DOMAINS and domain in domains:
        return "ligger på en domän där en låda"
    return None


def _resolved(spec: dict, key: str) -> str | None:
    """spec[key] or the secret behind spec[key + '_ref']."""
    from .. import config

    value = spec.get(key)
    if value:
        return str(value)
    return config.secret(spec.get(f"{key}_ref"))


def ntfy_identity(server: str | None, topic: str | None) -> str:
    """Comparable identity of an ntfy topic (scheme and case of host ignored)."""
    from urllib.parse import urlsplit

    parts = urlsplit((server or DEFAULT_NTFY_SERVER).strip())
    base = f"{(parts.hostname or '').lower()}{f':{parts.port}' if parts.port else ''}{parts.path.rstrip('/')}"
    return f"ntfy:{base}/{(topic or '').strip()}"


def webhook_identity(url: str | None) -> str:
    """Comparable identity of a webhook: the webhook id for Discord (all its
    host aliases are the same webhook), else host + path."""
    from urllib.parse import urlsplit

    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if host in _DISCORD_HOSTS:
        m = _DISCORD_PATH.match(parts.path)
        if m:
            return f"discord:{m.group(1)}"
    return f"url:{host}{parts.path.rstrip('/')}"


def exposed_identities(notify_store) -> set[str]:
    """ntfy topics and webhook URLs of every user's brief channels.

    The model sets those itself through brief_configure and can read them
    back, so a code channel must never be one of them.
    """
    out: set[str] = set()
    if notify_store is None:
        return out
    specs = (spec for channels in notify_store.all_channels() for spec in channels or [])
    for spec in specs:
        try:
            identity = _brief_identity(spec)
        except Exception:  # noqa: BLE001
            # a broken brief spec is not a code channel
            continue
        if identity:
            out.add(identity)
    return out


def _brief_identity(spec: dict | None) -> str | None:
    """Comparable identity of one brief channel's ntfy topic or webhook."""
    spec = spec or {}
    ctype = spec.get("type")
    if ctype == "ntfy" and spec.get("topic"):
        return ntfy_identity(spec.get("server"), spec.get("topic"))
    if ctype in ("webhook", "discord"):
        url = _resolved(spec, "url")
        return webhook_identity(url) if url else None
    return None


def _check_url(url: str | None, label: str) -> str:
    from urllib.parse import urlsplit

    if not url:
        raise ValueError(f"{label}-kanalen behöver 'url' eller 'url_ref'")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"{label}-kanalens URL måste vara https")
    if parts.query or parts.fragment:
        # ?wait=true would make the service return the message (and its id),
        # which together with the URL lets anyone read the code back.
        raise ValueError(f"{label}-kanalens URL får inte ha någon query-sträng (t.ex. ?wait=true)")
    return url


def check_channel(spec: dict, readable: set[str], exposed: set[str] | frozenset[str] = frozenset()) -> None:
    """Raise ValueError if `spec` could put the code where the model reads it.

    Error texts never contain the URL, topic or token.
    """
    ctype = spec.get("type")
    if ctype == "ntfy":
        _check_ntfy(spec, exposed)
        return
    if ctype in ("discord", "webhook"):
        _check_webhook(spec, ctype, exposed)
        return
    if ctype != "smtp":
        raise ValueError(
            f"kanaltyp {ctype!r} stöds inte för upplåsningskoder (använd discord, ntfy, webhook eller smtp)"
        )
    _check_smtp(spec, readable)


def _check_ntfy(spec: dict, exposed: set[str] | frozenset[str]) -> None:
    """Unguessable topic, https server, and not a topic the model can see."""
    from urllib.parse import urlsplit

    topic = _resolved(spec, "topic")
    if not topic:
        raise ValueError("ntfy-kanalen behöver 'topic' eller 'topic_ref'")
    if len(topic) < MIN_NTFY_TOPIC_LEN or not re.fullmatch(r"[A-Za-z0-9_-]+", topic):
        raise ValueError(
            f"ntfy-ämnet måste vara minst {MIN_NTFY_TOPIC_LEN} slumpade tecken (A-Z, a-z, 0-9, _ eller -) "
            "— ett gissningsbart ämne kan vem som helst läsa"
        )
    if urlsplit(str(spec.get("server") or DEFAULT_NTFY_SERVER)).scheme.lower() != "https":
        raise ValueError("ntfy-kanalens server måste vara https — ämnet får aldrig gå i klartext")
    if ntfy_identity(spec.get("server"), topic) in exposed:
        raise ValueError("ntfy-ämnet används också av en notiskanal som modellen kan se och ändra")


def _check_webhook(spec: dict, ctype: str, exposed: set[str] | frozenset[str]) -> None:
    """https without query, a real Discord webhook for discord, and not a
    webhook the model can see."""
    url = _check_url(_resolved(spec, "url"), ctype)
    if ctype == "discord" and not webhook_identity(url).startswith("discord:"):
        raise ValueError("discord-kanalen kräver en Discord-webhook (https://discord.com/api/webhooks/<id>/<token>)")
    if webhook_identity(url) in exposed:
        raise ValueError(f"{ctype}-kanalen pekar på samma webhook som en notiskanal som modellen kan se och ändra")


def _check_smtp(spec: dict, readable: set[str]) -> None:
    """to and from required; none of to, from and user may land in a
    mailbox a mail tool can read."""
    for field_name in ("to", "from", "user"):
        value = _norm(str(spec.get(field_name) or ""))
        if field_name in ("to", "from") and not value:
            raise ValueError(f"smtp-kanalen saknar {field_name!r}")
        hit = _readable_hit(value, readable) if value else None
        if hit:
            raise ValueError(
                f"smtp-kanalens {field_name} ({value}) {hit} som Memaix mejlverktyg kan läsa — "
                "då kan modellen läsa koden själv. Välj en låda utanför Memaix, eller discord."
            )


class _HttpCodeChannel:
    """POSTs the code and throws the response away.

    The response is never read or returned (a Discord message id plus the
    webhook URL is enough to fetch the message back), and failures are
    re-raised without the URL: requests' HTTPError text contains it.
    """

    def __init__(self, label: str, url: str, *, headers: dict | None = None, _http=None) -> None:
        self._label = label
        self._url = url
        self._headers = headers or {}
        self._http = _http

    def _payload(self, subject: str, text: str) -> dict:
        return {"json": {"subject": subject, "text": text}}

    def send(self, subject: str, markdown: str, text: str) -> None:  # NOSONAR - channel interface
        self._post(self._payload(subject, text))

    def _post(self, payload: dict) -> None:
        """One POST; the response body is never read."""
        http = self._http
        try:
            if http is None:
                import requests

                from ..safety.net import validate_external_url

                http = requests
                validate_external_url(self._url)
            resp = http.post(
                self._url, headers=self._headers, timeout=10, allow_redirects=False, **payload,
            )
            status = int(getattr(resp, "status_code", 200) or 200)
        except Exception:  # noqa: BLE001
            # the original text can hold the URL
            raise RuntimeError(f"{self._label}: leveransen misslyckades") from None
        if not 200 <= status < 300:
            raise RuntimeError(f"{self._label}: svarade {status}")


class DiscordCodeChannel(_HttpCodeChannel):
    """Discord webhook. A code goes out as two messages (send_code): first
    what it is for and how long it lasts, then the code alone as `content`,
    so it shows in the push notification and a long press on the phone
    copies exactly the code. No ?wait=true (refused in check_channel), no
    mentions; if either POST fails the channel has failed."""

    def __init__(self, url: str, *, _http=None) -> None:
        super().__init__("discord", url, _http=_http)

    @staticmethod
    def _message(content: str) -> dict:
        return {"json": {"content": content, "allowed_mentions": {"parse": []}}}

    def _payload(self, subject: str, text: str) -> dict:
        return self._message(f"**{subject}**\n{text}"[:2000])

    def send_code(self, code: str, ttl_minutes: float) -> None:
        """Explanation first, then a message holding nothing but the code.
        A failed explanation raises before the code is posted."""
        self._post(self._payload(UNLOCK_SUBJECT, code_explanation(ttl_minutes)))
        self._post(self._message(code))


class WebhookCodeChannel(_HttpCodeChannel):
    def __init__(self, url: str, *, _http=None) -> None:
        super().__init__("webhook", url, _http=_http)


class NtfyCodeChannel(_HttpCodeChannel):
    """ntfy, optionally with an access token (token_ref) for a protected topic."""

    def __init__(self, server: str | None, topic: str, token: str | None = None, *, _http=None) -> None:
        from ..notify.channels import _header_safe

        headers = {"Title": _header_safe(UNLOCK_SUBJECT)}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(
            "ntfy", f"{(server or DEFAULT_NTFY_SERVER).rstrip('/')}/{topic}", headers=headers, _http=_http,
        )

    def _payload(self, subject: str, text: str) -> dict:
        return {"data": text.encode("utf-8")}


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


def build_code_channels(
    specs: list[dict], readable: set[str], exposed: set[str] | frozenset[str] = frozenset(),
    *, _http=None, _smtp_factory=None,
) -> list:
    """Channel objects for `specs`, each checked against `readable` and
    `exposed` first.

    Raises ValueError on the first unsafe or unknown channel: a half-safe
    channel list is not something to quietly work around.
    """
    channels: list = []
    for spec in specs:
        check_channel(spec, readable, exposed)
        ctype = spec["type"]
        if ctype == "ntfy":
            channels.append(NtfyCodeChannel(
                spec.get("server"), _resolved(spec, "topic") or "", _resolved(spec, "token"), _http=_http,
            ))
        elif ctype == "discord":
            channels.append(DiscordCodeChannel(_resolved(spec, "url") or "", _http=_http))
        elif ctype == "webhook":
            channels.append(WebhookCodeChannel(_resolved(spec, "url") or "", _http=_http))
        else:
            channels.append(SmtpCodeChannel(spec, _smtp_factory=_smtp_factory))
    return channels


def _code_terms(ttl_minutes: float) -> str:
    return (
        f"Koden gäller i {int(ttl_minutes)} minuter och bara för den chattsession som bad om den. "
        "Skriv in den i chatten om det var du som bad om den. Har du inte bett om någon kod: "
        "skriv inte in den någonstans."
    )


def code_message(code: str, ttl_minutes: float) -> str:
    return f"Din kod för att låsa upp mejlutskick i Memaix: {code}\n\n{_code_terms(ttl_minutes)}"


def code_explanation(ttl_minutes: float) -> str:
    """What the code in the next message is for; never the code itself."""
    return f"Nästa meddelande är din kod för att låsa upp mejlutskick i Memaix.\n\n{_code_terms(ttl_minutes)}"


def deliver_code(channel, code: str, ttl_minutes: float) -> None:
    """Send `code` through one channel: its own send_code when it has one
    (Discord: explanation, then the bare code), else a single message."""
    send_code = getattr(channel, "send_code", None)
    if send_code is not None:
        send_code(code, ttl_minutes)
        return
    text = code_message(code, ttl_minutes)
    channel.send(UNLOCK_SUBJECT, text, text)


def channel_hint(spec: dict) -> str:
    """A non-secret description of a channel for the tool result."""
    ctype = spec.get("type", "?")
    if ctype == "smtp":
        to = _norm(str(spec.get("to") or ""))
        local, _, domain = to.partition("@")
        return f"mejl till {local[:1]}***@{domain}" if domain else "mejl"
    if ctype == "ntfy":
        return "ntfy-notis"
    if ctype == "discord":
        return "Discord"
    return "webhook" if ctype == "webhook" else "okänd kanal"
