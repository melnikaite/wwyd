# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM backend presets and the instance-wide default backend (SPEC §12)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

Preset = Literal["instance", "localai", "ollama", "lmstudio", "openrouter"]
PRESET_NAMES: tuple[str, ...] = ("instance", "localai", "ollama", "lmstudio", "openrouter")
LOCAL_PRESETS = frozenset({"localai", "ollama", "lmstudio"})


@dataclass(frozen=True)
class Backend:
    """A resolved backend. ``api_key`` is plaintext and lives only in memory."""

    preset: str
    base_url: str
    model: str | None
    api_key: str | None = None
    price_in_per_m: float | None = None
    price_out_per_m: float | None = None


# base_url and default model per preset; ``instance`` is resolved from the module setting.
PRESETS: dict[str, tuple[str, str | None]] = {
    "localai": ("http://127.0.0.1:1240/v1", "qwen3-vl-8b-instruct"),
    "ollama": ("http://127.0.0.1:11434/v1", None),
    "lmstudio": ("http://127.0.0.1:1234/v1", None),
    "openrouter": ("https://openrouter.ai/api/v1", None),
}

_DEFAULT_INSTANCE = Backend("instance", *PRESETS["localai"])
_instance: Backend = _DEFAULT_INSTANCE


def set_instance_backend(
    base_url: str | None = None,
    model: str | None = None,
    *,
    api_key: str | None = None,
    price_in_per_m: float | None = None,
    price_out_per_m: float | None = None,
) -> Backend:
    """Override the shared default used by personas without their own backend (startup).

    Arguments left as None keep the built-in LocalAI default; calling with no arguments
    restores it.
    """
    global _instance
    _instance = replace(
        _DEFAULT_INSTANCE,
        base_url=(base_url or _DEFAULT_INSTANCE.base_url).rstrip("/"),
        model=model or _DEFAULT_INSTANCE.model,
        api_key=api_key or None,
        price_in_per_m=price_in_per_m,
        price_out_per_m=price_out_per_m,
    )
    return _instance


def instance_backend() -> Backend:
    return _instance
