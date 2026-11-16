"""Private-address refusal (README 1, "Private addresses").

An address is refused unless it is globally routable: ``ipaddress``'s ``is_global`` plus
an explicit list of special ranges that ``is_global`` lets through or that embed an IPv4
address (NAT64, 6to4, Teredo, IPv4-mapped/compatible). A host that does not resolve is
refused too (it cannot be checked). Resolutions are cached for five minutes, so a job
re-checks a host it keeps fetching instead of trusting the first answer for its whole life.

``guarded_transport()`` makes the check at connect time for the one-off HTTP fetches the
service makes itself without a proxy (analysis, samples, robots.txt, sitemaps): it resolves
the host, refuses when any address is not public, and connects to the checked address, so a
DNS answer cannot change between the check and the connection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Any

from app.settings import get_settings

_cache: dict[str, tuple[float, bool | None]] = {}
_TTL = 300.0

# ranges refused although ``is_global`` may call them global, or that need an explicit look
_EXTRA_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
)]
_EXTRA_V6 = [ipaddress.ip_network(n) for n in (
    "::/128", "::1/128", "64:ff9b:1::/48", "100::/64", "2001:db8::/32", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
)]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class PrivateAddressError(OSError):
    pass


def ip_is_refused(addr: str) -> bool:
    """True when the address is not a public unicast address."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = None
        if ip.ipv4_mapped is not None:
            embedded = ip.ipv4_mapped
        elif ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None:
            embedded = ip.sixtofour
        elif ip.teredo is not None:
            server, client = ip.teredo
            if ip_is_refused(str(server)) or ip_is_refused(str(client)):
                return True
        elif int(ip) >> 32 == 0 and int(ip) > 1:  # deprecated IPv4-compatible ::a.b.c.d
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return ip_is_refused(str(embedded))
        if any(ip in net for net in _EXTRA_V6):
            return True
    elif any(ip in net for net in _EXTRA_V4):
        return True
    return not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback or ip.is_link_local


# the old name
_is_private = ip_is_refused


def _denied(host: str) -> bool:
    denied = get_settings().denied_target_hosts
    return bool(denied) and host.lower().rstrip(".") in denied


async def resolve(host: str, port: int | None = None) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


async def host_is_private(host: str) -> bool | None:
    """True = refused; False = public; None = does not resolve."""
    host = host.strip("[]")
    if _denied(host):
        return True
    if get_settings().allow_private_targets:
        return False
    cached = _cache.get(host)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        result: bool | None = ip_is_refused(host)
    except ValueError:
        try:
            addrs = await resolve(host)
        except (socket.gaierror, UnicodeError, OSError):
            result = None
        else:
            result = any(ip_is_refused(a) for a in addrs) if addrs else None
    _cache[host] = (time.monotonic() + _TTL, result)
    return result


async def url_is_private(url: str) -> bool:
    """True when the URL's host must not be fetched: private, special, denied or unresolvable."""
    from app.core.urls import host_of

    try:
        host = host_of(url)
    except ValueError:
        return True
    result = await host_is_private(host)
    return result is None or bool(result)


def _guarded_backend_class():
    import httpcore
    from httpcore._backends.auto import AutoBackend

    class GuardedNetworkBackend(AutoBackend):
        """Resolves, checks and connects to the checked address (no second resolution)."""

        async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                              socket_options: Any = None):
            if _denied(host):
                raise PrivateAddressError(f"refused target host {host}")
            if get_settings().allow_private_targets:
                return await super().connect_tcp(host, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            try:
                addrs = await resolve(host, port)
            except (socket.gaierror, UnicodeError, OSError) as exc:
                raise httpcore.ConnectError(f"cannot resolve {host}") from exc
            if not addrs or any(ip_is_refused(a) for a in addrs):
                raise PrivateAddressError(f"{host} resolves to a refused address")
            last: Exception | None = None
            for addr in addrs:
                try:
                    return await super().connect_tcp(addr, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
                except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                    last = exc
            raise httpcore.ConnectError(f"cannot connect to {host}") from last

    return GuardedNetworkBackend


def guarded_transport(**kwargs: Any):
    """An httpx transport whose connections go only to checked public addresses."""
    import httpx

    transport = httpx.AsyncHTTPTransport(**kwargs)
    transport._pool._network_backend = _guarded_backend_class()()  # httpcore pool internals (httpx 0.28)
    return transport
