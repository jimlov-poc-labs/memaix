# SPDX-License-Identifier: AGPL-3.0-or-later
"""Characterization tests for ``_ICalAdapter._fetch``: SSRF guard before the
request, redirects disabled, HTTP errors, and the vobject -> event-dict
mapping (all-day vs timed, missing DTEND, series/exception identity, TRANSP,
missing UID). ``requests`` and DNS are mocked; no real network access."""

from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest
import requests
import vobject

import memaix_gateway.safety.net as net
from memaix_gateway.safety.net import BlockedURLError
from memaix_gateway.tools.calendar import _ICalAdapter

URL = "https://calendar.example.com/secret/basic.ics"


def _cl(name: str, value: str) -> str:
    """How the adapter renders a property: the vobject ContentLine's .value, stripped."""
    return value.strip()


def _ics(*events: str, extra: str = "") -> str:
    body = "".join(f"BEGIN:VEVENT\r\n{e}\r\nEND:VEVENT\r\n" for e in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n{extra}{body}END:VCALENDAR\r\n"


class _Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status
        self.raised = False

    def raise_for_status(self):
        self.raised = True
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


@pytest.fixture()
def http(monkeypatch):
    """Mock DNS (public by default) and requests.get; records the calls."""
    class _H:
        resp = _Resp(_ics())
        calls: list = []
        dns_answer: object = ["93.184.216.34"]
        raise_on_get: BaseException | None = None

    h = _H()
    h.calls = []

    def fake_get(url, **kw):
        h.calls.append((url, kw))
        if h.raise_on_get:
            raise h.raise_on_get
        return h.resp

    def fake_dns(host, port):
        if isinstance(h.dns_answer, BaseException):
            raise h.dns_answer
        return [(socket.AF_INET, 0, 0, "", (ip, port)) for ip in h.dns_answer]

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(net.socket, "getaddrinfo", fake_dns)
    return h


# ---- SSRF / transport -----------------------------------------------------

def test_request_uses_timeout_and_never_follows_redirects(http):
    _ICalAdapter(URL)._fetch()
    assert http.calls == [(URL, {"timeout": 10, "allow_redirects": False})]
    assert http.resp.raised is True


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x.ics",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/x.ics",
    "http://192.168.1.1/x.ics",
    "http://[::1]/x.ics",
    "http://[fe80::1]/x.ics",
    "http://[::ffff:127.0.0.1]/x.ics",
    "http://0.0.0.0/x.ics",
])
def test_private_literal_ip_is_blocked_before_any_request(url, http):
    with pytest.raises(BlockedURLError, match="non-public address"):
        _ICalAdapter(url)._fetch()
    assert http.calls == []


@pytest.mark.parametrize("url", ["ftp://example.com/x.ics", "file:///etc/passwd", "gopher://x", "", "example.com/x.ics"])
def test_non_http_scheme_is_blocked_before_any_request(url, http):
    with pytest.raises(BlockedURLError):
        _ICalAdapter(url)._fetch()
    assert http.calls == []


@pytest.mark.parametrize("answer", [["127.0.0.1"], ["10.1.1.1"], ["93.184.216.34", "169.254.169.254"], ["::1"]])
def test_hostname_resolving_private_is_blocked_before_any_request(answer, http):
    http.dns_answer = answer
    with pytest.raises(BlockedURLError, match="resolves to a non-public address"):
        _ICalAdapter("https://rebind.example.com/x.ics")._fetch()
    assert http.calls == []


def test_dns_failure_is_blocked_before_any_request(http):
    http.dns_answer = socket.gaierror(-2, "Name or service not known")
    with pytest.raises(BlockedURLError, match="could not resolve host"):
        _ICalAdapter(URL)._fetch()
    assert http.calls == []


