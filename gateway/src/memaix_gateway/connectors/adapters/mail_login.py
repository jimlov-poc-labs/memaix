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
    MailboxLoginError, any non-abort imaplib `error`, or an `abort` whose
    text is an authentication refusal) is final after ONE attempt. A retry
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
_AUTH_MARKERS = ("authenticationfailed", "invalid credentials", "login failed", "authorizationfailed")


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


def _is_auth_refusal(exc: BaseException) -> bool:
    if _MailboxLoginError is not None and isinstance(exc, _MailboxLoginError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def _is_transient(exc: BaseException) -> bool:
    """True only for failures in transit. Whitelist: unknown means final."""
    if isinstance(exc, imaplib.IMAP4.abort):  # subclass of IMAP4.error: test it FIRST
        return True
    if isinstance(exc, imaplib.IMAP4.error):
        return False
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
    except Exception:  # noqa: BLE001 - nothing useful to do with a dead socket
        pass


def connect_and_login(
    connect: Callable[[], Any],
    user: str,
    password: str,
    *,
    account: str,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """`connect()` (builds a new MailBox) then `.login(user, password)`.

    Returns the logged-in mailbox. Raises MailTemporarilyUnavailable after
    MAX_ATTEMPTS transient failures, MailAuthFailed straight away on a
    refusal, and anything else unchanged.
    """
    wait = sleep or _sleep
    for attempt in range(1, MAX_ATTEMPTS + 1):
        mailbox = None
        try:
            mailbox = connect()
            mailbox.login(user, password)
        except Exception as exc:  # noqa: BLE001 - classified below, unknown ones re-raised
            if mailbox is not None:
                _discard(mailbox)
            if _is_auth_refusal(exc) or (
                isinstance(exc, imaplib.IMAP4.error) and not isinstance(exc, imaplib.IMAP4.abort)
            ):
                _log.warning("mail login refused for %s (%s)", account, type(exc).__name__)
                raise MailAuthFailed(account) from None
            if not _is_transient(exc):
                raise
            _log.warning(
                "mail login failed in transit for %s (%s), attempt %d of %d",
                account, type(exc).__name__, attempt, MAX_ATTEMPTS,
            )
            if attempt == MAX_ATTEMPTS:
                raise MailTemporarilyUnavailable(account) from None
            wait(RETRY_BACKOFF_SECONDS[attempt - 1])
            continue
        _widen_timeout(mailbox)
        return mailbox
    raise MailTemporarilyUnavailable(account)  # pragma: no cover - loop always returns or raises


def _widen_timeout(mailbox: Any) -> None:
    try:
        mailbox.client.sock.settimeout(OPERATION_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - a stand-in or closed socket: keep the connect timeout
        pass
