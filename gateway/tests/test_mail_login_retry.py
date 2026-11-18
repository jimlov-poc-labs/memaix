# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: F811
"""IMAP LOGIN retry, the two clear errors, and one dead mailbox not taking the
others down with it (2026-11-18: `command: LOGIN => socket error: EOF`).

Everything runs against fakes; nothing connects anywhere.
"""

from __future__ import annotations

import errno
import imaplib
import logging
import ssl
import sys
import types

import pytest
from imap_tools.errors import MailboxLoginError

import memaix_gateway.connectors.adapters.mail_imap_user as t_imap_user
import memaix_gateway.connectors.adapters.mail_login as ml
import memaix_gateway.connectors.registry as registry_mod
import memaix_gateway.tools.email as t_email
from memaix_gateway import server
from memaix_gateway.acl import Acl
from memaix_gateway.connectors.adapters.mail_login import (
    MailAuthFailed,
    MailTemporarilyUnavailable,
    connect_and_login,
)
from tests.test_mail_multi_account_server import (  # noqa: F401 - fixtures
    _FakeMailbox,
    _Msg,
    token_store,
    wired,
)

SECRET = "hunter2-pw"
HOST = "imap.secret-host.example.com"
USER = "login-name-xyz"


class _Box:
    """MailBox stand-in whose login() raises the queued errors in order."""

    def __init__(self, script: list):
        self.script = script
        self.logins = 0
        self.closed = 0
        self.client = types.SimpleNamespace(shutdown=self._shutdown)

    def _shutdown(self):
        self.closed += 1

    def login(self, user, password):
        self.logins += 1
        outcome = self.script.pop(0) if self.script else None
        if outcome is not None:
            raise outcome
        return self


def _run(script, *, account="acct", connect=None):
    box = _Box(script)
    sleeps: list[float] = []
    mb = connect_and_login(connect or (lambda: box), USER, SECRET, account=account, sleep=sleeps.append)
    return mb, box, sleeps


def _fail(script, **kw):
    box = _Box(script)
    sleeps: list[float] = []
    with pytest.raises(Exception) as info:
        connect_and_login(kw.get("connect") or (lambda: box), USER, SECRET, account="acct", sleep=sleeps.append)
    return info.value, box, sleeps


# --- retry on transient failures ---------------------------------------------


def test_eof_at_login_is_retried_and_then_succeeds():
    eof = imaplib.IMAP4.abort("command: LOGIN => socket error: EOF")
    mb, box, sleeps = _run([eof])
    assert mb is box and box.logins == 2
    assert sleeps == [0.5]


def test_three_eofs_give_temporarily_unavailable_after_exactly_three_attempts():
    eof = lambda: imaplib.IMAP4.abort("command: LOGIN => socket error: EOF")  # noqa: E731
    exc, box, sleeps = _fail([eof(), eof(), eof(), eof(), eof()])
    assert isinstance(exc, MailTemporarilyUnavailable)
    assert str(exc) == "mail_temporarily_unavailable: acct"
    assert box.logins == 3  # never a fourth
    assert sleeps == [0.5, 1.5]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("reset"),
        BrokenPipeError("pipe"),
        TimeoutError("timed out"),
        ssl.SSLEOFError("EOF occurred in violation of protocol"),
        OSError(errno.ECONNRESET, "Connection reset by peer"),
        OSError("socket error: EOF"),
    ],
)
def test_transport_errors_are_retried(error):
    _, box, sleeps = _run([error])
    assert box.logins == 2 and sleeps == [0.5]


def test_a_failure_while_connecting_is_retried_with_a_fresh_connection():
    made: list[_Box] = []

    def connect():
        if not made:
            made.append(None)  # type: ignore[arg-type]
            raise ConnectionResetError("reset on connect")
        box = _Box([])
        made.append(box)
        return box

    mb, _, sleeps = _run([], connect=connect)
    assert mb is made[1] and sleeps == [0.5]


def test_the_dead_connection_is_closed_before_the_next_attempt():
    _, box, _ = _run([imaplib.IMAP4.abort("EOF")])
    assert box.closed == 1


# --- never retry authentication --------------------------------------------------


def test_login_refused_by_imap_tools_is_auth_failed_after_one_attempt():
    exc, box, sleeps = _fail([MailboxLoginError(("NO", [b"[AUTHENTICATIONFAILED] Invalid credentials"]), "OK")])
    assert isinstance(exc, MailAuthFailed) and str(exc) == "mail_auth_failed: acct"
    assert box.logins == 1  # exactly once: a retry must never be a password guess
    assert sleeps == []


def test_imap4_error_no_authenticationfailed_is_auth_failed_after_one_attempt():
    exc, box, sleeps = _fail([imaplib.IMAP4.error("NO [AUTHENTICATIONFAILED] Invalid credentials")])
    assert isinstance(exc, MailAuthFailed)
    assert box.logins == 1 and sleeps == []