def test_redirect_response_is_not_followed(http):
    # With allow_redirects=False a 302 is returned as-is: exactly one request
    # is made (never to the Location target) and the empty body fails parsing.
    http.resp = _Resp("", status=302)
    with pytest.raises(StopIteration):
        _ICalAdapter(URL)._fetch()
    assert len(http.calls) == 1 and http.calls[0][0] == URL


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_http_error_status_propagates(status, http):
    http.resp = _Resp("", status=status)
    with pytest.raises(requests.HTTPError, match=str(status)):
        _ICalAdapter(URL)._fetch()


@pytest.mark.parametrize("exc", [requests.ConnectionError("down"), requests.Timeout("slow")])
def test_transport_errors_propagate(exc, http):
    http.raise_on_get = exc
    with pytest.raises(type(exc)):
        _ICalAdapter(URL)._fetch()


def test_unparseable_body_raises_parse_error(http):
    http.resp = _Resp("this is not ics")
    with pytest.raises(vobject.base.ParseError):
        _ICalAdapter(URL)._fetch()


# ---- parsing --------------------------------------------------------------

def test_calendar_without_events_is_empty(http):
    http.resp = _Resp(_ics())
    assert _ICalAdapter(URL)._fetch() == []


def test_non_vevent_components_are_ignored(http):
    todo = "BEGIN:VTODO\r\nUID:t1\r\nSUMMARY:todo\r\nEND:VTODO\r\n"
    http.resp = _Resp(_ics("UID:e1\r\nDTSTART:20260106T100000Z\r\nSUMMARY:real", extra=todo))
    assert [e["id"] for e in _ICalAdapter(URL)._fetch()] == [_cl("UID", "e1")]


def test_timed_utc_event_full_mapping(http):
    http.resp = _Resp(_ics(
        "UID: abc \r\nDTSTART:20260106T100000Z\r\nDTEND:20260106T110000Z\r\n"
        "SUMMARY:  Möte \r\nLOCATION: Rum 4\r\nDESCRIPTION: Agenda "
    ))
    (ev,) = _ICalAdapter(URL)._fetch()
    start = datetime(2026, 1, 6, 10, tzinfo=timezone.utc)
    end = datetime(2026, 1, 6, 11, tzinfo=timezone.utc)
    assert ev == {
        "id": _cl("UID", " abc "), "title": _cl("SUMMARY", "  Möte "),
        "start": start.isoformat(), "end": end.isoformat(),
        "location": _cl("LOCATION", " Rum 4"), "description": _cl("DESCRIPTION", " Agenda "),
        "series_id": None, "is_exception": False, "source_busy": True,
        "_dtstart": start, "_dtend": end,
    }


def test_missing_optional_fields_become_empty_strings(http):
    http.resp = _Resp(_ics("UID:e1\r\nDTSTART:20260106T100000Z"))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert (ev["title"], ev["location"], ev["description"]) == ("", "", "")


def test_missing_dtend_falls_back_to_dtstart(http):
    http.resp = _Resp(_ics("UID:e1\r\nDTSTART:20260106T100000Z"))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert ev["end"] == ev["start"] == "2026-01-06T10:00:00+00:00"
    assert ev["_dtend"] == ev["_dtstart"]


def test_all_day_dates_are_normalized_to_utc_midnight(http):
    http.resp = _Resp(_ics("UID:e1\r\nDTSTART;VALUE=DATE:20260105\r\nDTEND;VALUE=DATE:20260106"))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert ev["start"] == "2026-01-05T00:00:00+00:00"
    assert ev["end"] == "2026-01-06T00:00:00+00:00"
    assert ev["_dtstart"] == datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert ev["_dtend"] == datetime(2026, 1, 6, tzinfo=timezone.utc)


def test_all_day_without_dtend_ends_at_start(http):
    http.resp = _Resp(_ics("UID:e1\r\nDTSTART;VALUE=DATE:20260105"))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert ev["start"] == ev["end"] == "2026-01-05T00:00:00+00:00"


