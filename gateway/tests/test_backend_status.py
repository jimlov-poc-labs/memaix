# SPDX-License-Identifier: AGPL-3.0-or-later
"""backend_status.collect_status + cli.py's backend-status subcommand.

Covers: nothing declared, satisfied by a shared mailbox, satisfied by a
per-user scope (exact and '*'), '*' without visibility, needs_relink,
scoped to a different project, a malformed/unknown declaration, and the
fail-closed TOKEN_MASTER_KEY guard. Also asserts no password ever reaches
the output.
"""

from __future__ import annotations

import argparse
import io

import pytest
from cryptography.fernet import Fernet

from memaix_gateway.acl import Acl
from memaix_gateway.backend_status import collect_status
from memaix_gateway.backends.token_store import TokenStore


@pytest.fixture()
def store(tmp_path):
    return TokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key())


def _acl(projects, users=None):
    return Acl(users=users or {"jimmy": {"grants": {}}, "eve": {"grants": {}}}, projects=projects)


def test_project_without_awaiting_link_is_skipped(store):
    acl = _acl({"alfa": {}})
    assert collect_status(acl, store) == []


def test_satisfied_by_shared_mailbox(store):
    acl = _acl({
        "alfa": {
            "awaiting_link": ["mail"],
            "mailbox": {"host": "imap.alfa.se", "user": "x@alfa.se", "password_ref": "env:ALFA_PW"},
        }
    })
    entries = collect_status(acl, store)
    assert len(entries) == 1
    assert entries[0].status == "satisfied"
    assert "delad projektresurs" in entries[0].detail


def test_shared_mailbox_missing_required_fields_does_not_satisfy(store):
    acl = _acl({"alfa": {"awaiting_link": ["mail"], "mailbox": {"type": "imap"}}})
    entries = collect_status(acl, store)
    assert entries[0].status == "waiting"


def test_shared_mailbox_without_password_ref_does_not_satisfy(store):
    # host+user present (exactly what a wizard writing intent, no secret, would
    # produce) must not read as configured.
    acl = _acl({"alfa": {"awaiting_link": ["mail"], "mailbox": {"host": "imap.alfa.se", "user": "x@alfa.se"}}})
    entries = collect_status(acl, store)
    assert entries[0].status == "waiting"


def test_waiting_when_nothing_linked(store):
    acl = _acl({"alfa": {"awaiting_link": ["mail"]}})
    entries = collect_status(acl, store)
    assert entries == [collect_status(acl, store)[0]]
    assert entries[0].status == "waiting"
    assert entries[0].project == "alfa"
    assert entries[0].capability == "mail"


def test_satisfied_by_per_user_scope(store):
    acl = _acl({"alfa": {"awaiting_link": ["mail"]}}, users={"jimmy": {"grants": {"alfa": "owner"}}})
    store.store("jimmy", "imap", "me@box.se", {"host": "h", "user": "me@box.se", "password": "x"})
    store.set_scopes("jimmy", "imap", "me@box.se", "mail", ["alfa"])
    entries = collect_status(acl, store)
    assert entries[0].status == "satisfied"
    assert "kopplad för jimmy" in entries[0].detail
    assert "imap/me@box.se" in entries[0].detail


def test_satisfied_by_star_scope_when_project_visible(store):
    acl = _acl({"alfa": {"awaiting_link": ["mail"]}}, users={"jimmy": {"grants": {"alfa": "owner"}}})
    store.store("jimmy", "imap", "me@box.se", {"host": "h", "user": "me@box.se", "password": "x"})
    store.set_scopes("jimmy", "imap", "me@box.se", "mail", ["*"])
    entries = collect_status(acl, store)
    assert entries[0].status == "satisfied"


def test_star_scope_does_not_satisfy_when_project_not_visible(store):
    # jimmy has no grant on alfa at all, so '*' never covers it.
    acl = _acl({"alfa": {"awaiting_link": ["mail"]}}, users={"jimmy": {"grants": {}}})
    store.store("jimmy", "imap", "me@box.se", {"host": "h", "user": "me@box.se", "password": "x"})
    store.set_scopes("jimmy", "imap", "me@box.se", "mail", ["*"])
    entries = collect_status(acl, store)
    assert entries[0].status == "waiting"


def test_needs_relink_does_not_satisfy(store):
    acl = _acl({"alfa": {"awaiting_link": ["mail"]}}, users={"jimmy": {"grants": {"alfa": "owner"}}})
    store.store("jimmy", "imap", "me@box.se", {"host": "h", "user": "me@box.se", "password": "x"})
    store.set_scopes("jimmy", "imap", "me@box.se", "mail", ["alfa"])
    with store._lock, store._connect() as conn:  # noqa: SLF001 - only way to flip status for this test
        conn.execute("UPDATE user_tokens SET status='needs_relink' WHERE memaix_user='jimmy'")
        conn.commit()
    entries = collect_status(acl, store)
    assert entries[0].status == "waiting"


