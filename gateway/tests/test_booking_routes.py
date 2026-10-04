# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the public /book/{slug} routes — memaix-src card 2bef1062."""

from __future__ import annotations

import functools
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from memaix_gateway import config
from memaix_gateway.acl import Acl
from memaix_gateway.connectors.booking_settings import BookingSettingsStore
from memaix_gateway.tools import email as email_mod


def _dt(h: int, m: int = 0) -> datetime:
    return datetime(2026, 1, 3, h, m, tzinfo=timezone.utc)


class _MockDav:
    def __init__(self, events=None):
        self._events = list(events or [])

    def list_events(self, start, end):
        # Overlap, not containment — matches every real adapter (Google,
        # iCal, CalDAV all return events that overlap the query window, not
        # just ones fully inside it).
        return [e for e in self._events if _parse(e["start"]) < end and _parse(e["end"]) > start]

    find_events = list_events

    def create_event(self, uid, title, start, end, attendees=None, location=None, description=None, want_conference=False):
        ev = {
            "id": uid, "title": title, "start": start.isoformat(), "end": end.isoformat(),
            "description": description, "location": location,
        }
        self._events.append(ev)
        return ev

    def update_event(self, id, **fields):
        ev = next((e for e in self._events if e["id"] == id), None)
        if ev is None:
            raise FileNotFoundError(f"event not found: {id!r}")
        if "start" in fields:
            ev["start"] = fields["start"]
        if "end" in fields:
            ev["end"] = fields["end"]
        if "title" in fields:
            ev["title"] = fields["title"]
        return ev

    def delete_event(self, id):
        ev = next((e for e in self._events if e["id"] == id), None)
        if ev is None:
            raise FileNotFoundError(f"event not found: {id!r}")
        self._events.remove(ev)


def _parse(s):
    return datetime.fromisoformat(s) if isinstance(s, str) else s


class _MockSmtp:
    def __init__(self) -> None:
        self.sent: list = []

    def send_message(self, msg) -> None:
        self.sent.append(msg)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    from memaix_gateway import server as server_mod

    # The limiter is a process-wide singleton; without this, the number of
    # requests one test makes silently becomes the next test's budget.
    from memaix_gateway.safety.rate_limit import rate_limiter
    rate_limiter._reset()

    vault = tmp_path / "vault"
    smtp = _MockSmtp()
    acl = Acl(
        users={"alice": {"grants": {"proj": "owner"}}},
        projects={"proj": {
            "vault": str(vault),
            "calendar": {"type": "caldav"},
            "mailbox": {"host": "imap.example.com", "user": "alice@example.com", "password_ref": "env:FAKE_MAILBOX_PW"},
            "allow_send": True,
        }},
    )
    monkeypatch.setenv("FAKE_MAILBOX_PW", "shh")
    import memaix_gateway.booking.routes as booking_routes_mod
    monkeypatch.setattr(
        booking_routes_mod.t_email, "email_send",
        functools.partial(email_mod.email_send, _smtp=smtp),
    )
    monkeypatch.setattr(server_mod, "_acl", acl)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    links_dir = tmp_path / "booking_links"
    links_dir.mkdir(parents=True)
    (links_dir / "alice-30.json").write_text(json.dumps({
        "project": "proj", "user": "alice", "duration_min": 30, "title_template": "Möte med {name}",
        "host_email": "alice@example.com", "host_timezone": "Europe/Stockholm",
    }))

    BookingSettingsStore(acl, "proj", "alice").set(True)

    dav = _MockDav()
    monkeypatch.setattr(server_mod, "_resolve_calendar_dav", lambda project, user, write=False: dav)
    monkeypatch.setattr(booking_routes_mod, "_resolve_dav_filtered", lambda project, user, acl: dav)

    monkeypatch.setenv("TEST_TURNSTILE_SECRET", "shh")
    monkeypatch.setattr(config, "load", lambda: {"memaix": {"booking": {"turnstile_secret_ref": "env:TEST_TURNSTILE_SECRET"}}})

    class _FakeTurnstileResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"success": True}

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return _FakeTurnstileResponse()

    import memaix_gateway.booking.routes as booking_routes_mod
    monkeypatch.setattr(booking_routes_mod.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient())

    app = server_mod.build_http_app()
    client = TestClient(app)
    client.smtp = smtp
    return client, dav


def test_unknown_slug_is_404(rig):
    client, _dav = rig
    resp = client.get("/book/no-such-slug/slots?within_start=2026-01-03T08:00:00+00:00&within_end=2026-01-03T18:00:00+00:00")
    assert resp.status_code == 404


def test_slots_returns_only_start_end(rig):
    client, dav = rig
    dav.create_event("ev1", "Secret", _dt(9), _dt(9, 30))
    resp = client.get(
        "/book/alice-30/slots",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "slots" in body
    for slot in body["slots"]:
        assert set(slot.keys()) == {"start", "end"}


def test_slots_404_when_booking_disabled(rig, monkeypatch):
    client, _dav = rig
    from memaix_gateway import server as server_mod
    BookingSettingsStore(server_mod._acl, "proj", "alice").set(False)
    resp = client.get(
        "/book/alice-30/slots",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()},
    )
    assert resp.status_code == 404


def _local_hhmm(times):
    return [t["start"][11:16] for t in times]


