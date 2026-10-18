# SPDX-License-Identifier: AGPL-3.0-or-later
"""Domain errors with stable codes, surfaced to MCP clients as tool errors.

Messages must never echo caller-supplied free text (claim text, queries, documents):
they reach logs in some code paths. IDs, names, paths and counts are fine.
"""

from __future__ import annotations

from typing import Any, Literal

ErrorCode = Literal["not_found", "forbidden", "invalid", "conflict", "too_large"]


class WwydError(Exception):
    code: ErrorCode = "invalid"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


class NotFound(WwydError):
    code = "not_found"


class Forbidden(WwydError):
    code = "forbidden"


class Invalid(WwydError):
    code = "invalid"


class Conflict(WwydError):
    code = "conflict"


class TooLarge(WwydError):
    code = "too_large"
