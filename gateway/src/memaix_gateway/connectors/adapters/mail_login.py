# SPDX-License-Identifier: AGPL-3.0-or-later
"""IMAP connect + LOGIN with a short, bounded retry for transient failures.

Why this exists: a provider hiccup (Purelymail dropping the socket during
LOGIN, `command: LOGIN => socket error: EOF`, 2026-11-18) used to fail the
whole mail tool call at once. Every IMAP login in the gateway (the shared
acl.yaml mailbox in tools/email.py and the per-user mailbox in
mail_imap_user.py) goes through `connect_and_login` here, so there is one
place that decides what is retried.

The rule, fail closed:

  * Transient transport failures (connection reset, EOF, timeout, TLS
    hiccup, imaplib's `abort`) are retried at most twice (three attempts
    in all) with a short backoff. The LAST error is the one reported.
  * Anything that says "the credentials were refused" (imap_tools'
    MailboxLoginError, any non-abort imaplib `error`, or an `abort` that is
    not a plain transport failure, e.g. `* BYE Authentication failed`) is
    final after ONE attempt. A retry
    must never become a password guess against the provider.
  * Anything else (a bug, a bad config value) is not retried and is raised
    unchanged: an unknown error is never relabelled as "temporary".

Each attempt builds a fresh connection: `imap_tools.MailBox(...)` connects in
its constructor and a dead imaplib client cannot log in again.

Errors carry the account's display name only, never host, user name or
password, and the cause chain is cut (`from None`) so a traceback cannot
leak what the library put in its message.
"""

from __future__ import annotations

import errno
import imaplib
import logging
import socket
import ssl
import time
from collections.abc import Callable
from typing import Any

try:  # the tests swap `imap_tools` for a stand-in that only has MailBox
    from imap_tools.errors import MailboxLoginError as _MailboxLoginError
except ImportError:  # pragma: no cover - imap_tools is a hard dependency
    _MailboxLoginError = None  # type: ignore[assignment,misc]

_log = logging.getLogger(__name__)

# Seconds to wait before retry 1 and retry 2. len(...) + 1 attempts in all.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.5)
MAX_ATTEMPTS = len(RETRY_BACKOFF_SECONDS) + 1
# Connect + LOGIN. imaplib keeps it as the socket timeout, so after login the
# socket is given the longer OPERATION_TIMEOUT: a server-side TEXT search over
# a big mailbox can be silent for a while and must not look like a failure.
CONNECT_TIMEOUT_SECONDS = 30
OPERATION_TIMEOUT_SECONDS = 120

_TRANSIENT_ERRNOS = {
    errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ETIMEDOUT,
    errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN,
}
# imaplib raises `abort` both for a dead socket and for a server that answers
# `* BYE <its own text>` (a refusal such as "Authentication failed" or "Too
# many login attempts" arrives that way). Only the first kind is a transport
# failure, so abort is a WHITELIST: it is retried only when its text names a
# transport problem and does not come from a BYE.
_TRANSPORT_MARKERS = (
    "socket error", "eof", "connection reset", "connection aborted", "broken pipe", "timed out", "timeout",
)
_AUTH_WORDS = (
    "authentication", "authorization", "credential", "login", "password", "too many",
    "disabled", "suspended", "denied", "locked", "blocked", "not allowed",
)
# Whole call per source, all attempts and pauses included.
TOTAL_TIMEOUT_SECONDS = 45
# A transport failure DURING the LOGIN command cannot be told from a provider
# that drops the connection on a wrong password, so it gets one retry only.
MAX_LOGIN_CALLS = 2


class MailLoginError(Exception):
    """Base of the two login failures the mail tools understand."""

    code = "mail_login_failed"

    def __init__(self, account: str) -> None:
        self.account = account
        super().__init__(f"{self.code}: {account}")


class MailAuthFailed(MailLoginError):
    """The server refused the credentials. Retrying will not help."""

    code = "mail_auth_failed"


class MailTemporarilyUnavailable(MailLoginError):
    """The connection or LOGIN kept failing in transit. Try again later."""

    code = "mail_temporarily_unavailable"


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _classify_abort(text: str) -> str:
    low = text.lower()
    if "bye" not in low and any(m in low for m in _TRANSPORT_MARKERS):
        return "transient"
    # The server's own words (or something we do not recognise): final.
    return "auth" if any(w in low for w in _AUTH_WORDS) else "unknown"


