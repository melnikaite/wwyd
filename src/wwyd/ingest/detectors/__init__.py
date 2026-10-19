# SPDX-License-Identifier: AGPL-3.0-or-later
"""Habit detectors (SPEC §7.2): pure functions from a parsed batch to keyed statistics."""

from __future__ import annotations

from datetime import tzinfo

from wwyd.ingest.detectors import calendar, messages
from wwyd.ingest.detectors.common import Finding, Rendered, Renderer, Stats
from wwyd.ingest.records import Batch

__all__ = ["Finding", "Rendered", "collect_all", "render", "renderer_for"]


def collect_all(batch: Batch, tz: tzinfo) -> list[Finding]:
    return calendar.collect(batch, tz) + messages.collect(batch, tz)


def renderer_for(key: str) -> Renderer | None:
    if key in calendar.RENDERERS:
        return calendar.RENDERERS[key]
    if key == "habit:active_hours":
        return messages.render_active_hours
    prefix, _, audience = key.rpartition(":")
    if prefix == "habit:reply_latency":
        return messages.render_reply_latency(audience)
    if prefix == "habit:message_length":
        return messages.render_message_length(audience)
    return None


def render(key: str, stats: Stats, period_days: int) -> Rendered | None:
    fn = renderer_for(key)
    if fn is None:
        return None
    try:
        return fn(stats, period_days)
    except (KeyError, TypeError, ZeroDivisionError):
        return None  # stats of an older shape; the next run replaces them
