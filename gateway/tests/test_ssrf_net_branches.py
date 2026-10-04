# SPDX-License-Identifier: AGPL-3.0-or-later
"""Characterization tests for ``safety.net.validate_external_url`` and
``_is_blocked_ip``: every branch of the SSRF guard (schemes, missing host,
literal IPv4/IPv6, DNS resolution incl. mixed/failed results, ports). DNS is
always mocked; no real network access."""

from __future__ import annotations

import socket

import pytest

import memaix_gateway.safety.net as net
from memaix_gateway.safety.net import BlockedURLError, _is_blocked_ip, validate_external_url


@pytest.fixture()
def dns(monkeypatch):
    """Mock getaddrinfo. ``dns.answer`` is a list of IP strings (or an
    exception to raise); ``dns.calls`` records (host, port)."""
    class _Dns:
        answer: object = ["93.184.216.34"]
        calls: list = []

    state = _Dns()
    state.calls = []

    def fake(host, port):
        state.calls.append((host, port))
        if isinstance(state.answer, BaseException):
            raise state.answer
        return [(socket.AF_INET, 0, 0, "", (ip, port)) for ip in state.answer]

    monkeypatch.setattr(net.socket, "getaddrinfo", fake)
    return state


# ---- _is_blocked_ip -------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.255.255.254",            # loopback
    "10.1.2.3", "172.16.0.1", "192.168.0.1",   # RFC1918
    "169.254.169.254",                         # link-local / cloud metadata
    "0.0.0.0",                                 # unspecified
    "224.0.0.1",                               # multicast
    "240.0.0.1", "255.255.255.255",            # reserved / broadcast
    "192.0.0.1", "198.18.0.1",                 # special-purpose private ranges
    "::1", "::",                               # ipv6 loopback / unspecified
    "fe80::1", "fc00::1", "fd12::1",           # ipv6 link-local / unique-local
    "ff02::1",                                 # ipv6 multicast
    "2001:db8::1",                             # ipv6 documentation range
    "::ffff:127.0.0.1",                        # ipv4-mapped loopback
    "64:ff9b::7f00:1", "2002:7f00:1::",        # NAT64 / 6to4 wrapping 127.0.0.1
    "garbage", "", "999.1.1.1",                # unparseable -> blocked
])
def test_is_blocked_ip_true(ip):
    assert _is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700::1111", "::ffff:8.8.8.8"])
def test_is_blocked_ip_false_for_public(ip):
    assert _is_blocked_ip(ip) is False


def test_is_blocked_ip_cgnat_is_not_blocked():
    # Locked as-is (potential gap): 100.64.0.0/10 (carrier-grade NAT, also
    # used by Tailscale) is neither "private" nor "reserved" in the stdlib.
    assert _is_blocked_ip("100.64.0.1") is False


# ---- shape checks ---------------------------------------------------------

@pytest.mark.parametrize("url", ["", None, 123, b"http://x", ["http://x"]])
def test_empty_or_non_string_url(url):
    with pytest.raises(BlockedURLError, match="^empty or non-string URL$"):
        validate_external_url(url)


@pytest.mark.parametrize("url,scheme", [
    ("ftp://example.com/x", "ftp"),
    ("file:///etc/passwd", "file"),
    ("gopher://example.com", "gopher"),
    ("javascript:alert(1)", "javascript"),
    ("//example.com/x", ""),
    ("example.com/x", ""),
])
def test_scheme_must_be_http_or_https(url, scheme, dns):
    with pytest.raises(BlockedURLError) as ei:
        validate_external_url(url)
    assert str(ei.value) == f"URL scheme must be http/https, got {scheme!r}"
    assert dns.calls == []


@pytest.mark.parametrize("url", ["http://", "http:///path", "https:example.com", "http://:80/"])
def test_missing_host(url, dns):
    with pytest.raises(BlockedURLError, match="^URL has no host$"):
        validate_external_url(url)
    assert dns.calls == []


def test_uppercase_scheme_is_accepted():
    assert validate_external_url("HTTP://8.8.8.8/x", resolve=False) == "HTTP://8.8.8.8/x"


def test_malformed_ipv6_url_raises_plain_valueerror():
    # Locked as-is: urlparse itself raises, not a BlockedURLError.
    with pytest.raises(ValueError) as ei:
        validate_external_url("http://[bad/", resolve=False)
    assert not isinstance(ei.value, BlockedURLError)


# ---- literal IPs ----------------------------------------------------------

@pytest.mark.parametrize("url,host", [
    ("http://127.0.0.1/", "127.0.0.1"),
    ("https://10.0.0.5:8443/x", "10.0.0.5"),
    ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
    ("http://user:pw@192.168.1.1/", "192.168.1.1"),
    ("http://[::1]/", "::1"),
    ("http://[::ffff:127.0.0.1]/", "::ffff:127.0.0.1"),
    ("http://[fe80::1]/", "fe80::1"),
    ("http://[fc00::1]:8080/", "fc00::1"),
    ("http://0.0.0.0/", "0.0.0.0"),
])
@pytest.mark.parametrize("resolve", [True, False])
def test_literal_non_public_ip_blocked_regardless_of_resolve(url, host, resolve, dns):
    with pytest.raises(BlockedURLError) as ei:
        validate_external_url(url, resolve=resolve)
    assert str(ei.value) == f"URL host is a non-public address: {host}"
    assert dns.calls == [], "literal IPs never hit DNS"


