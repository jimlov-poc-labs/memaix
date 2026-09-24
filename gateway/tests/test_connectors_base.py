# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural (Protocol) conformance tests for the connector capability
interfaces — FEATURE-CONNECTOR-FRAMEWORK.md §4."""

from __future__ import annotations

from memaix_gateway.connectors.base import (
    CalendarBackend,
    ChatBackend,
    ContactsBackend,
    FilesBackend,
    IssueBackend,
    MailBackend,
    TasksBackend,
)


class _FakeMail:
    def fetch(self, criteria="ALL", *, mark_seen=False, limit=None):
        return []

    def append(self, message, folder="INBOX", dt=None, flag_set=None):
        pass

    def logout(self):
        pass


class _FakeCalendar:
    def list_events(self, start, end):
        return []

    def find_events(self, start, end):
        return []

    def create_event(self, uid, title, start, end, attendees=None, location=None, description=None):
        return {}

    def update_event(self, id, **fields):
        return {}

    def delete_event(self, id):
        pass


class _FakeFiles:
    def list_files(self, path):
        return []

    def read_file(self, path):
        return ""

    def write_file(self, path, content):
        return ""

    def search_files(self, query, path):
        return []


class _FakeContacts:
    def search(self, query):
        return []

    def get(self, id):
        return {}


class _FakeChat:
    def post(self, channel, text):
        return {}

    def list_messages(self, channel, since):
        return []


class _FakeIssues:
    def list(self, query):
        return []

    def create(self, item):
        return {}

    def update(self, id, **fields):
        return {}


class _FakeTasks:
    def list(self):
        return []

    def add(self, title, due=None, notes=None):
        return {}

    def complete(self, id):
        return {}


def test_fake_mail_satisfies_mail_backend():
    assert isinstance(_FakeMail(), MailBackend)


def test_fake_calendar_satisfies_calendar_backend():
    assert isinstance(_FakeCalendar(), CalendarBackend)


def test_fake_files_satisfies_files_backend():
    assert isinstance(_FakeFiles(), FilesBackend)


def test_fake_contacts_satisfies_contacts_backend():
    assert isinstance(_FakeContacts(), ContactsBackend)


def test_fake_chat_satisfies_chat_backend():
    assert isinstance(_FakeChat(), ChatBackend)


def test_fake_issues_satisfies_issue_backend():
    assert isinstance(_FakeIssues(), IssueBackend)


def test_fake_tasks_satisfies_tasks_backend():
    assert isinstance(_FakeTasks(), TasksBackend)


def test_incomplete_object_does_not_satisfy_mail_backend():
    class _Incomplete:
        def fetch(self, criteria="ALL", *, mark_seen=False, limit=None):
            return []

    assert not isinstance(_Incomplete(), MailBackend)