def _classify(exc: BaseException) -> str:
    """"transient" (retry), "auth" (refused, final) or "unknown" (re-raise)."""
    if isinstance(exc, imaplib.IMAP4.abort):  # subclass of IMAP4.error: test it FIRST
        return _classify_abort(str(exc))
    if isinstance(exc, imaplib.IMAP4.error) or (_MailboxLoginError is not None and isinstance(exc, _MailboxLoginError)):
        return "auth"
    return "transient" if _is_transient(exc) else "unknown"


def _is_transient(exc: BaseException) -> bool:
    """Transport failures raised by sockets/TLS. Whitelist: unknown means final."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False  # permanent, and its text names the host
    if isinstance(exc, ConnectionError | TimeoutError | ssl.SSLError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _TRANSIENT_ERRNOS or exc.errno == socket.EAI_AGAIN or "eof" in str(exc).lower()
    return False


def _discard(mailbox: Any) -> None:
    """Best-effort close of a half-open connection; never raises."""
    try:
        client = getattr(mailbox, "client", None)
        if client is not None:
            client.shutdown()
    # Nothing useful to do with a dead socket.
    except Exception:  # noqa: BLE001
        pass


class _Run:
    """What one connect_and_login call has used up so far."""

    def __init__(self, account: str, now: Callable[[], float]) -> None:
        self.account = account
        self.now = now
        self.started = now()
        self.login_calls = 0
        self.phase = "connect"


def _attempt(connect: Callable[[], Any], user: str, password: str, run: _Run) -> Any:
    """One connection and LOGIN. A half-open connection is closed on failure."""
    mailbox = None
    run.phase = "connect"
    try:
        mailbox = connect()
        run.phase = "login"
        run.login_calls += 1
        mailbox.login(user, password)
    except Exception:
        if mailbox is not None:
            _discard(mailbox)
        raise
    return mailbox


def _delay_before_retry(exc: Exception, run: _Run, attempt: int) -> float:
    """Seconds to wait before the next attempt, or raise: MailAuthFailed for a
    refusal (never retried), `exc` itself when it is unknown, and
    MailTemporarilyUnavailable once the tries or the time are used up."""
    verdict = _classify(exc)
    if verdict == "auth":
        _log.warning("mail login refused for %s (%s)", run.account, type(exc).__name__)
        raise MailAuthFailed(run.account) from None
    if verdict != "transient":
        raise exc
    _log.warning(
        "mail %s failed in transit for %s (%s), attempt %d of %d",
        run.phase, run.account, type(exc).__name__, attempt, MAX_ATTEMPTS,
    )
    delay = RETRY_BACKOFF_SECONDS[attempt - 1] if attempt <= len(RETRY_BACKOFF_SECONDS) else 0.0
    if _budget_spent(run, attempt, delay):
        raise MailTemporarilyUnavailable(run.account) from None
    return delay


def _budget_spent(run: _Run, attempt: int, delay: float) -> bool:
    out_of_tries = attempt == MAX_ATTEMPTS or (run.phase == "login" and run.login_calls >= MAX_LOGIN_CALLS)
    out_of_time = run.now() - run.started + delay >= TOTAL_TIMEOUT_SECONDS
    return out_of_tries or out_of_time


def connect_and_login(
    connect: Callable[[], Any],
    user: str,
    password: str,
    *,
    account: str,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> Any:
    """`connect()` (builds a new MailBox) then `.login(user, password)`.

    Returns the logged-in mailbox. Raises MailTemporarilyUnavailable after
    MAX_ATTEMPTS transient failures (at most MAX_LOGIN_CALLS of them during
    LOGIN itself, and none once TOTAL_TIMEOUT_SECONDS has passed),
    MailAuthFailed straight away on a refusal, and anything else unchanged.
    """
    wait = sleep or _sleep
    run = _Run(account, clock or time.monotonic)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            mailbox = _attempt(connect, user, password, run)
        except Exception as exc:  # noqa: BLE001
            # Classified in _delay_before_retry: unknown errors are re-raised there.
            wait(_delay_before_retry(exc, run, attempt))
            continue
        _widen_timeout(mailbox)
        return mailbox
    raise MailTemporarilyUnavailable(account)  # pragma: no cover - loop always returns or raises


def _widen_timeout(mailbox: Any) -> None:
    try:
        mailbox.client.sock.settimeout(OPERATION_TIMEOUT_SECONDS)
    # A stand-in or closed socket: keep the connect timeout.
    except Exception:  # noqa: BLE001
        pass