def test_times_are_offered_on_the_hosts_clock(rig):
    """The bug /times exists to fix: a meeting overrunning to 10:15 local
    must not turn the rest of the day into 10:15/10:45/11:15 offers."""
    client, dav = rig
    dav.create_event("ev1", "Överdraget", _dt(8), _dt(9, 15))  # 09:00-10:15 lokal tid
    resp = client.get(
        "/book/alice-30/times",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(12).isoformat()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["duration_min"] == 30
    assert body["granularity_min"] == 30
    assert _local_hhmm(body["times"])[:3] == ["10:30", "11:00", "11:30"]


def test_times_are_expressed_in_the_hosts_timezone(rig):
    """A visitor's browser can render whatever it likes, but the offer the
    server makes is the host's local time — the link says Europe/Stockholm."""
    client, _dav = rig
    resp = client.get(
        "/book/alice-30/times",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(10).isoformat()},
    )
    assert all(t["start"].endswith("+01:00") for t in resp.json()["times"])


def test_times_never_offer_a_busy_moment(rig):
    client, dav = rig
    dav.create_event("ev1", "Upptagen", _dt(9), _dt(10))  # 10:00-11:00 lokal tid
    resp = client.get(
        "/book/alice-30/times",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(12).isoformat()},
    )
    offered = _local_hhmm(resp.json()["times"])
    assert "10:00" not in offered and "10:30" not in offered
    assert "09:00" in offered and "11:00" in offered


def test_times_returns_only_start_end(rig):
    """Same disclosure rule as /slots (card de858332) — no titles, no
    attendees, no hint of which calendar the gap came from."""
    client, dav = rig
    dav.create_event("ev1", "Secret", _dt(9), _dt(9, 30))
    resp = client.get(
        "/book/alice-30/times",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()},
    )
    assert resp.json()["times"]
    for t in resp.json()["times"]:
        assert set(t.keys()) == {"start", "end"}


def test_times_honours_a_finer_grid(rig):
    client, _dav = rig
    resp = client.get(
        "/book/alice-30/times",
        params={
            "within_start": _dt(8).isoformat(), "within_end": _dt(10).isoformat(),
            "granularity_min": "15",
        },
    )
    body = resp.json()
    assert body["granularity_min"] == 15
    assert _local_hhmm(body["times"])[:3] == ["09:00", "09:15", "09:30"]


def test_times_ignores_a_nonsense_grid_rather_than_refusing(rig):
    client, _dav = rig
    resp = client.get(
        "/book/alice-30/times",
        params={
            "within_start": _dt(8).isoformat(), "within_end": _dt(10).isoformat(),
            "granularity_min": "abc",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["granularity_min"] == 30


def test_times_404_when_booking_disabled(rig):
    client, _dav = rig
    from memaix_gateway import server as server_mod
    BookingSettingsStore(server_mod._acl, "proj", "alice").set(False)
    resp = client.get(
        "/book/alice-30/times",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()},
    )
    assert resp.status_code == 404


def test_times_404_for_unknown_slug(rig):
    client, _dav = rig
    resp = client.get(
        "/book/no-such-slug/times",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()},
    )
    assert resp.status_code == 404


def test_times_rejects_a_backwards_window(rig):
    client, _dav = rig
    resp = client.get(
        "/book/alice-30/times",
        params={"within_start": _dt(18).isoformat(), "within_end": _dt(8).isoformat()},
    )
    assert resp.status_code == 400


def test_slots_still_returns_raw_windows(rig):
    """Both live sites still divide the windows themselves. /times must be
    an addition, not a redefinition — the day /slots starts returning
    half-hour pieces is the day both sites show a grid of duplicates."""
    client, dav = rig
    dav.create_event("ev1", "Upptagen", _dt(9), _dt(10))
    windows = client.get(
        "/book/alice-30/slots",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(12).isoformat()},
    ).json()["slots"]
    assert len(windows) == 2
    assert windows[0]["end"] == _dt(9).isoformat()


# ---------------------------------------------------------------------
# /config and the embedded widget
# ---------------------------------------------------------------------


def test_config_tells_the_widget_what_it_needs(rig):
    """The widget is served to origins we don't control, so it can't carry
    per-host settings in its own source. Everything it needs to draw itself
    before it knows a single free time comes from here."""
    client, _dav = rig
    body = client.get("/book/alice-30/config").json()
    assert body["duration_min"] == 30
    assert body["granularity_min"] == 30
    assert body["timezone"] == "Europe/Stockholm"
    assert body["meeting_forms"] == []


def test_config_never_publishes_a_meeting_forms_private_config(rig):
    """A phone form keeps the host's number in its config. The visitor is
    about to be offered the *choice*; publishing the number to anyone who
    curls the endpoint is a different thing entirely."""
    client, _dav = rig
    from memaix_gateway import server as server_mod
    from memaix_gateway.tools import calendar as t_cal
    t_cal.calendar_meeting_form_set(server_mod._acl, "alice", "proj", [
        {"slug": "ring", "provider": "phone", "label": "Telefon",
         "config": {"phone_number": "+46701234567"}},
    ])
    resp = client.get("/book/alice-30/config")
    assert "+46701234567" not in resp.text
    assert resp.json()["meeting_forms"] == [
        {"slug": "ring", "label": "Telefon", "provider": "phone", "default": True}
    ]


def test_config_404_for_unknown_slug(rig):
    client, _dav = rig
    assert client.get("/book/no-such-slug/config").status_code == 404


