# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the per-user IMAP mail adapter (mail_imap_user.build_mailbox) —
FEATURE-CONNECTOR-FRAMEWORK.md, IMAP as a per-user provider."""

from __future__ import annotations

import pytest

from memaix_gateway.connectors.adapters.mail_imap_user import build_mailbox


class _FakeMailBox:
    """Stand-in for imap_tools.MailBox that records how it was built."""

    instances: list["_FakeMailBox"] = []

    def __init__(self, host, port=993, **kwargs):
        self.host = host
        self.port = port
        self.logged_in = None
        _FakeMailBox.instances.append(self)

    def login(self, user, password):
        self.logged_in = (user, password)
        return self


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeMailBox.instances.clear()
    yield
    _FakeMailBox.instances.clear()


@pytest.fixture()
def fake_imap_tools(monkeypatch):
    import sys
    import types

    fake_module = types.ModuleType("imap_tools")
    fake_module.MailBox = _FakeMailBox
    monkeypatch.setitem(sys.modules, "imap_tools", fake_module)
    return fake_module


def test_build_mailbox_logs_in_with_token_fields(fake_imap_tools):
    token = {"host": "imap.example.com", "user": "alice@example.com", "password": "s3cret"}
    mb = build_mailbox(token)
    assert mb.host == "imap.example.com"
    assert mb.logged_in == ("alice@example.com", "s3cret")
    assert mb.port == 993  # imap_tools default, not overridden


def test_build_mailbox_uses_custom_port_when_given(fake_imap_tools):
    token = {"host": "imap.example.com", "user": "a@x.com", "password": "pw", "port": 1993}
    mb = build_mailbox(token)
    assert mb.port == 1993


def test_build_mailbox_missing_host_raises(fake_imap_tools):
    with pytest.raises(ValueError, match="host"):
        build_mailbox({"user": "a@x.com", "password": "pw"})


def test_build_mailbox_missing_user_raises(fake_imap_tools):
    with pytest.raises(ValueError, match="host"):
        build_mailbox({"host": "imap.example.com", "password": "pw"})


def test_build_mailbox_missing_password_raises(fake_imap_tools):
    with pytest.raises(ValueError, match="host"):
        build_mailbox({"host": "imap.example.com", "user": "a@x.com"})


def test_build_mailbox_never_echoes_password_value_in_error(fake_imap_tools):
    try:
        build_mailbox({"host": "imap.example.com", "user": "a@x.com", "password": "s3cret-value"})
    except ValueError:
        pytest.fail("should not raise when all required fields are present")

    with pytest.raises(ValueError) as excinfo:
        build_mailbox({"host": "imap.example.com", "user": "a@x.com"})
    assert "s3cret-value" not in str(excinfo.value)
