# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for connectors.catalog — proves the registry builds today's real
mail/calendar adapters from project config (FEATURE-CONNECTOR-FRAMEWORK.md §6)."""

from __future__ import annotations

import pytest

from memaix_gateway.acl import Acl
from memaix_gateway.connectors.catalog import register_defaults
from memaix_gateway.connectors.registry import ConnectorRegistry


class _FakeTokenStore:
    def list_accounts(self, user):
        return []

    def load_one(self, user, provider, account):
        return None


@pytest.fixture()
def acl():
    return Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={
            "acme": {
                "vault": "/srv/vaults/acme",
                "mailbox": {"host": "imap.example.com", "user": "acme@example.com", "password_ref": "env:PW"},
                "calendar": {"url": "https://caldav.example.com/acme/", "user": "acme@example.com"},
                "contacts": {
                    "url": "https://nc.example.com/dav/contacts/",
                    "user": "acme@example.com",
                    "password_ref": "env:NC_PW",
                },
                "files": {
                    "url": "https://nc.example.com/dav/files/acme/",
                    "user": "acme@example.com",
                    "password_ref": "env:NC_FILES_PW",
                },
                "tasks": {
                    "url": "https://nc.example.com/dav/tasks/acme/",
                    "user": "acme@example.com",
                    "password_ref": "env:NC_TASKS_PW",
                },
                "deck": {
                    "url": "https://nc.example.com",
                    "user": "acme@example.com",
                    "password_ref": "env:NC_DECK_PW",
                    "board_id": 1,
                    "stack_id": 2,
                },
                "notes": {
                    "url": "https://nc.example.com",
                    "user": "acme@example.com",
                    "password_ref": "env:NC_NOTES_PW",
                },
            }
        },
    )


@pytest.fixture()
def registry():
    r = ConnectorRegistry()
    register_defaults(r)
    return r


def test_catalog_registers_imap_for_mail(registry, acl, monkeypatch):
    sentinel = object()
    import memaix_gateway.tools.email as t_email

    monkeypatch.setattr(t_email, "_make_mailbox", lambda acl, project: sentinel)
    result = registry.get(acl, _FakeTokenStore(), "acme", "mail", "alice")
    assert result is sentinel


def test_catalog_registers_caldav_for_calendar(registry, acl, monkeypatch):
    sentinel = object()
    import memaix_gateway.tools.calendar as t_cal

    monkeypatch.setattr(t_cal, "_RealDavAdapter", lambda acl, project: sentinel)
    result = registry.get(acl, _FakeTokenStore(), "acme", "calendar", "alice")
    assert result is sentinel


def test_catalog_registers_carddav_for_contacts(registry, acl, monkeypatch):
    monkeypatch.setenv("NC_PW", "s3cret")
    seen = {}

    class _Sentinel:
        def __init__(self, url, user, password):
            seen["url"] = url
            seen["user"] = user
            seen["password"] = password

    import memaix_gateway.connectors.adapters.contacts_carddav as t_contacts

    monkeypatch.setattr(t_contacts, "CardDavContactsAdapter", _Sentinel)
    result = registry.get(acl, _FakeTokenStore(), "acme", "contacts", "alice")
    assert isinstance(result, _Sentinel)
    assert seen == {
        "url": "https://nc.example.com/dav/contacts/",
        "user": "acme@example.com",
        "password": "s3cret",
    }


def test_catalog_registers_webdav_for_files(registry, acl, monkeypatch):
    monkeypatch.setenv("NC_FILES_PW", "f1les")
    seen = {}

    class _Sentinel:
        def __init__(self, url, user, password):
            seen["url"] = url
            seen["user"] = user
            seen["password"] = password

    import memaix_gateway.connectors.adapters.files_webdav as t_files

    monkeypatch.setattr(t_files, "WebDavFilesAdapter", _Sentinel)
    result = registry.get(acl, _FakeTokenStore(), "acme", "files", "alice")
    assert isinstance(result, _Sentinel)
    assert seen == {
        "url": "https://nc.example.com/dav/files/acme/",
        "user": "acme@example.com",
        "password": "f1les",
    }


def test_catalog_registers_caldav_for_tasks(registry, acl, monkeypatch):
    monkeypatch.setenv("NC_TASKS_PW", "t4sks")
    seen = {}

    class _Sentinel:
        def __init__(self, url, user, password):
            seen["url"] = url
            seen["user"] = user
            seen["password"] = password

    import memaix_gateway.connectors.adapters.tasks_caldav as t_tasks

    monkeypatch.setattr(t_tasks, "CalDavTasksAdapter", _Sentinel)
    result = registry.get(acl, _FakeTokenStore(), "acme", "tasks", "alice")
    assert isinstance(result, _Sentinel)
    assert seen == {
        "url": "https://nc.example.com/dav/tasks/acme/",
        "user": "acme@example.com",
        "password": "t4sks",
    }


def test_tasks_and_calendar_are_independent_caldav_resources(registry, acl, monkeypatch):
    """Both default to type 'caldav' but must not collide in the registry —
    they're different capabilities with different resource configs."""
    monkeypatch.setenv("NC_TASKS_PW", "t4sks")
    import memaix_gateway.tools.calendar as t_cal
    import memaix_gateway.connectors.adapters.tasks_caldav as t_tasks

    monkeypatch.setattr(t_cal, "_RealDavAdapter", lambda acl, project: "calendar-adapter")
    monkeypatch.setattr(t_tasks, "CalDavTasksAdapter", lambda url, user, password: "tasks-adapter")
    assert registry.get(acl, _FakeTokenStore(), "acme", "calendar", "alice") == "calendar-adapter"
    assert registry.get(acl, _FakeTokenStore(), "acme", "tasks", "alice") == "tasks-adapter"