@pytest.mark.parametrize("url", [
    "http://8.8.8.8/", "https://93.184.216.34:8443/x", "http://[2606:4700::1111]/",
])
@pytest.mark.parametrize("resolve", [True, False])
def test_literal_public_ip_allowed_without_dns(url, resolve, dns):
    assert validate_external_url(url, resolve=resolve) == url
    assert dns.calls == []


def test_literal_ip_port_is_not_validated():
    # Locked as-is: parsed.port is only read on the DNS path.
    url = "http://8.8.8.8:99999/"
    assert validate_external_url(url) == url


# ---- hostnames / DNS ------------------------------------------------------

def test_hostname_without_resolve_skips_dns(dns):
    url = "https://hooks.example.com/abc"
    assert validate_external_url(url, resolve=False) == url
    assert dns.calls == []


def test_hostname_that_looks_private_is_not_checked_without_resolve(dns):
    assert validate_external_url("http://localhost/x", resolve=False) == "http://localhost/x"
    assert dns.calls == []


def test_resolve_is_the_default(dns):
    validate_external_url("https://example.com/x")
    assert dns.calls == [("example.com", 443)]


@pytest.mark.parametrize("url,port", [
    ("http://example.com/x", 80),
    ("https://example.com/x", 443),
    ("http://example.com:8080/x", 8080),
    ("https://example.com:8443/x", 8443),
    ("HTTPS://example.com/x", 443),
])
def test_resolve_uses_scheme_default_or_explicit_port(url, port, dns):
    assert validate_external_url(url) == url
    assert dns.calls == [("example.com", port)]


def test_port_zero_falls_back_to_scheme_default(dns):
    # Locked as-is: `parsed.port or default` treats port 0 as unset.
    validate_external_url("http://example.com:0/x")
    assert dns.calls == [("example.com", 80)]


def test_out_of_range_port_raises_plain_valueerror_on_dns_path(dns):
    with pytest.raises(ValueError) as ei:
        validate_external_url("http://example.com:99999/x")
    assert not isinstance(ei.value, BlockedURLError)
    assert dns.calls == []


def test_hostname_resolving_public_is_allowed(dns):
    dns.answer = ["93.184.216.34", "8.8.8.8"]
    url = "https://example.com/x"
    assert validate_external_url(url) == url


@pytest.mark.parametrize("bad", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.0.9", "0.0.0.0", "::1", "fe80::1", "fc00::1",
    "::ffff:10.0.0.1", "garbage",
])
def test_hostname_resolving_to_non_public_is_blocked(bad, dns):
    dns.answer = [bad]
    with pytest.raises(BlockedURLError) as ei:
        validate_external_url("https://sneaky.example.com/x")
    assert str(ei.value) == f"URL host 'sneaky.example.com' resolves to a non-public address: {bad}"


@pytest.mark.parametrize("answer", [
    ["93.184.216.34", "10.0.0.1"],
    ["10.0.0.1", "93.184.216.34"],
])
def test_any_private_address_among_several_blocks(answer, dns):
    dns.answer = answer
    with pytest.raises(BlockedURLError, match="resolves to a non-public address: 10.0.0.1$"):
        validate_external_url("https://rebind.example.com/x")


def test_first_blocked_address_is_the_one_reported(dns):
    dns.answer = ["127.0.0.1", "10.0.0.1"]
    with pytest.raises(BlockedURLError, match=r": 127\.0\.0\.1$"):
        validate_external_url("https://rebind.example.com/x")


def test_ipv6_getaddrinfo_tuples_are_checked(monkeypatch):
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [(socket.AF_INET6, 0, 0, "", ("::1", port, 0, 0))],
    )
    with pytest.raises(BlockedURLError, match="resolves to a non-public address: ::1$"):
        validate_external_url("https://v6.example.com/x")


def test_dns_failure_is_blocked_with_cause(dns):
    err = socket.gaierror(-2, "Name or service not known")
    dns.answer = err
    with pytest.raises(BlockedURLError) as ei:
        validate_external_url("https://nope.invalid/x")
    assert str(ei.value) == f"could not resolve host 'nope.invalid': {err}"
    assert ei.value.__cause__ is err


def test_empty_resolution_result_is_allowed(dns):
    # Locked as-is: no addresses -> nothing to block -> URL passes.
    dns.answer = []
    url = "https://example.com/x"
    assert validate_external_url(url) == url


def test_url_is_returned_unchanged(dns):
    url = "https://Example.com:443/a/b?c=d#e"
    assert validate_external_url(url) == url


def test_non_gaierror_from_dns_propagates(dns):
    dns.answer = OSError("boom")
    with pytest.raises(OSError, match="boom"):
        validate_external_url("https://example.com/x")


def test_blocked_url_error_is_a_valueerror():
    assert issubclass(BlockedURLError, ValueError)