def test_config_404_when_booking_disabled(rig):
    """Same answer /times and /slots give. A link whose host turned booking
    off must not still hand out a captcha key and a timezone."""
    client, _dav = rig
    from memaix_gateway import server as server_mod
    BookingSettingsStore(server_mod._acl, "proj", "alice").set(False)
    assert client.get("/book/alice-30/config").status_code == 404


def test_config_prefers_the_links_own_turnstile_key(rig, monkeypatch, tmp_path):
    """One gateway can front several booking pages, and Turnstile keys are
    per-domain — so the link wins over the gateway-wide default."""
    client, _dav = rig
    monkeypatch.setattr(config, "load", lambda: {
        "memaix": {"booking": {"turnstile_site_key": "gateway-wide"}}
    })
    assert client.get("/book/alice-30/config").json()["turnstile_site_key"] == "gateway-wide"

    (tmp_path / "booking_links" / "alice-own-key.json").write_text(json.dumps({
        "project": "proj", "user": "alice", "duration_min": 30,
        "turnstile_site_key": "links-own",
    }))
    assert client.get("/book/alice-own-key/config").json()["turnstile_site_key"] == "links-own"


def test_the_widget_is_served_as_javascript(rig):
    client, _dav = rig
    resp = client.get("/embed/booking.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    # The mount attribute is the whole public contract of the embed. If it
    # ever gets renamed, every page that already ships a script tag goes
    # quietly blank — this is the test that makes that a failing build.
    assert "data-memaix-booking" in resp.text


def test_a_missing_widget_file_is_a_404_not_a_crash(rig, monkeypatch, tmp_path):
    """package-data is a whitelist, and this file is one line in it.

    Drop that line and booking.js is simply absent from the installed wheel —
    a packaging mistake nobody notices until production. FileResponse answers
    a missing path with RuntimeError, i.e. a 500 that reads like the gateway
    fell over. 404 says the true thing: this gateway has no widget.
    """
    from memaix_gateway.booking import routes as booking_routes

    monkeypatch.setattr(booking_routes, "_WIDGET_JS", tmp_path / "gone.js")
    client, _dav = rig
    assert client.get("/embed/booking.js").status_code == 404


def test_a_link_may_allow_its_own_embedding_origin(rig, tmp_path):
    """The widget's point is that anyone can embed it. Before per-link
    origins, "anyone" meant the two sites hardcoded in routes.py."""
    client, _dav = rig
    (tmp_path / "booking_links" / "alice-guest.json").write_text(json.dumps({
        "project": "proj", "user": "alice", "duration_min": 30,
        "origins": ["https://kund.example"],
    }))
    resp = client.get("/book/alice-guest/config", headers={"Origin": "https://kund.example"})
    assert resp.headers.get("access-control-allow-origin") == "https://kund.example"


def test_an_unlisted_origin_gets_no_cors_headers(rig):
    client, _dav = rig
    resp = client.get("/book/alice-30/config", headers={"Origin": "https://elsewhere.example"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_the_two_original_sites_keep_their_access(rig):
    """Hardcoded rather than configured on purpose — a typo in a link file
    must not be able to take booking off the air on the live sites."""
    client, _dav = rig
    resp = client.get("/book/alice-30/config", headers={"Origin": "https://memaix.se"})
    assert resp.headers.get("access-control-allow-origin") == "https://memaix.se"


def test_browsing_the_calendar_does_not_spend_the_budget_for_booking(rig):
    """Reading and writing get separate counters. They used to share one, so
    a visitor who clicked through a dozen weeks looking for a time was then
    refused when they finally picked one."""
    client, _dav = rig
    params = {"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()}
    for _ in range(15):
        assert client.get("/book/alice-30/times", params=params).status_code == 200
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(10).isoformat(), "end": _dt(10, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True, "consent_text": "Jag samtycker till lagring i 1 år.",
        },
    )
    assert resp.status_code == 200


def test_create_booking_succeeds_and_stores_event(rig):
    client, dav = rig
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(10).isoformat(), "end": _dt(10, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True, "consent_text": "Jag samtycker till lagring i 1 år.",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert any(e["title"] == "Möte med Bob" for e in dav._events)


def test_create_booking_uses_writable_adapter_not_readonly_multi_adapter(rig, monkeypatch):
    """Regression for '_MultiCalendarAdapter' object has no attribute
    'create_event': _resolve_dav_filtered may legitimately return a
    read-only, merge-only adapter (what _MultiCalendarAdapter actually is)
    when more than one calendar source is enabled — fine for the
    availability check, but must never be the adapter calendar_create
    writes through. This pins _resolve_dav_filtered and
    _resolve_dav/_resolve_calendar_dav to two different fakes, so the test
    only passes if booking_create resolves its write adapter independently
    instead of reusing whatever _resolve_dav_filtered returned.
    """
    import memaix_gateway.booking.routes as booking_routes_mod

    client, write_dav = rig

    class _ReadOnlyMultiDav:
        """Mirrors _MultiCalendarAdapter's actual shape: list_events/
        find_events only, no create_event/update_event/delete_event."""

        def __init__(self, events):
            self._events = events

        def list_events(self, start, end):
            return [e for e in self._events if _parse(e["start"]) < end and _parse(e["end"]) > start]

        find_events = list_events

    read_dav = _ReadOnlyMultiDav(write_dav._events)
    monkeypatch.setattr(booking_routes_mod, "_resolve_dav_filtered", lambda project, user, acl: read_dav)
    # _resolve_dav (write path) still resolves through server's
    # _resolve_calendar_dav, already patched by `rig` to return the
    # writable `write_dav` mock — left untouched here on purpose.

    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(11).isoformat(), "end": _dt(11, 30).isoformat(),
            "name": "Carol", "email": "carol@example.com", "turnstile_token": "tok",
            "consent": True, "consent_text": "Jag samtycker till lagring i 1 år.",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert any(e["title"] == "Möte med Carol" for e in write_dav._events)


def test_create_booking_unhandled_error_still_carries_cors_header(rig, monkeypatch):
    """The secondary CORS bug: an unhandled exception in the route used to
    bypass _json() entirely and fall into Starlette's default error
    middleware, which drops the CORS header — the browser then reports
    net::ERR_FAILED with no usable status instead of a real 500."""
    import memaix_gateway.booking.routes as booking_routes_mod

    client, _dav = rig

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(booking_routes_mod.t_cal, "calendar_create", _boom)

    resp = client.post(
        "/book/alice-30",
        headers={"Origin": "https://memaix.se"},
        json={
            "start": _dt(12).isoformat(), "end": _dt(12, 30).isoformat(),
            "name": "Dave", "email": "dave@example.com", "turnstile_token": "tok",
            "consent": True, "consent_text": "Jag samtycker till lagring i 1 år.",
        },
    )
    assert resp.status_code == 500
    assert resp.headers.get("access-control-allow-origin") == "https://memaix.se"
    assert resp.json()["error"] == "internal_error"


def test_create_booking_without_consent_is_rejected(rig):
    client, dav = rig
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(19).isoformat(), "end": _dt(19, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "consent_required"
    assert not any(e["start"] == _dt(19).isoformat() for e in dav._events)


def test_create_booking_records_consent(rig, monkeypatch, tmp_path):
    from memaix_gateway.booking.consent_store import ConsentStore
    import memaix_gateway.booking.routes as booking_routes_mod

    store = ConsentStore(tmp_path / "consent.db")
    monkeypatch.setattr(booking_routes_mod, "get_consent_store", lambda: store)

    client, dav = rig
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(20).isoformat(), "end": _dt(20, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True, "consent_text": "Jag samtycker.",
        },
    )
    assert resp.status_code == 200
    rows = store.due(int(_dt(20, 30).timestamp()) + 366 * 86400)
    assert len(rows) == 1
    assert rows[0]["project"] == "proj"
    assert rows[0]["host_user"] == "alice"
    assert rows[0]["visitor_email"] == "bob@example.com"


def test_create_booking_passes_purpose_as_description(rig):
    client, dav = rig
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(13).isoformat(), "end": _dt(13, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "purpose": "Prata om samarbete kring X.", "consent": True,
        },
    )
    assert resp.status_code == 200
    ev = next(e for e in dav._events if e["start"] == _dt(13).isoformat())
    assert ev["description"] == "Prata om samarbete kring X."


def test_create_booking_without_purpose_stores_no_description(rig):
    client, dav = rig
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(14).isoformat(), "end": _dt(14, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True,
        },
    )
    assert resp.status_code == 200
    ev = next(e for e in dav._events if e["start"] == _dt(14).isoformat())
    assert ev["description"] is None


def test_create_booking_truncates_overlong_purpose(rig):
    client, dav = rig
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(15).isoformat(), "end": _dt(15, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "purpose": "x" * 900, "consent": True,
        },
    )
    assert resp.status_code == 200
    ev = next(e for e in dav._events if e["start"] == _dt(15).isoformat())
    assert len(ev["description"]) == 500


def test_create_booking_sends_confirmation_to_visitor_and_host(rig):
    client, dav = rig
    from memaix_gateway.safety.rate_limit import rate_limiter
    rate_limiter._windows.pop("booking:testclient", None)
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(9).isoformat(), "end": _dt(9, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "purpose": "Prata om X.", "timezone": "Europe/Stockholm", "consent": True,
        },
    )
    assert resp.status_code == 200
    assert len(client.smtp.sent) == 2
    recipients = {msg["To"] for msg in client.smtp.sent}
    assert recipients == {"bob@example.com", "alice@example.com"}
    for msg in client.smtp.sent:
        attachments = list(msg.iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "moete.ics"
        ics_content = attachments[0].get_content()
        ics_text = ics_content.decode("utf-8") if isinstance(ics_content, bytes) else ics_content
        assert "BEGIN:VEVENT" in ics_text
        assert "Prata om X." in msg.get_body(preferencelist=("plain",)).get_content()


def test_create_booking_without_host_email_only_emails_visitor(rig, tmp_path):
    client, dav = rig
    from memaix_gateway import config
    from memaix_gateway.safety.rate_limit import rate_limiter
    rate_limiter._windows.pop("booking:testclient", None)
    (config.CONFIG_DIR / "booking_links" / "no-host-email.json").write_text(json.dumps({
        "project": "proj", "user": "alice", "duration_min": 30, "title_template": "Möte",
    }))
    resp = client.post(
        "/book/no-host-email",
        json={
            "start": _dt(17).isoformat(), "end": _dt(17, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True,
        },
    )
    assert resp.status_code == 200
    assert len(client.smtp.sent) == 1
    assert client.smtp.sent[0]["To"] == "bob@example.com"


def test_create_booking_succeeds_even_if_confirmation_email_fails(rig, monkeypatch):
    client, dav = rig
    import memaix_gateway.booking.routes as booking_routes_mod
    from memaix_gateway.safety.rate_limit import rate_limiter
    rate_limiter._windows.pop("booking:testclient", None)

    def _boom(*a, **kw):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(booking_routes_mod.t_email, "email_send", _boom)
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(18).isoformat(), "end": _dt(18, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_create_booking_rejects_when_slot_no_longer_free(rig):
    client, dav = rig
    dav._events.append({"id": "ev-taken", "title": "Taken", "start": _dt(10).isoformat(), "end": _dt(10, 30).isoformat()})
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(10).isoformat(), "end": _dt(10, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True,
        },
    )
    assert resp.status_code == 409


def test_concurrent_bookings_for_same_slot_only_one_wins(rig):
    # Card d99e2840: pins the required outcome — of two visitors racing for
    # the identical slot, exactly one gets 200 and the other gets 409
    # slot_unavailable, never two overlapping events. This is a result
    # assertion, not proof the _BOOKING_LOCKS lock itself is exercised: with
    # TestClient, booking_create's async handler already serializes on the
    # event loop (no await inside the critical section), so this passes
    # regardless of the lock. What it does guard is the TOCTOU re-check
    # logic never regressing to let both requests through.
    client, dav = rig
    # The module-level rate limiter's window is shared across every test in
    # this file (same client_ip) — clear it so the two concurrent requests
    # below are judged only against this test's own traffic, not the POSTs
    # already made by earlier tests in the module.
    from memaix_gateway.safety.rate_limit import rate_limiter
    rate_limiter._windows.pop("booking:testclient", None)

    payload = {
        "start": _dt(16).isoformat(), "end": _dt(16, 30).isoformat(),
        "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
        "consent": True,
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.post, "/book/alice-30", json=payload) for _ in range(2)]
        responses = [f.result() for f in futures]

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409]
    matching = [e for e in dav._events if e["start"] == _dt(16).isoformat()]
    assert len(matching) == 1


