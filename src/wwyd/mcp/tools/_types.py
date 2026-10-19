# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded parameter types shared by the tool schemas (limits mirror the store's checks)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from wwyd.store.claims import MAX_IDS, MAX_QUERY
from wwyd.store.common import MAX_DESCRIPTION, MAX_TEXT

Id = Annotated[str, Field(max_length=100)]
Name = Annotated[str, Field(max_length=100)]
Path = Annotated[str, Field(max_length=200)]
Cursor = Annotated[str, Field(max_length=500)]
Short = Annotated[str, Field(max_length=500)]
ClaimText = Annotated[str, Field(max_length=MAX_TEXT)]
Description = Annotated[str, Field(max_length=MAX_DESCRIPTION)]
Query = Annotated[str, Field(max_length=MAX_QUERY)]
Ids = Annotated[list[Id], Field(max_length=MAX_IDS)]
Names = Annotated[list[Name], Field(max_length=MAX_IDS)]
Paths = Annotated[list[Path], Field(max_length=MAX_IDS)]
