# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry of builtin analyzers (SPEC §7.2), found by ``name``."""

from __future__ import annotations

from wwyd.analyzers.builtin import stylometry, time_activity, time_duration, time_gaps
from wwyd.analyzers.builtin.base import Builtin, ClaimSpec, Context, Item, host_fields

BUILTINS: dict[str, Builtin] = {
    b.name: b
    for b in (
        time_activity.BUILTIN,
        time_duration.BUILTIN,
        time_gaps.BUILTIN,
        stylometry.BUILTIN,
    )
}

__all__ = ["BUILTINS", "Builtin", "ClaimSpec", "Context", "Item", "host_fields"]