def test_create_booking_fails_closed_without_turnstile_configured(rig, monkeypatch):
    client, _dav = rig
    monkeypatch.setattr(config, "load", lambda: {"memaix": {}})
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(11).isoformat(), "end": _dt(11, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "captcha_failed"


def test_create_booking_rejects_missing_fields(rig):
    client, _dav = rig
    resp = client.post("/book/alice-30", json={"start": _dt(12).isoformat(), "turnstile_token": "tok"})
    assert resp.status_code == 400


def test_cors_headers_present_for_allowed_origin(rig):
    client, _dav = rig
    resp = client.get(
        "/book/alice-30/slots",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()},
        headers={"Origin": "https://jimlov.se"},
    )
    assert resp.headers["access-control-allow-origin"] == "https://jimlov.se"


@pytest.mark.parametrize("origin", ["https://memaix.se", "https://www.memaix.se"])
def test_cors_headers_present_for_memaix_origin(rig, origin):
    # memaix.se/boka fronts the same booking link as jimlov.se/boka, so the
    # gateway has to answer it too — otherwise the slot fetch dies in the
    # browser before the visitor ever sees a calendar.
    client, _dav = rig
    resp = client.get(
        "/book/alice-30/slots",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()},
        headers={"Origin": origin},
    )
    assert resp.headers["access-control-allow-origin"] == origin


