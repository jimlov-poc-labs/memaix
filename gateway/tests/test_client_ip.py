# SPDX-License-Identifier: AGPL-3.0-or-later
"""H8: klient-IP bakom proxy (Cloudflare -> cloudflared -> Caddy -> gateway).

Rate limit-hinken ska följa riktig klient när (och bara när) peer är en betrodd
proxy. Spoofad X-Forwarded-For från icke-betrodd källa ska ignoreras.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from memaix_gateway.acl import Acl
from memaix_gateway.rules.store import RulesStore
from memaix_gateway.safety.rate_limit import RateLimiter

TRUSTED = "172.16.0.0/12"
CADDY = ("172.21.0.6", 4000)
OUTSIDE = ("198.51.100.9", 4000)


@pytest.fixture()
def webhook(tmp_path, monkeypatch):
    from memaix_gateway import server as server_mod

    acl = Acl(users={"alice": {"grants": {"proj": "owner"}}}, projects={"proj": {"vault": str(tmp_path / "vault")}})
    monkeypatch.setattr(server_mod, "_acl", acl)
    monkeypatch.setattr(server_mod, "_rules_store", RulesStore.for_path(tmp_path / "rules.db"))
    monkeypatch.setattr(server_mod, "_rate_limiter", RateLimiter())
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", TRUSTED)
    app = server_mod.build_http_app()

    def make(peer):
        return TestClient(app, client=peer)

    return make


def _burn(client, xff, n=30):
    for _ in range(n):
        client.post("/hooks/nope", json={}, headers={"X-Forwarded-For": xff})


def test_trusted_proxy_gives_separate_buckets_per_client(webhook):
    client = webhook(CADDY)
    _burn(client, "203.0.113.1")
    assert client.post("/hooks/nope", json={}, headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 429
    assert client.post("/hooks/nope", json={}, headers={"X-Forwarded-For": "203.0.113.2"}).status_code != 429


def test_xff_from_untrusted_peer_cannot_dodge_limit(webhook):
    client = webhook(OUTSIDE)
    _burn(client, "203.0.113.1")
    for spoof in ("203.0.113.77", "10.9.9.9", "garbage"):
        r = client.post("/hooks/nope", json={}, headers={"X-Forwarded-For": spoof})
        assert r.status_code == 429


def test_no_forwarded_allow_ips_means_trust_nobody(webhook, monkeypatch):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS")
    client = webhook(CADDY)
    _burn(client, "203.0.113.1")
    assert client.post("/hooks/nope", json={}, headers={"X-Forwarded-For": "203.0.113.2"}).status_code == 429


def _req(peer, xff=None):
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": headers, "client": peer, "method": "GET", "path": "/"})


def test_client_ip_rules(monkeypatch):
    from memaix_gateway.safety.client_ip import client_ip

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", TRUSTED)
    # rightmost untrusted wins; client-supplied left part is ignored
    assert client_ip(_req(CADDY, "6.6.6.6, 203.0.113.5, 172.21.0.8")) == "203.0.113.5"
    # trasig header från betrodd proxy -> peer, aldrig fri hink
    assert client_ip(_req(CADDY, "not-an-ip")) == "172.21.0.6"
    assert client_ip(_req(CADDY, "")) == "172.21.0.6"
    assert client_ip(_req(CADDY)) == "172.21.0.6"
    # IPv4-mappad och ren form ger samma nyckel
    assert client_ip(_req(CADDY, "::ffff:203.0.113.5")) == client_ip(_req(CADDY, "203.0.113.5"))
    assert client_ip(_req(None)) == "unknown"
    # '*' accepteras inte
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    assert client_ip(_req(CADDY, "203.0.113.5")) == "172.21.0.6"


def test_ipv6_keyed_on_slash64(monkeypatch):
    from memaix_gateway.safety.client_ip import client_ip

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", TRUSTED)
    a = client_ip(_req(CADDY, "2001:db8:1:2::1"))
    assert a == client_ip(_req(CADDY, "2001:db8:1:2:ffff::9"))
    assert a != client_ip(_req(CADDY, "2001:db8:1:3::1"))
    assert client_ip(_req(("2001:db8:1:2::5", 1))) == a  # peer utan proxy
    assert client_ip(_req(CADDY, "::ffff:203.0.113.5")) == "203.0.113.5"


@pytest.mark.parametrize("xff", ["fe80::1%eth0", "203.0.113.5:4711", "6.6.6.6,,", "203.0.113.5, ", ",203.0.113.5", "203.0.113.5, fe80::1%1"])
def test_malformed_chain_entries_fall_back_to_peer(monkeypatch, xff):
    from memaix_gateway.safety.client_ip import client_ip

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", TRUSTED)
    assert client_ip(_req(CADDY, xff)) == "172.21.0.6"
