# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for connectors.calendar_cache — memaix-src card 4daa20e2."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from memaix_gateway.acl import Acl
from memaix_gateway.connectors.calendar_cache import (
    DEFAULT_SYNC_INTERVAL,
    iter_calendar_targets,
    is_due,
    read_cache,
    sync_all_due,
    sync_user_calendar,
)
from memaix_gateway.connectors.registry import ConnectorRegistry, ConnectorSpec


def _dt(h: int = 0) -> datetime:
    return datetime(2026, 1, 5, h, tzinfo=timezone.utc)


class _FakeTokenStore:
    def list_accounts(self, user: str) -> list[dict]:
        return []

    def load_one(self, user: str, provider: str, account: str):
        return None

    def is_allowed(self, user, provider, account, capability, project):
        return True


class _FakeBackend:
    def __init__(self, events=None, raises=None):
        self._events = events or []
        self._raises = raises

    def list_events(self, start, end):
        if self._raises:
            raise self._raises
        return self._events


def _acl(tmp_path, calendar_cfg=None):
    projects = {"acme": {"vault": str(tmp_path)}}
    if calendar_cfg is not None:
        projects["acme"]["calendar"] = calendar_cfg
    return Acl(
        users={"alice": {"grants": {"acme": "owner"}}},
        projects=projects,
    )


def test_sync_user_calendar_writes_cache_and_returns_data(tmp_path):
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="caldav", capability="calendar", auth="shared",
            factory=lambda a, p, u, c, t: _FakeBackend(
                events=[{"start": "2026-01-05T09:00:00+00:00", "end": "2026-01-05T10:00:00+00:00"}]
            ),
        )
    )
    acl = _acl(tmp_path, {"type": "caldav", "url": "https://x"})
    data = sync_user_calendar(acl, _FakeTokenStore(), "acme", "alice", registry=registry, now=_dt())
    assert data["source_count"] == 1
    assert data["errors"] == []
    assert data["busy"] == [{"start": _dt(9).isoformat(), "end": _dt(10).isoformat(), "source": "caldav:acme"}]
    assert read_cache(acl, "acme", "alice") == data


def test_sync_user_calendar_records_per_source_errors(tmp_path):
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="caldav", capability="calendar", auth="shared",
            factory=lambda a, p, u, c, t: _FakeBackend(raises=RuntimeError("timeout")),
        )
    )
    acl = _acl(tmp_path, {"type": "caldav", "url": "https://x"})
    data = sync_user_calendar(acl, _FakeTokenStore(), "acme", "alice", registry=registry, now=_dt())
    assert data["busy"] == []
    assert data["errors"] == [{"source": "caldav:acme", "error": "timeout"}]


def test_read_cache_returns_none_when_never_synced(tmp_path):
    acl = _acl(tmp_path, {"type": "caldav", "url": "https://x"})
    assert read_cache(acl, "acme", "alice") is None


def test_is_due_true_when_cache_missing():
    assert is_due(None, _dt()) is True


def test_is_due_false_within_interval():
    cache = {"synced_at": _dt(0).isoformat()}
    assert is_due(cache, _dt(0) + timedelta(minutes=5), DEFAULT_SYNC_INTERVAL) is False


def test_is_due_true_once_interval_elapsed():
    cache = {"synced_at": _dt(0).isoformat()}
    assert is_due(cache, _dt(0) + timedelta(minutes=10), DEFAULT_SYNC_INTERVAL) is True


def test_iter_calendar_targets_only_includes_configured_and_granted():
    acl = Acl(
        users={
            "alice": {"grants": {"acme": "owner"}},
            "bob": {"grants": {}},
        },
        projects={
            "acme": {"vault": "/x", "calendar": {"type": "caldav"}},
            "other": {"vault": "/y"},
        },
    )
    assert iter_calendar_targets(acl) == [("acme", "alice")]


def test_sync_all_due_syncs_only_due_targets(tmp_path):
    registry = ConnectorRegistry()
    registry.register(
        ConnectorSpec(
            type="caldav", capability="calendar", auth="shared",
            factory=lambda a, p, u, c, t: _FakeBackend(events=[]),
        )
    )
    acl = _acl(tmp_path, {"type": "caldav", "url": "https://x"})

    synced_first = sync_all_due(acl, _FakeTokenStore(), registry=registry, now=_dt(0))
    assert synced_first == 1

    synced_again_soon = sync_all_due(acl, _FakeTokenStore(), registry=registry, now=_dt(0) + timedelta(minutes=1))
    assert synced_again_soon == 0

    synced_after_interval = sync_all_due(
        acl, _FakeTokenStore(), registry=registry, now=_dt(0) + DEFAULT_SYNC_INTERVAL
    )
    assert synced_after_interval == 1


def test_sync_all_due_skips_target_whose_sync_raises(tmp_path):
    class _BoomRegistry(ConnectorRegistry):
        def get_all(self, *a, **kw):
            raise RuntimeError("boom")

    acl = _acl(tmp_path, {"type": "caldav", "url": "https://x"})
    synced = sync_all_due(acl, _FakeTokenStore(), registry=_BoomRegistry(), now=_dt())
    assert synced == 0