def test_cors_headers_absent_for_disallowed_origin(rig):
    client, _dav = rig
    resp = client.get(
        "/book/alice-30/slots",
        params={"within_start": _dt(8).isoformat(), "within_end": _dt(18).isoformat()},
        headers={"Origin": "https://evil.example"},
    )
    assert "access-control-allow-origin" not in resp.headers


def test_real_browser_preflight_reaches_booking_handler_not_app_wide_cors(rig):
    # A real browser preflights any POST with Content-Type: application/json
    # by sending OPTIONS with Access-Control-Request-Method — unlike a bare
    # Origin header (see test_cors_headers_present_for_allowed_origin above),
    # this is what actually exercises the app-wide CORSMiddleware's own
    # preflight interception (Simon's review on card 2bef1062: that
    # middleware only knows claude.ai and would 400 this before it ever
    # reaches booking/routes.py's own OPTIONS handler).
    client, _dav = rig
    resp = client.options(
        "/book/alice-30",
        headers={
            "Origin": "https://jimlov.se",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "https://jimlov.se"


def test_turnstile_fails_closed_without_secret_ref(monkeypatch):
    import asyncio

    from memaix_gateway.booking.routes import _verify_turnstile
    monkeypatch.setattr(config, "load", lambda: {"memaix": {}})
    assert asyncio.run(_verify_turnstile("some-token", "1.2.3.4")) is False


# --- Reschedule / cancel — memaix-src card 8056150d -------------------------


def _manage_token_for(store, project: str, host_user: str) -> str:
    with store._connect() as conn:
        row = conn.execute(
            "SELECT manage_token FROM booking_consent WHERE project = ? AND host_user = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (project, host_user),
        ).fetchone()
    return row["manage_token"]


@pytest.fixture()
def rig_with_store(rig, monkeypatch, tmp_path):
    from memaix_gateway.booking.consent_store import ConsentStore
    import memaix_gateway.booking.routes as booking_routes_mod

    store = ConsentStore(tmp_path / "consent.db")
    monkeypatch.setattr(booking_routes_mod, "get_consent_store", lambda: store)
    client, dav = rig

    from memaix_gateway.safety.rate_limit import rate_limiter
    rate_limiter._windows.pop("booking:testclient", None)
    rate_limiter._windows.pop("booking-manage:testclient", None)

    return client, dav, store


def _book(client, start_h, start_m=0, end_h=None, end_m=30):
    end_h = end_h if end_h is not None else start_h
    return client.post(
        "/book/alice-30",
        json={
            "start": _dt(start_h, start_m).isoformat(), "end": _dt(end_h, end_m).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True, "consent_text": "Jag samtycker.",
        },
    )


def test_reschedule_moves_the_calendar_event(rig_with_store):
    client, dav, store = rig_with_store
    resp = _book(client, 9)
    assert resp.status_code == 200
    token = _manage_token_for(store, "proj", "alice")

    resp = client.post(f"/booking/{token}/reschedule", json={
        "start": _dt(11).isoformat(), "end": _dt(11, 30).isoformat(),
    })
    assert resp.status_code == 200
    assert not any(e["start"] == _dt(9).isoformat() for e in dav._events)
    assert any(e["start"] == _dt(11).isoformat() for e in dav._events)

    row = store.get_by_manage_token(token)
    assert row["status"] == "rescheduled"
    assert row["meeting_end"] == int(_dt(11, 30).timestamp())


def test_reschedule_sends_email_with_new_ics(rig_with_store):
    client, dav, store = rig_with_store
    _book(client, 9)
    token = _manage_token_for(store, "proj", "alice")
    client.smtp.sent.clear()

    resp = client.post(f"/booking/{token}/reschedule", json={
        "start": _dt(11).isoformat(), "end": _dt(11, 30).isoformat(),
    })
    assert resp.status_code == 200
    assert len(client.smtp.sent) == 2
    recipients = {msg["To"] for msg in client.smtp.sent}
    assert recipients == {"bob@example.com", "alice@example.com"}


def test_reschedule_to_an_overlapping_window_excludes_own_event(rig_with_store):
    # The booking's own event still overlaps its new window until
    # calendar_update actually moves it — the free/busy re-check must not
    # mistake it for a conflicting event (memaix-src card 8056150d).
    client, dav, store = rig_with_store
    _book(client, 9)
    token = _manage_token_for(store, "proj", "alice")

    resp = client.post(f"/booking/{token}/reschedule", json={
        "start": _dt(9, 15).isoformat(), "end": _dt(9, 45).isoformat(),
    })
    assert resp.status_code == 200
    assert any(e["start"] == _dt(9, 15).isoformat() for e in dav._events)


def test_reschedule_rejects_when_new_slot_taken(rig_with_store):
    client, dav, store = rig_with_store
    _book(client, 9)
    token = _manage_token_for(store, "proj", "alice")
    dav._events.append({"id": "ev-taken", "title": "Taken", "start": _dt(11).isoformat(), "end": _dt(11, 30).isoformat()})

    resp = client.post(f"/booking/{token}/reschedule", json={
        "start": _dt(11).isoformat(), "end": _dt(11, 30).isoformat(),
    })
    assert resp.status_code == 409
    row = store.get_by_manage_token(token)
    assert row["status"] == "confirmed"  # untouched


def test_reschedule_unknown_token_is_404(rig_with_store):
    client, _dav, _store = rig_with_store
    resp = client.post("/booking/no-such-token/reschedule", json={
        "start": _dt(11).isoformat(), "end": _dt(11, 30).isoformat(),
    })
    assert resp.status_code == 404


def test_reschedule_a_cancelled_booking_is_rejected(rig_with_store):
    client, _dav, store = rig_with_store
    _book(client, 9)
    token = _manage_token_for(store, "proj", "alice")
    assert client.post(f"/booking/{token}/cancel", json={}).status_code == 200

    resp = client.post(f"/booking/{token}/reschedule", json={
        "start": _dt(11).isoformat(), "end": _dt(11, 30).isoformat(),
    })
    assert resp.status_code == 409
    assert resp.json()["error"] == "already_cancelled"


def test_cancel_deletes_the_calendar_event(rig_with_store):
    client, dav, store = rig_with_store
    _book(client, 9)
    token = _manage_token_for(store, "proj", "alice")

    resp = client.post(f"/booking/{token}/cancel", json={})
    assert resp.status_code == 200
    assert not any(e["start"] == _dt(9).isoformat() for e in dav._events)
    row = store.get_by_manage_token(token)
    assert row["status"] == "cancelled"


def test_cancel_sends_cancellation_emails(rig_with_store):
    client, dav, store = rig_with_store
    _book(client, 9)
    token = _manage_token_for(store, "proj", "alice")
    client.smtp.sent.clear()

    resp = client.post(f"/booking/{token}/cancel", json={})
    assert resp.status_code == 200
    assert len(client.smtp.sent) == 2
    for msg in client.smtp.sent:
        assert "Avbokat" in msg["Subject"]


def test_cancel_twice_is_rejected(rig_with_store):
    client, _dav, store = rig_with_store
    _book(client, 9)
    token = _manage_token_for(store, "proj", "alice")
    assert client.post(f"/booking/{token}/cancel", json={}).status_code == 200

    resp = client.post(f"/booking/{token}/cancel", json={})
    assert resp.status_code == 409
    assert resp.json()["error"] == "already_cancelled"


def test_cancel_unknown_token_is_404(rig_with_store):
    client, _dav, _store = rig_with_store
    resp = client.post("/booking/no-such-token/cancel", json={})
    assert resp.status_code == 404


def test_manage_get_returns_status_and_meeting_end(rig_with_store):
    client, _dav, store = rig_with_store
    _book(client, 9)
    token = _manage_token_for(store, "proj", "alice")

    resp = client.get(f"/booking/{token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["meeting_end"] == int(_dt(9, 30).timestamp())


def test_manage_get_unknown_token_is_404(rig_with_store):
    client, _dav, _store = rig_with_store
    resp = client.get("/booking/no-such-token")
    assert resp.status_code == 404


def test_confirmation_email_includes_manage_link(rig_with_store):
    client, dav, store = rig_with_store
    client.smtp.sent.clear()
    resp = _book(client, 9)
    assert resp.status_code == 200
    token = _manage_token_for(store, "proj", "alice")
    for msg in client.smtp.sent:
        body = msg.get_body(preferencelist=("plain",)).get_content()
        assert token in body


# --- Meeting forms — memaix-src card 85854d2c -------------------------------


class _MockDavWithMeet(_MockDav):
    """Same as _MockDav but honours want_conference, mirroring the real
    Google Calendar adapter's conferenceData handling (tools/calendar.py's
    _to_dict) closely enough for booking_create's google_meet path."""

    def create_event(self, uid, title, start, end, attendees=None, location=None, description=None, want_conference=False):
        ev = {
            "id": uid, "title": title, "start": start.isoformat(), "end": end.isoformat(),
            "description": description, "location": location,
        }
        if want_conference:
            ev["meet_url"] = f"https://meet.google.com/{uid}"
        self._events.append(ev)
        return ev


def _set_forms(acl, project, user, forms):
    from memaix_gateway.connectors.meeting_forms import MeetingFormsStore

    return MeetingFormsStore(acl, project, user).set(forms)


def test_create_booking_with_no_forms_configured_is_unchanged(rig):
    # Pure backward-compat: a host who never touched meeting forms sees
    # identical behaviour to before this card — no location set, no error.
    client, dav = rig
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(21).isoformat(), "end": _dt(21, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True,
        },
    )
    assert resp.status_code == 200
    ev = next(e for e in dav._events if e["start"] == _dt(21).isoformat())
    assert ev.get("location") is None


def test_create_booking_auto_selects_default_phone_form(rig):
    client, dav = rig
    from memaix_gateway import server as server_mod

    _set_forms(server_mod._acl, "proj", "alice", [
        {"slug": "phone", "provider": "phone", "label": "Ring oss", "config": {"phone_number": "+46701234567"}},
    ])
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(22).isoformat(), "end": _dt(22, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True,
        },
    )
    assert resp.status_code == 200
    ev = next(e for e in dav._events if e["start"] == _dt(22).isoformat())
    assert ev["description"] is None  # sanity: purpose untouched by the form


def test_create_booking_phone_form_ends_up_in_calendar_location_with_no_http_call(rig, monkeypatch):
    client, dav = rig
    from memaix_gateway import server as server_mod

    _set_forms(server_mod._acl, "proj", "alice", [
        {"slug": "phone", "provider": "phone", "label": "Ring oss", "config": {"phone_number": "+46701234567"}},
    ])

    def _boom(*a, **kw):
        raise AssertionError("phone form must never make an HTTP call")

    monkeypatch.setattr("requests.post", _boom)
    monkeypatch.setattr("requests.patch", _boom)
    monkeypatch.setattr("requests.delete", _boom)

    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(23).isoformat(), "end": _dt(23, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True, "meeting_form_slug": "phone",
        },
    )
    assert resp.status_code == 200
    ev = next(e for e in dav._events if e["start"] == _dt(23).isoformat())
    assert ev["location"] == "+46701234567"


