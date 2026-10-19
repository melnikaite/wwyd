# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory, process-local sliding-window counters for the auth surface.

Each :class:`Throttle` counts events per key (an IP, a login, a client_id, ``*`` for a
global cap) within ``window`` seconds. ``limit`` events block the key for ``lockout``
seconds (default: until the oldest event leaves the window). Only limit names and IPs are
ever logged, never submitted secrets. Time comes from :func:`wwyd.auth.clock.mono`, so tests
can move it.
"""

from __future__ import annotations

import ipaddress
import logging
import math
from collections import deque
from dataclasses import dataclass, field

from wwyd.auth import clock

log = logging.getLogger("wwyd.auth.throttle")

_MAX_KEYS = 100_000


class Throttled(Exception):
    def __init__(self, name: str, retry_after: int) -> None:
        super().__init__(name)
        self.name = name
        self.retry_after = max(1, retry_after)


@dataclass
class Throttle:
    name: str
    limit: int
    window: float
    lockout: float | None = None
    _events: dict[str, deque[float]] = field(default_factory=dict, repr=False)
    _locked: dict[str, float] = field(default_factory=dict, repr=False)

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may try again; 0 when not blocked."""
        t = clock.mono()
        until = self._locked.get(key)
        if until is not None:
            if until > t:
                return math.ceil(until - t)
            del self._locked[key]
        events = self._prune(key, t)
        if events is not None and len(events) >= self.limit:
            return math.ceil(events[0] + self.window - t)
        return 0

    def check(self, key: str) -> None:
        wait = self.retry_after(key)
        if wait:
            raise Throttled(self.name, wait)

    def hit(self, key: str) -> None:
        """Record one event; lock the key when it reaches the limit."""
        t = clock.mono()
        if len(self._events) > _MAX_KEYS:
            self._gc(t)
        events = self._events.setdefault(key, deque())
        events.append(t)
        self._prune(key, t)
        if len(events) >= self.limit:
            if self.lockout:
                self._locked[key] = t + self.lockout
            log.warning("throttle %s reached for %s", self.name, _loggable(key))

    def take(self, key: str) -> None:
        """Check, then count (for plain request rates)."""
        self.check(key)
        self.hit(key)

    def reset(self, key: str) -> None:
        self._events.pop(key, None)
        self._locked.pop(key, None)

    def clear(self) -> None:
        self._events.clear()
        self._locked.clear()

    def _prune(self, key: str, t: float) -> deque[float] | None:
        events = self._events.get(key)
        if events is None:
            return None
        while events and events[0] <= t - self.window:
            events.popleft()
        if not events:
            del self._events[key]
            return None
        return events

    def _gc(self, t: float) -> None:
        for key in list(self._events):
            self._prune(key, t)
        for key, until in list(self._locked.items()):
            if until <= t:
                del self._locked[key]


def _loggable(key: str) -> str:
    """IPs and the global key are logged; anything else (logins, client ids) is not."""
    try:
        ipaddress.ip_address(key)
        return key
    except ValueError:
        return "*" if key == "*" else "(key)"


class Throttles:
    """All auth throttles of one app instance."""

    def __init__(self) -> None:
        m, h = 60.0, 3600.0
        self.login_ip = Throttle("login_ip", 10, 15 * m)
        self.login_user = Throttle("login_user", 10, 15 * m, lockout=15 * m)
        self.password_change = Throttle("password_change", 5, 15 * m, lockout=15 * m)
        self.link_ip = Throttle("link_ip", 10, 15 * m)
        self.reset_request_ip = Throttle("reset_request_ip", 5, h)
        self.token_ip = Throttle("token_ip", 20, 10 * m)
        self.token_client = Throttle("token_client", 20, 10 * m)
        self.register_ip = Throttle("register_ip", 20, h)
        self.register_global = Throttle("register_global", 500, h)
        self.authorize_ip = Throttle("authorize_ip", 60, m)
        self.cimd_host = Throttle("cimd_host", 10, m)
        self.cimd_global = Throttle("cimd_global", 60, m)
        self.bearer_ip = Throttle("bearer_ip", 30, 10 * m)

    def all(self) -> list[Throttle]:
        return [v for v in vars(self).values() if isinstance(v, Throttle)]

    def clear(self) -> None:
        for t in self.all():
            t.clear()


def client_ip(peer: str | None, forwarded_for: str | None, trusted: list[str]) -> str:
    """The socket peer, or the right-most untrusted ``X-Forwarded-For`` hop when the peer is
    a trusted proxy."""
    peer = peer or "unknown"
    if not forwarded_for or not _in(peer, trusted):
        return peer
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _in(hop, trusted):
            return hop
    return hops[0] if hops else peer


def _in(addr: str, networks: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for net in networks:
        try:
            if ip in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False
