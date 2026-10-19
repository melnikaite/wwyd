# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outbound-request guard against SSRF (CIMD fetches, per-persona LLM backends).

Resolve the host once, refuse anything that is not a public unicast address, then connect to
the validated IP literal while keeping the hostname in ``Host`` and TLS SNI (no DNS
rebinding between the check and the connection).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit

import anyio

Resolver = Callable[[str, int], Awaitable[list[str]]]
SyncResolver = Callable[[str, int], list[str]]


class BlockedAddress(Exception):
    """The URL's host does not resolve, or resolves to a non-public address."""


def blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def pinned_url(parts: SplitResult, addr: str, scheme: str = "https") -> str:
    """``parts`` with the host replaced by the IP literal ``addr`` (bracketed for IPv6)."""
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    literal = f"[{ip}]" if ip.version == 6 else str(ip)
    netloc = f"{literal}:{parts.port}" if parts.port else literal
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def resolve_sync(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def default_resolver(host: str, port: int) -> list[str]:
    return await anyio.to_thread.run_sync(lambda: resolve_sync(host, port))


@dataclass(frozen=True)
class PinnedRequest:
    """Where to actually connect: URL with an IP literal plus Host header / SNI extension."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    extensions: dict[str, str] = field(default_factory=dict)


def _pin(parts: SplitResult, addrs: list[str]) -> PinnedRequest:
    if not addrs or any(blocked_ip(a) for a in addrs):
        raise BlockedAddress("host resolves to a non-public address")
    scheme = parts.scheme
    ext = {"sni_hostname": parts.hostname or ""} if scheme == "https" else {}
    return PinnedRequest(pinned_url(parts, addrs[0], scheme), {"Host": parts.netloc}, ext)


def _parts(url: str) -> SplitResult:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedAddress("not an http(s) URL")
    return parts


def _port(parts: SplitResult) -> int:
    return parts.port or (443 if parts.scheme == "https" else 80)


def pin_public_sync(url: str, resolver: SyncResolver | None = None) -> PinnedRequest:
    """Resolve ``url``'s host and pin it; raises :class:`BlockedAddress` if not public."""
    parts = _parts(url)
    try:
        addrs = (resolver or resolve_sync)(parts.hostname or "", _port(parts))
    except OSError:
        raise BlockedAddress("host did not resolve") from None
    return _pin(parts, addrs)


async def pin_public(url: str, resolver: Resolver | None = None) -> PinnedRequest:
    """Async :func:`pin_public_sync`."""
    parts = _parts(url)
    try:
        addrs = await (resolver or default_resolver)(parts.hostname or "", _port(parts))
    except OSError:
        raise BlockedAddress("host did not resolve") from None
    return _pin(parts, addrs)