def test_create_booking_explicit_valid_slug_selects_that_form(rig):
    client, dav = rig
    from memaix_gateway import server as server_mod

    _set_forms(server_mod._acl, "proj", "alice", [
        {"slug": "phone", "provider": "phone", "label": "Ring oss", "config": {"phone_number": "+46701234567"}, "default": True},
        {"slug": "phone2", "provider": "phone", "label": "Ring oss 2", "config": {"phone_number": "+46709999999"}},
    ])
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(0).isoformat(), "end": _dt(0, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True, "meeting_form_slug": "phone2",
        },
    )
    assert resp.status_code == 200


def test_create_booking_invalid_meeting_form_slug_is_400(rig):
    client, dav = rig
    from memaix_gateway import server as server_mod

    _set_forms(server_mod._acl, "proj", "alice", [
        {"slug": "phone", "provider": "phone", "label": "Ring oss", "config": {"phone_number": "+46701234567"}},
    ])
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(1).isoformat(), "end": _dt(1, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True, "meeting_form_slug": "does-not-exist",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_meeting_form"


def test_create_booking_provider_error_surfaces_as_502(rig, monkeypatch):
    client, dav = rig
    from memaix_gateway import server as server_mod

    _set_forms(server_mod._acl, "proj", "alice", [
        {"slug": "zoomform", "provider": "zoom", "label": "Zoom"},
    ])

    import memaix_gateway.booking.routes as booking_routes_mod
    from memaix_gateway.booking.meeting_providers import MeetingProviderError

    def _boom(*a, **kw):
        raise MeetingProviderError("zoom not configured for this deployment")

    monkeypatch.setattr(booking_routes_mod, "resolve_meeting_detail", _boom)

    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(2).isoformat(), "end": _dt(2, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True, "meeting_form_slug": "zoomform",
        },
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "meeting_form_unavailable"
    assert not any(e["start"] == _dt(2).isoformat() for e in dav._events)


def test_create_booking_google_meet_form_resolves_after_calendar_create(rig, monkeypatch):
    from memaix_gateway import server as server_mod
    import memaix_gateway.booking.routes as booking_routes_mod

    dav = _MockDavWithMeet()
    monkeypatch.setattr(server_mod, "_resolve_calendar_dav", lambda project, user, write=False: dav)
    monkeypatch.setattr(booking_routes_mod, "_resolve_dav_filtered", lambda project, user, acl: dav)
    client, _old_dav = rig

    _set_forms(server_mod._acl, "proj", "alice", [
        {"slug": "meet", "provider": "google_meet", "label": "Google Meet"},
    ])

    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(3).isoformat(), "end": _dt(3, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True,
        },
    )
    assert resp.status_code == 200
    ev = next(e for e in dav._events if e["start"] == _dt(3).isoformat())
    assert ev.get("meet_url", "").startswith("https://meet.google.com/")


