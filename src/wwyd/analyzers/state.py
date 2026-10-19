# SPDX-License-Identifier: AGPL-3.0-or-later
"""``analyzer_state`` (SPEC §4.6, §7.2): counters merged by addition, read back per metric."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from wwyd.analyzers.contract import Output

# metric -> month -> key -> value
State = dict[str, dict[str, dict[str, float]]]


def merge_state(conn: sqlite3.Connection, analyzer_id: str, output: Output) -> int:
    """Add a validated, filtered output to the stored state; returns the rows written."""
    rows = [
        (analyzer_id, month, metric, key, value)
        for metric, months in output.items()
        for month, keys in months.items()
        for key, value in keys.items()
    ]
    conn.executemany(
        "INSERT INTO analyzer_state(analyzer_id, month, metric, key, value)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(analyzer_id, month, metric, key)"
        " DO UPDATE SET value = value + excluded.value",
        rows,
    )
    return len(rows)


def read_state(conn: sqlite3.Connection, analyzer_id: str) -> State:
    state: State = defaultdict(lambda: defaultdict(dict))
    for r in conn.execute(
        "SELECT month, metric, key, value FROM analyzer_state WHERE analyzer_id = ?"
        " ORDER BY metric, month, key",
        (analyzer_id,),
    ):
        state[r["metric"]][r["month"]][r["key"]] = r["value"]
    return {m: dict(v) for m, v in state.items()}


def totals(state: State, metric: str) -> dict[str, float]:
    """A metric summed over all months: key -> value."""
    out: dict[str, float] = defaultdict(float)
    for keys in state.get(metric, {}).values():
        for key, value in keys.items():
            out[key] += value
    return dict(out)


def summary(conn: sqlite3.Connection, analyzer_id: str) -> dict[str, Any]:
    """State summary for ``get_analyzer``: metrics, months, key count and keys per metric."""
    state = read_state(conn, analyzer_id)
    months = sorted({m for per in state.values() for m in per})
    metrics = []
    for metric in sorted(state):
        keys = sorted({k for per in state[metric].values() for k in per})
        metrics.append(
            {
                "metric": metric,
                "months": sorted(state[metric]),
                "key_count": len(keys),
                "keys": keys,
            }
        )
    return {"months": months, "metrics": metrics}
