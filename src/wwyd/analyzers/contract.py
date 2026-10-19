# SPDX-License-Identifier: AGPL-3.0-or-later
"""The analyzer output contract (SPEC §7.2), shared by builtin and LLM analyzers.

``{metric: {"YYYY-MM": {key: number}}}`` with finite numbers (never bool), keys of at most
64 characters, at most 20 metrics, at most 200 keys per metric per month and at most 1 MB
serialized. Error messages carry counts and limits only, never a key or a value: analyzer
output may echo episode text.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from wwyd.errors import Invalid

MAX_METRICS = 20
MAX_KEYS = 200  # per metric per month
MAX_KEY_LEN = 64
MAX_METRIC_LEN = 64
MAX_BYTES = 1_000_000

METRIC = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

Output = dict[str, dict[str, dict[str, float]]]


class ContractError(Invalid):
    """Analyzer output violates the contract; ``reason`` names the rule that failed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, reason=reason)
        self.reason = reason


def _number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def validate_output(output: Any) -> Output:
    """Return a normalised copy (values as float) or raise :class:`ContractError`."""
    if not isinstance(output, dict):
        raise ContractError("shape", "output must be an object of metrics")
    if len(output) > MAX_METRICS:
        raise ContractError("too_many_metrics", f"more than {MAX_METRICS} metrics")
    out: Output = {}
    for metric, months in output.items():
        if not isinstance(metric, str) or not METRIC.match(metric):
            raise ContractError("metric_name", "metric names must match [a-z][a-z0-9_]{0,63}")
        if not isinstance(months, dict):
            raise ContractError("shape", "each metric must map months to objects")
        out[metric] = {}
        for month, keys in months.items():
            if not isinstance(month, str) or not MONTH.match(month):
                raise ContractError("month", "months must look like YYYY-MM")
            if not isinstance(keys, dict):
                raise ContractError("shape", "each month must map keys to numbers")
            if len(keys) > MAX_KEYS:
                raise ContractError("too_many_keys", f"more than {MAX_KEYS} keys in a month")
            row: dict[str, float] = {}
            for key, value in keys.items():
                if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY_LEN:
                    raise ContractError("key_length", f"keys must be 1..{MAX_KEY_LEN} chars")
                if not _number(value):
                    raise ContractError("value", "values must be finite numbers")
                row[key] = float(value)
            out[metric][month] = row
    try:
        size = len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError):
        raise ContractError("shape", "output is not serializable") from None
    if size > MAX_BYTES:
        raise ContractError("too_large", f"output larger than {MAX_BYTES} bytes")
    return out