def test_other_imap4_errors_are_not_retried_but_abort_is():
    # abort IS a subclass of IMAP4.error; the filter must tell them apart.
    assert issubclass(imaplib.IMAP4.abort, imaplib.IMAP4.error)
    exc, box, sleeps = _fail([imaplib.IMAP4.error("BAD command unknown")])
    assert isinstance(exc, MailAuthFailed) and box.logins == 1 and sleeps == []
    _, box2, sleeps2 = _run([imaplib.IMAP4.abort("socket error: EOF")])
    assert box2.logins == 2 and sleeps2 == [0.5]


def test_an_abort_that_is_an_authentication_refusal_is_not_retried():
    exc, box, _ = _fail([imaplib.IMAP4.abort("LOGIN failed: AUTHENTICATIONFAILED")])
    assert isinstance(exc, MailAuthFailed) and box.logins == 1


def test_unknown_errors_and_certificate_errors_are_raised_unchanged_without_retry():
    for error in (ValueError("bad config"), ssl.SSLCertVerificationError("hostname mismatch")):
        exc, box, sleeps = _fail([error])
        assert exc is error and box.logins == 1 and sleeps == []


def test_no_secret_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    for script in ([imaplib.IMAP4.abort(f"EOF {HOST}")] * 3, [imaplib.IMAP4.error(f"NO {SECRET} refused")]):
        exc, _, _ = _fail(list(script))
        assert exc.__cause__ is None and exc.__suppress_context__
        for text in (str(exc), repr(exc)):
            assert SECRET not in text and HOST not in text and USER not in text
    logged = caplog.text
    assert SECRET not in logged and HOST not in logged and USER not in logged
    assert "IMAP4" in logged or "abort" in logged or "error" in logged  # class names only


# --- both login paths use it, with a timeout -----------------------------------------


def _fake_imap_tools(monkeypatch, script):
    built: list = []

    class MailBox(_Box):
        def __init__(self, host, port=993, timeout=None):
            super().__init__(script)
            self.host, self.timeout = host, timeout
            built.append(self)

        def folder(self):  # pragma: no cover - never used
            raise AssertionError

    monkeypatch.setitem(sys.modules, "imap_tools", types.SimpleNamespace(MailBox=MailBox))
    monkeypatch.setattr(ml, "_sleep", lambda s: None)
    return built


def test_shared_mailbox_retries_and_sets_timeout(monkeypatch):
    built = _fake_imap_tools(monkeypatch, [imaplib.IMAP4.abort("EOF")])
    monkeypatch.setattr(t_email.config, "secret", lambda ref: SECRET)
    acl = Acl(users={}, projects={"jimlov": {"mailbox": {"host": HOST, "user": USER}}})
    t_email._make_mailbox(acl, "jimlov")
    assert len(built) == 2 and built[0].timeout == ml.CONNECT_TIMEOUT_SECONDS == 30


def test_shared_mailbox_auth_failure_is_clear_and_once(monkeypatch):
    built = _fake_imap_tools(monkeypatch, [imaplib.IMAP4.error("NO [AUTHENTICATIONFAILED]")])
    monkeypatch.setattr(t_email.config, "secret", lambda ref: SECRET)
    acl = Acl(users={}, projects={"jimlov": {"mailbox": {"host": HOST, "user": USER}}})
    with pytest.raises(MailAuthFailed, match="^mail_auth_failed: jimlov$"):
        t_email._make_mailbox(acl, "jimlov")
    assert len(built) == 1 and built[0].logins == 1


def test_per_user_mailbox_retries_and_names_the_account_only(monkeypatch):
    built = _fake_imap_tools(monkeypatch, [imaplib.IMAP4.abort("EOF")] * 3)
    token = {"host": HOST, "user": "alice@example.com", "password": SECRET}
    with pytest.raises(MailTemporarilyUnavailable) as info:
        t_imap_user.build_mailbox(token)
    assert str(info.value) == "mail_temporarily_unavailable: alice@example.com"
    assert HOST not in str(info.value) and SECRET not in str(info.value)
    assert len(built) == 3 and all(b.timeout == 30 for b in built)


# --- one dead mailbox does not take the others down --------------------------------------


def _wire(monkeypatch, shared, personal_by_host):
    """`shared` / personal values may be a mailbox or an exception to raise."""

    def maybe(value):
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(t_email, "_make_mailbox", lambda acl, project: maybe(shared))
    monkeypatch.setattr(t_imap_user, "build_mailbox", lambda token: maybe(personal_by_host[token["host"]]))
    monkeypatch.setattr(registry_mod, "_registry", None)


def _link(token_store, host="imap.a.example.com", account="alice@a.example.com"):
    token_store.store("alice", "imap", account, {"host": host, "user": "alice", "password": "pw"})


TEMP = MailTemporarilyUnavailable("proj")