def test_catalog_registers_nextcloud_for_deck(registry, acl, monkeypatch):
    monkeypatch.setenv("NC_DECK_PW", "d3ck")
    seen = {}

    class _Sentinel:
        def __init__(self, url, user, password):
            seen["url"] = url
            seen["user"] = user
            seen["password"] = password

    import memaix_gateway.connectors.adapters.deck_nextcloud as t_deck

    monkeypatch.setattr(t_deck, "DeckAdapter", _Sentinel)
    result = registry.get(acl, _FakeTokenStore(), "acme", "deck", "alice")
    assert isinstance(result, _Sentinel)
    assert seen == {"url": "https://nc.example.com", "user": "acme@example.com", "password": "d3ck"}


def test_catalog_registers_nextcloud_for_notes(registry, acl, monkeypatch):
    monkeypatch.setenv("NC_NOTES_PW", "n0tes")
    seen = {}

    class _Sentinel:
        def __init__(self, url, user, password):
            seen["url"] = url
            seen["user"] = user
            seen["password"] = password

    import memaix_gateway.connectors.adapters.notes_nextcloud as t_notes

    monkeypatch.setattr(t_notes, "NotesAdapter", _Sentinel)
    result = registry.get(acl, _FakeTokenStore(), "acme", "notes", "alice")
    assert isinstance(result, _Sentinel)
    assert seen == {"url": "https://nc.example.com", "user": "acme@example.com", "password": "n0tes"}


def test_deck_and_notes_are_independent_nextcloud_resources(registry, acl, monkeypatch):
    """Both default to type 'nextcloud' but must not collide in the registry."""
    monkeypatch.setenv("NC_DECK_PW", "d3ck")
    monkeypatch.setenv("NC_NOTES_PW", "n0tes")
    import memaix_gateway.connectors.adapters.deck_nextcloud as t_deck
    import memaix_gateway.connectors.adapters.notes_nextcloud as t_notes

    monkeypatch.setattr(t_deck, "DeckAdapter", lambda url, user, password: "deck-adapter")
    monkeypatch.setattr(t_notes, "NotesAdapter", lambda url, user, password: "notes-adapter")
    assert registry.get(acl, _FakeTokenStore(), "acme", "deck", "alice") == "deck-adapter"
    assert registry.get(acl, _FakeTokenStore(), "acme", "notes", "alice") == "notes-adapter"