def test_create_booking_google_meet_missing_meet_url_rolls_back_event(rig, monkeypatch):
    # conferenceData provisioning can fail on Google's side even though
    # calendar_create itself succeeds — the event comes back with no
    # meet_url, MeetingProviderError fires, and the handler must not leave
    # an orphaned event (visitor invited, no consent record) behind.
    from memaix_gateway import server as server_mod
    import memaix_gateway.booking.routes as booking_routes_mod

    class _MockDavMeetProvisionFails(_MockDav):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.deleted_ids: list = []

        def create_event(self, uid, title, start, end, attendees=None, location=None, description=None, want_conference=False):
            ev = {
                "id": uid, "title": title, "start": start.isoformat(), "end": end.isoformat(),
                "description": description, "location": location,
            }
            # want_conference is honoured but no meet_url key is set — this
            # mirrors Google returning an event with no conferenceData.
            self._events.append(ev)
            return ev

        def delete_event(self, id):
            self.deleted_ids.append(id)
            super().delete_event(id)

    dav = _MockDavMeetProvisionFails()
    monkeypatch.setattr(server_mod, "_resolve_calendar_dav", lambda project, user, write=False: dav)
    monkeypatch.setattr(booking_routes_mod, "_resolve_dav_filtered", lambda project, user, acl: dav)
    client, _old_dav = rig

    _set_forms(server_mod._acl, "proj", "alice", [
        {"slug": "meet", "provider": "google_meet", "label": "Google Meet"},
    ])

    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(4).isoformat(), "end": _dt(4, 30).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok",
            "consent": True,
        },
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "meeting_form_unavailable"
    assert not any(e["start"] == _dt(4).isoformat() for e in dav._events)
    assert len(dav.deleted_ids) == 1