def test_email_search_returns_the_healthy_source_and_a_warning_row(wired, monkeypatch):
    _, ts = wired
    _link(ts)
    _wire(monkeypatch, TEMP, {"imap.a.example.com": _FakeMailbox([_Msg("1", "From A")])})
    rows = server.email_search("proj", None)
    assert [r["subject"] for r in rows if "subject" in r] == ["From A"]
    warning = rows[-1]
    assert "Ofullständigt" in warning["warning"]
    assert warning["source_errors"] == [{"source": "imap:proj", "error": "mail_temporarily_unavailable: proj"}]


def test_email_list_same_and_auth_failure_stays_visible(wired, monkeypatch):
    _, ts = wired
    _link(ts)
    _wire(monkeypatch, MailAuthFailed("proj"), {"imap.a.example.com": _FakeMailbox([_Msg("1", "From A")])})
    rows = server.email_list("proj")
    assert rows[0]["subject"] == "From A"
    assert rows[-1]["source_errors"][0]["error"] == "mail_auth_failed: proj"


def test_a_failing_per_user_source_does_not_hide_the_shared_one(wired, monkeypatch):
    _, ts = wired
    _link(ts)
    _wire(monkeypatch, _FakeMailbox([_Msg("1", "Shared")]), {"imap.a.example.com": MailAuthFailed("alice@a.example.com")})
    rows = server.email_list("proj")
    assert rows[0]["subject"] == "Shared"
    assert rows[-1]["source_errors"][0]["source"] == "imap_user:alice@a.example.com"


def test_a_single_failing_source_raises_the_clear_error(wired, monkeypatch):
    _wire(monkeypatch, TEMP, {})
    with pytest.raises(MailTemporarilyUnavailable, match="^mail_temporarily_unavailable: proj$"):
        server.email_search("proj", "x")


def test_configuration_and_permission_errors_are_not_swallowed(wired, monkeypatch):
    _, ts = wired
    _link(ts)
    for error in (ValueError("no password configured"), PermissionError("denied")):
        _wire(monkeypatch, error, {"imap.a.example.com": _FakeMailbox([_Msg("1", "From A")])})
        with pytest.raises(type(error)):
            server.email_list("proj")


def test_email_read_for_the_unreachable_source_says_so_not_not_found(wired, monkeypatch):
    _, ts = wired
    _link(ts)
    healthy = _FakeMailbox([_Msg("7", "From A")])
    _wire(monkeypatch, TEMP, {"imap.a.example.com": healthy})
    with pytest.raises(MailTemporarilyUnavailable):
        server.email_read("proj", "imap:proj|7")
    assert server.email_read("proj", "imap_user:alice@a.example.com|7")["subject"] == "From A"
    with pytest.raises(FileNotFoundError):  # labelled for a healthy source: genuinely absent
        server.email_read("proj", "imap_user:alice@a.example.com|99")


def test_a_draft_is_not_silently_saved_elsewhere_when_a_source_is_down(wired, monkeypatch):
    _, ts = wired
    _link(ts)
    healthy = _FakeMailbox([])
    _wire(monkeypatch, TEMP, {"imap.a.example.com": healthy})
    with pytest.raises(MailTemporarilyUnavailable):
        server.email_create_draft("proj", "to@x.com", "S", "b")
    assert healthy.appended == []


# --- logout ---------------------------------------------------------------------------


class _Closing(_FakeMailbox):
    def __init__(self, msgs, logout_error=None, fetch_error=None):
        super().__init__(msgs)
        self.logouts = 0
        self.logout_error = logout_error
        self.fetch_error = fetch_error

    def fetch(self, *a, **kw):
        if self.fetch_error:
            raise self.fetch_error
        return super().fetch(*a, **kw)

    def logout(self):
        self.logouts += 1
        if self.logout_error:
            raise self.logout_error


@pytest.mark.parametrize("call", [
    lambda: server.email_list("proj"),
    lambda: server.email_search("proj", None),
    lambda: server.email_read("proj", "1"),
])
def test_logout_after_list_search_read(wired, monkeypatch, call):
    box = _Closing([_Msg("1", "S")])
    _wire(monkeypatch, box, {})
    call()
    assert box.logouts == 1


def test_logout_also_runs_on_error_and_a_logout_error_hides_neither_result_nor_error(wired, monkeypatch):
    boom = RuntimeError("fetch blew up")
    box = _Closing([], logout_error=OSError("socket dead"), fetch_error=boom)
    _wire(monkeypatch, box, {})
    with pytest.raises(RuntimeError, match="fetch blew up"):
        server.email_list("proj")
    assert box.logouts == 1
    ok = _Closing([_Msg("1", "S")], logout_error=OSError("socket dead"))
    _wire(monkeypatch, ok, {})
    assert server.email_list("proj")[0]["subject"] == "S"


def test_attachments_and_drafts_are_not_logged_out(wired, monkeypatch):
    box = _Closing([_Msg("1", "S")])
    _wire(monkeypatch, box, {})
    server.email_create_draft("proj", "to@x.com", "S", "b")
    assert box.logouts == 0