def test_files_capability_does_not_read_vault_key(registry):
    """'files' must resolve against the new 'files' resource key, never 'vault'
    — the local vault has a completely different (bare string) shape."""
    acl = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/srv/vaults/acme"}},  # no 'files' resource configured
    )
    with pytest.raises(ValueError, match="no files configured"):
        registry.get(acl, _FakeTokenStore(), "acme", "files", "alice")


def test_default_registry_is_a_lazy_singleton():
    from memaix_gateway.connectors.registry import default_registry

    first = default_registry()
    second = default_registry()
    assert first is second


def test_catalog_registers_imap_user_for_mail(registry, monkeypatch):
    """Per-user IMAP (type='imap_user', provider='imap') coexists with the
    shared type='imap' spec — get_all's per_user sweep can pick it up even
    though acl.yaml's mailbox resource still says type='imap' (shared)."""
    sentinel = object()
    import memaix_gateway.connectors.adapters.mail_imap_user as t_imap_user

    monkeypatch.setattr(t_imap_user, "build_mailbox", lambda token: sentinel)

    acl_no_mail = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/srv/vaults/acme"}},
    )

    class _ImapUserStore:
        def list_accounts(self, user):
            return [{"provider": "imap", "account": "alice@personal.example.com"}]

        def load_one(self, user, provider, account):
            return {"host": "imap.personal.example.com", "user": "alice", "password": "pw"}

    result = registry.get_all(acl_no_mail, _ImapUserStore(), "acme", "mail", "alice")
    assert any(adapter is sentinel for _, adapter in result)


def test_shared_imap_spec_and_imap_user_spec_are_distinct_types(registry):
    """The two mail/imap* specs must not collide in the registry's (capability, type) key."""
    imap_spec = registry.get_spec("mail", "imap")
    imap_user_spec = registry.get_spec("mail", "imap_user")
    assert imap_spec is not None and imap_user_spec is not None
    assert imap_spec.auth == "shared"
    assert imap_user_spec.auth == "per_user"
    assert imap_user_spec.provider == "imap"


def test_catalog_registers_google_calendar_spec(registry, monkeypatch):
    """Google per_user spec is registered — get_all sweep can pick it up."""
    sentinel = object()
    import memaix_gateway.tools.calendar as t_cal

    monkeypatch.setattr(t_cal, "_PerUserGoogleAdapter", lambda token: sentinel)

    acl_no_cal = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/srv/vaults/acme"}},
    )

    class _GoogleStore:
        def list_accounts(self, user):
            return [{"provider": "google", "account": "a@gmail.com"}]

        def load_one(self, user, provider, account):
            return {"access_token": "tok"}

    result = registry.get_all(acl_no_cal, _GoogleStore(), "acme", "calendar", "alice")
    assert any(adapter is sentinel for _, adapter in result)


def test_catalog_registers_ical_secret_calendar_spec(registry, monkeypatch):
    """iCal-secret per_user spec is registered — get_all sweep can pick it up."""
    sentinel = object()
    import memaix_gateway.tools.calendar as t_cal

    monkeypatch.setattr(t_cal, "_ICalAdapter", lambda url: sentinel)

    acl_no_cal = Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects={"acme": {"vault": "/srv/vaults/acme"}},
    )

    class _ICalStore:
        def list_accounts(self, user):
            return [{"provider": "ical_secret", "account": "ical_secret"}]

        def load_one(self, user, provider, account):
            return {"ical_url": "https://cal.example/secret.ics"}

    result = registry.get_all(acl_no_cal, _ICalStore(), "acme", "calendar", "alice")
    assert any(adapter is sentinel for _, adapter in result)
