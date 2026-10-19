# SPDX-License-Identifier: AGPL-3.0-or-later
"""Built-in limit defaults (SPEC §11); config may override them via ``dataclasses.replace``."""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Per-user limits: effective = min(admin ceiling if set else default, user's own setting).
USER_LIMIT_KEYS: tuple[str, ...] = (
    "rpm_subject",
    "rpm_persona",
    "predict_per_day",
    "predict_per_audience_per_day",
    "llm_tokens_per_day",
    "ingest_bytes_per_day",
    "batch_records",
    "batch_bytes",
    "concurrent_jobs",
    "spend_cap_usd",
)
# Limits whose value is a float; the rest are integers.
FLOAT_LIMIT_KEYS = frozenset({"spend_cap_usd"})


@dataclass(frozen=True)
class LimitDefaults:
    rpm_subject: int = 60
    rpm_persona: int = 120
    predict_per_day: int = 500
    predict_per_audience_per_day: int = 150
    llm_tokens_per_day: int = 500_000
    ingest_bytes_per_day: int = 20 * 1024 * 1024
    batch_records: int = 1000
    batch_bytes: int = 2 * 1024 * 1024
    concurrent_jobs: int = 1
    # Per persona per calendar month (UTC); only backends that report or price cost count.
    # None means no cap.
    spend_cap_usd: float | None = 5.0
    # Instance-level only, never per user.
    llm_concurrency: int = 2
    llm_queue_timeout_s: float = 60.0
    llm_call_timeout_s: float = 120.0

    def user_limits(self) -> dict[str, int | float | None]:
        values = asdict(self)
        return {k: values[k] for k in USER_LIMIT_KEYS}


DEFAULTS = LimitDefaults()