def test_floating_time_stays_naive(http):
    http.resp = _Resp(_ics("UID:e1\r\nDTSTART:20260107T090000\r\nDTEND:20260107T100000"))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert ev["start"] == "2026-01-07T09:00:00" and ev["end"] == "2026-01-07T10:00:00"
    assert ev["_dtstart"].tzinfo is None


def test_tzid_event_keeps_its_offset(http):
    tz = ("BEGIN:VTIMEZONE\r\nTZID:Etc/GMT-2\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\n"
          "TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0200\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n")
    http.resp = _Resp(_ics("UID:e1\r\nDTSTART;TZID=Etc/GMT-2:20260107T090000\r\nDTEND;TZID=Etc/GMT-2:20260107T100000", extra=tz))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert ev["start"] == "2026-01-07T09:00:00+02:00"


@pytest.mark.parametrize("transp,busy", [
    ("TRANSPARENT", False), ("transparent", False), (" Transparent ", False),
    ("OPAQUE", True), ("opaque", True), ("", True),
])
def test_transp_transparent_means_free(transp, busy, http):
    line = f"\r\nTRANSP:{transp}" if transp else ""
    http.resp = _Resp(_ics(f"UID:e1\r\nDTSTART:20260106T100000Z{line}"))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert ev["source_busy"] is busy


def test_missing_uid_gets_positional_id(http):
    http.resp = _Resp(_ics(
        "UID:first\r\nDTSTART:20260106T100000Z",
        "DTSTART:20260106T110000Z",
        "UID:third\r\nDTSTART:20260106T120000Z",
        "DTSTART:20260106T130000Z",
    ))
    assert [e["id"] for e in _ICalAdapter(URL)._fetch()] == [_cl("UID", "first"), "ical-1", _cl("UID", "third"), "ical-3"]


def test_series_master_and_exception_share_series_id(http):
    http.resp = _Resp(_ics(
        "UID:ser\r\nDTSTART:20260106T100000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nSUMMARY:master",
        "UID:ser\r\nDTSTART:20260113T150000Z\r\nRECURRENCE-ID:20260113T100000Z\r\nSUMMARY:moved",
        "UID:single\r\nDTSTART:20260106T100000Z\r\nSUMMARY:one-off",
    ))
    master, exc, single = _ICalAdapter(URL)._fetch()
    ser = _cl("UID", "ser")
    assert (master["series_id"], master["is_exception"]) == (ser, False)
    assert (exc["series_id"], exc["is_exception"]) == (ser, True)
    assert (single["series_id"], single["is_exception"]) == (None, False)


def test_orphan_exception_alone_marks_its_uid_as_series(http):
    http.resp = _Resp(_ics("UID:x\r\nDTSTART:20260113T150000Z\r\nRECURRENCE-ID:20260113T100000Z"))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert (ev["series_id"], ev["is_exception"]) == (_cl("UID", "x"), True)


def test_event_without_uid_is_never_a_series_even_with_rrule(http):
    http.resp = _Resp(_ics("DTSTART:20260106T100000Z\r\nRRULE:FREQ=DAILY;COUNT=2"))
    (ev,) = _ICalAdapter(URL)._fetch()
    assert ev["id"] == "ical-0" and ev["series_id"] is None


def test_event_without_dtstart_raises_attribute_error(http):
    # Locked as-is: DTSTART is assumed present.
    http.resp = _Resp(_ics("UID:e1\r\nSUMMARY:no start"))
    with pytest.raises(AttributeError):
        _ICalAdapter(URL)._fetch()


def test_list_events_filters_range_and_strips_private_keys(http):
    http.resp = _Resp(_ics(
        "UID:in\r\nDTSTART:20260106T100000Z\r\nDTEND:20260106T110000Z",
        "UID:out\r\nDTSTART:20260301T100000Z\r\nDTEND:20260301T110000Z",
    ))
    got = _ICalAdapter(URL).list_events(
        datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)
    )
    assert [e["id"] for e in got] == [_cl("UID", "in")]
    assert not any(k.startswith("_") for k in got[0])