def _set_types(acl, project, user, types):
    from memaix_gateway.connectors.meeting_types import MeetingTypesStore

    return MeetingTypesStore(acl, project, user).set(types)


def test_config_lists_session_lengths_and_defaults_duration_to_default_type(rig):
    client, _dav = rig
    from memaix_gateway import server as server_mod

    _set_types(server_mod._acl, "proj", "alice", [
        {"slug": "kort", "name": "30 min", "duration_min": 30},
        {"slug": "lang", "name": "2 timmar", "duration_min": 120, "default": True},
    ])
    body = client.get("/book/alice-30/config").json()
    assert body["duration_min"] == 120
    assert [(t["slug"], t["default"]) for t in body["meeting_types"]] == [("kort", False), ("lang", True)]


def test_create_booking_rejects_length_not_among_host_session_lengths(rig):
    client, dav = rig
    from memaix_gateway import server as server_mod

    _set_types(server_mod._acl, "proj", "alice", [{"slug": "kort", "name": "30 min", "duration_min": 30}])
    resp = client.post(
        "/book/alice-30",
        json={
            "start": _dt(21).isoformat(), "end": _dt(21, 45).isoformat(),
            "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok", "consent": True,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_duration"
    assert not dav._events or all(e["start"] != _dt(21).isoformat() for e in dav._events)


def test_second_booking_same_day_is_refused_when_day_cap_reached(rig, monkeypatch, tmp_path):
    import memaix_gateway.booking.routes as booking_routes_mod
    from memaix_gateway.booking import consent_store as cs
    from memaix_gateway import server as server_mod
    from memaix_gateway.connectors.working_hours import WorkingHoursStore

    store = cs.ConsentStore(tmp_path / "cap-consent.db")
    monkeypatch.setattr(booking_routes_mod, "get_consent_store", lambda: store)
    monkeypatch.setattr(cs, "_store", store)
    client, _dav = rig
    WorkingHoursStore(server_mod._acl, "proj", "alice").set_extras(max_per_day=1)

    def book(hour):
        return client.post(
            "/book/alice-30",
            json={
                "start": _dt(hour).isoformat(), "end": _dt(hour, 30).isoformat(),
                "name": "Bob", "email": "bob@example.com", "turnstile_token": "tok", "consent": True,
            },
        )

    assert book(21).status_code == 200
    second = book(22)
    assert second.status_code == 409
    assert second.json()["error"] == "slot_unavailable"
