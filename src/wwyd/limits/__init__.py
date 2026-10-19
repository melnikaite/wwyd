# SPDX-License-Identifier: AGPL-3.0-or-later
"""Limits and usage (SPEC §11). Use :func:`get_limiter` so server and tools share state."""

from __future__ import annotations

import sqlite3

from wwyd.limits.defaults import DEFAULTS, USER_LIMIT_KEYS, LimitDefaults
from wwyd.limits.gate import LLMGate
from wwyd.limits.limiter import Limiter, estimate_cost

__all__ = [
    "DEFAULTS",
    "USER_LIMIT_KEYS",
    "LLMGate",
    "LimitDefaults",
    "Limiter",
    "configure",
    "drop_limiter",
    "estimate_cost",
    "get_gate",
    "get_limiter",
]

# sqlite3.Connection does not support weak references; keyed by id() and holding the conn
# so the id cannot be reused while cached. Call drop_limiter() when closing a connection.
_limiters: dict[int, tuple[sqlite3.Connection, Limiter]] = {}
_gate: LLMGate | None = None
_defaults: LimitDefaults = DEFAULTS


def configure(defaults: LimitDefaults) -> None:
    """Install the instance limit defaults (from config) for :func:`get_limiter` and
    :func:`get_gate`. Existing limiters switch to them; the gate is rebuilt on next use."""
    global _defaults, _gate
    _defaults = defaults
    for _, limiter in _limiters.values():
        limiter.defaults = defaults
    _gate = None


def get_limiter(conn: sqlite3.Connection, defaults: LimitDefaults | None = None) -> Limiter:
    """The shared :class:`Limiter` for ``conn``; ``defaults`` applies only on first creation."""
    entry = _limiters.get(id(conn))
    if entry is None or entry[0] is not conn:
        entry = (conn, Limiter(conn, defaults or _defaults))
        _limiters[id(conn)] = entry
    return entry[1]


def drop_limiter(conn: sqlite3.Connection) -> None:
    _limiters.pop(id(conn), None)


def get_gate(defaults: LimitDefaults | None = None) -> LLMGate:
    """The process-wide :class:`LLMGate`; ``defaults`` applies only on first creation."""
    global _gate
    if _gate is None:
        d = defaults or _defaults
        _gate = LLMGate(d.llm_concurrency, d.llm_queue_timeout_s, d.llm_call_timeout_s)
    return _gate
