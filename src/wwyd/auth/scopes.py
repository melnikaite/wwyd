# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scope strings: ``owner``, ``read``, ``respond:<a>,<b>`` (audience names, no spaces).

A grant or key carries exactly one scope. ``respond:`` is parametrised, so it cannot be
enumerated in ``scopes_supported`` or in a DCR client's registered ``scope``; we therefore
validate scopes ourselves (see :class:`wwyd.auth.provider.WwydClient`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from wwyd.store.common import AUDIENCE_NAME

Kind = Literal["owner", "read", "respond"]
KINDS: tuple[Kind, ...] = ("owner", "read", "respond")

DESCRIPTIONS: dict[Kind, tuple[str, str]] = {
    "owner": ("Full access to your persona", "Read and change everything, like you can."),
    "read": (
        "Read your persona to personalise this app",
        "Sees your approved claims, including private ones. Cannot change anything.",
    ),
    "respond": (
        "Answer on your behalf for selected groups",
        "Can only ask what you would do, for the groups you tick, using what those groups "
        "may see. Cannot read or change your persona.",
    ),
}


@dataclass(frozen=True)
class Scope:
    kind: Kind
    audiences: tuple[str, ...] = ()

    def __str__(self) -> str:
        if self.kind == "respond":
            return "respond:" + ",".join(self.audiences)
        return self.kind

    @property
    def label(self) -> str:
        if self.kind == "respond":
            return "Answer for " + ", ".join(self.audiences)
        return {"owner": "Full access", "read": "Read only"}[self.kind]


def parse_scope(value: str | None) -> Scope | None:
    """Parse one scope string; ``None`` if it is not a valid wwyd scope."""
    if not value:
        return None
    if value in ("owner", "read"):
        return Scope(value)  # type: ignore[arg-type]
    if value.startswith("respond:"):
        names = value[len("respond:") :].split(",")
        # "default" is an alias resolved per call (SPEC §4.3); scopes must name real audiences.
        if names and all(AUDIENCE_NAME.match(n) and n != "default" for n in names):
            return Scope("respond", tuple(sorted(set(names))))
    return None


def pick_requested(scopes: list[str] | None) -> Scope | None:
    """The first valid wwyd scope among the requested ones (unknown ones are ignored)."""
    for value in scopes or ():
        parsed = parse_scope(value)
        if parsed is not None:
            return parsed
    return None


def make_scope(kind: str, audiences: list[str] | tuple[str, ...] = ()) -> Scope | None:
    if kind == "respond":
        return parse_scope("respond:" + ",".join(audiences)) if audiences else None
    return parse_scope(kind)