def test_scope_for_a_different_project_does_not_satisfy(store):
    acl = _acl(
        {"alfa": {"awaiting_link": ["mail"]}, "beta": {}},
        users={"jimmy": {"grants": {"alfa": "owner", "beta": "owner"}}},
    )
    store.store("jimmy", "imap", "me@box.se", {"host": "h", "user": "me@box.se", "password": "x"})
    store.set_scopes("jimmy", "imap", "me@box.se", "mail", ["beta"])
    entries = collect_status(acl, store)
    assert entries[0].status == "waiting"


def test_revoked_grant_with_stale_scope_row_does_not_satisfy(store):
    # jimmy was scoped to alfa while he had a grant; the grant was later
    # removed (acl.yaml edited / project_member_set(role=None)) but nobody
    # cleaned up the old scope row. enforce() would refuse this at request
    # time, so this view must not report "kopplad" from the stale row alone.
    acl = _acl({"alfa": {"awaiting_link": ["mail"]}}, users={"jimmy": {"grants": {}}})
    store.store("jimmy", "imap", "me@box.se", {"host": "h", "user": "me@box.se", "password": "x"})
    store.set_scopes("jimmy", "imap", "me@box.se", "mail", ["alfa"])
    entries = collect_status(acl, store)
    assert entries[0].status == "waiting"


def test_disabled_user_scope_does_not_satisfy(store):
    acl = _acl(
        {"alfa": {"awaiting_link": ["mail"]}},
        users={"jimmy": {"grants": {"alfa": "owner"}, "disabled": True}},
    )
    store.store("jimmy", "imap", "me@box.se", {"host": "h", "user": "me@box.se", "password": "x"})
    store.set_scopes("jimmy", "imap", "me@box.se", "mail", ["alfa"])
    entries = collect_status(acl, store)
    assert entries[0].status == "waiting"


def test_malformed_awaiting_link_is_reported_as_error_not_raised(store):
    acl = _acl({"alfa": {"awaiting_link": "mail"}})  # should be a list
    entries = collect_status(acl, store)
    assert entries[0].status == "error"
    assert "måste vara en lista" in entries[0].detail


def test_unknown_capability_is_reported_as_error(store):
    acl = _acl({"alfa": {"awaiting_link": ["telepathy"]}})
    entries = collect_status(acl, store)
    assert entries[0].status == "error"
    assert "okänd kapabilitet" in entries[0].detail


def test_no_password_in_any_entry_line(store):
    acl = _acl({"alfa": {"awaiting_link": ["mail"]}}, users={"jimmy": {"grants": {"alfa": "owner"}}})
    secret = "supersecret-pw-123"
    store.store("jimmy", "imap", "me@box.se", {"host": "h", "user": "me@box.se", "password": secret})
    store.set_scopes("jimmy", "imap", "me@box.se", "mail", ["alfa"])
    entries = collect_status(acl, store)
    for e in entries:
        assert secret not in e.line()
        assert secret not in e.detail


# ------------------------------------------------------------------
# cli.cmd_backend_status
# ------------------------------------------------------------------


def _args(**kw):
    base = dict(project=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_backend_status_master_key_required(monkeypatch):
    from memaix_gateway import cli

    monkeypatch.delenv("TOKEN_MASTER_KEY", raising=False)
    rc = cli.cmd_backend_status(_args())
    assert rc == 1


def test_cli_backend_status_prints_nothing_pending(store, capsys):
    from memaix_gateway import cli

    acl = _acl({"alfa": {}})
    rc = cli.cmd_backend_status(_args(), acl=acl, store=store)
    assert rc == 0
    assert "Inget väntar" in capsys.readouterr().out


def test_cli_backend_status_prints_entries_and_filters_by_project(store, capsys):
    from memaix_gateway import cli

    acl = _acl({"alfa": {"awaiting_link": ["mail"]}, "beta": {"awaiting_link": ["mail"]}})
    rc = cli.cmd_backend_status(_args(project="beta"), acl=acl, store=store)
    out = capsys.readouterr().out
    assert rc == 0
    assert "beta: mail" in out
    assert "alfa: mail" not in out


def test_cli_backend_status_unknown_project_is_an_error(store, capsys):
    from memaix_gateway import cli

    acl = _acl({"alfa": {"awaiting_link": ["mail"]}})
    rc = cli.cmd_backend_status(_args(project="nope"), acl=acl, store=store)
    assert rc == 1
    assert "Okänt projekt: nope" in capsys.readouterr().err


def test_cli_backend_status_exits_nonzero_on_malformed_entry(store, capsys):
    from memaix_gateway import cli

    acl = _acl({"alfa": {"awaiting_link": "mail"}})  # malformed: not a list
    rc = cli.cmd_backend_status(_args(), acl=acl, store=store)
    out = capsys.readouterr().out
    assert rc == 1
    assert "lista" in out


def test_cli_backend_status_loads_acl_and_store_from_server_when_not_given(monkeypatch, store):
    from memaix_gateway import cli, server

    monkeypatch.setenv("TOKEN_MASTER_KEY", Fernet.generate_key().decode())
    acl = _acl({"alfa": {"awaiting_link": ["mail"]}})
    monkeypatch.setattr(server, "_get_acl", lambda: acl)
    monkeypatch.setattr(server, "_get_token_store", lambda: store)
    assert cli.cmd_backend_status(_args()) == 0
