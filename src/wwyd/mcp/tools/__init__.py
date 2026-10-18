# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP tool groups. Each module exposes ``register(server)``; tools are thin wrappers over
``wwyd.store`` and declare the scopes that may see and call them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from wwyd.mcp.tools import audiences, categories, claims, persona

if TYPE_CHECKING:
    from wwyd.mcp.server import WwydServer


def register_all(server: WwydServer) -> None:
    for module in (claims, audiences, categories, persona):
        module.register(server)
