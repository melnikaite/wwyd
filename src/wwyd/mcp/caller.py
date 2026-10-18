# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is calling: the seam between transports/auth and tools.

stdio fixes one owner-scoped caller per process. In M2 the HTTP transport supplies a
provider that derives the caller from ``mcp.server.auth.middleware.auth_context
.get_access_token()`` (grant/API key lookup -> user, persona, scope, audiences).
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from wwyd.errors import Forbidden

Scope = Literal["owner", "read", "respond"]


@dataclass(frozen=True)
class Caller:
    user_id: str
    persona_id: str
    scope: Scope
    audiences: tuple[str, ...] | None = None  # respond scope: permitted audience names
    client_name: str | None = None


CallerProvider = Callable[[], Caller | None]

_current: ContextVar[Caller | None] = ContextVar("wwyd_caller", default=None)


def fixed_caller(caller: Caller) -> CallerProvider:
    """Provider for transports with one caller per process (stdio)."""
    return lambda: caller


def current_caller() -> Caller:
    """The caller of the tool call being executed (set by the server around each call)."""
    caller = _current.get()
    if caller is None:
        raise Forbidden("no authenticated caller")
    return caller


def bind_caller(caller: Caller):  # noqa: ANN201 - returns a contextvars.Token
    return _current.set(caller)


def unbind_caller(token) -> None:  # noqa: ANN001
    _current.reset(token)
