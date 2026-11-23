# SPDX-License-Identifier: AGPL-3.0-or-later
"""Klientens IP bakom reverse proxy (Cloudflare -> cloudflared -> Caddy -> gateway).

En enda sanning för "vem är klienten" för rate limiting m.m. Fail closed:

* X-Forwarded-For läses bara när TCP-peern ligger i FORWARDED_ALLOW_IPS
  (kommaseparerade IP/CIDR). Saknas variabeln är ingen proxy betrodd och
  peer-adressen används alltid. "*" godtas inte (skulle låta vem som helst
  spoofa); det behandlas som tom lista.
* Kedjan läses från höger: första adressen som INTE är en betrodd proxy är
  klienten. Vänstra delen är klientstyrd och ignoreras.
* Poster med scope-id (%), portar eller tomma poster räknas som trasiga.
* Trasig/tom header eller icke-IP-värde ger peer-adressen, aldrig en ny hink.
* IPv6 nycklas på sitt /64-nät (nätadressen returneras); IPv4 oförändrat.
* IPv4-mappade IPv6-adresser (::ffff:a.b.c.d) normaliseras till IPv4 så att
  hinknyckeln blir densamma.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import os

logger = logging.getLogger(__name__)

ENV_VAR = "FORWARDED_ALLOW_IPS"

_IP = ipaddress.IPv4Address | ipaddress.IPv6Address


def _normalize(addr: str) -> _IP | None:
    addr = addr.strip().strip("[]")
    if "%" in addr:  # scope-id (fe80::1%eth0): aldrig en giltig klientadress
        return None
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


@functools.lru_cache(maxsize=8)
def _parse_trusted(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            logger.warning("%s='*' is refused (would trust any source); ignoring entry", ENV_VAR)
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("%s: ignoring invalid entry %r", ENV_VAR, part)
    return tuple(nets)


def _key(ip: _IP) -> str:
    """Hinknyckel: IPv4 som är, IPv6 som sitt /64-nät (annars kan en klient rotera inom sitt prefix)."""
    if isinstance(ip, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    return _key(ip)


def _trusted(ip: _IP, nets) -> bool:
    return any(ip.version == n.version and ip in n for n in nets)


def client_ip(request) -> str:
    """Normalized client address for *request*, or "unknown" without a peer."""
    peer_raw = request.client.host if request.client else ""
    if not peer_raw:
        return "unknown"
    peer = _normalize(peer_raw)
    if peer is None:
        return peer_raw  # t.ex. unix socket/testklient; aldrig betrodd
    nets = _parse_trusted(os.environ.get(ENV_VAR, ""))
    if not nets or not _trusted(peer, nets):
        return _key(peer)
    header = request.headers.get("x-forwarded-for", "")
    if not header.strip():
        return _key(peer)
    for part in reversed(header.split(",")):
        ip = _normalize(part)
        if ip is None:
            return _key(peer)  # trasig kedja: lita inte på något i den
        if not _trusted(ip, nets):
            return _key(ip)
    return _key(peer)  # hela kedjan är betrodd (intern trafik)
