# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime configuration: built-in defaults -> ``$WWYD_DATA_DIR/wwyd.toml`` -> ``WWYD_*`` env."""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from wwyd.limits.defaults import DEFAULTS, LimitDefaults

DB_FILENAME = "wwyd.db"
CONFIG_FILENAME = "wwyd.toml"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8740
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class ConfigError(Exception):
    """The configuration cannot be used; the message is meant for the operator."""


def data_dir() -> Path:
    """Return the data directory (``WWYD_DATA_DIR`` or ``~/.wwyd``), creating it if needed."""
    raw = os.environ.get("WWYD_DATA_DIR")
    path = Path(raw).expanduser() if raw else Path.home() / ".wwyd"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / DB_FILENAME


@dataclass(frozen=True)
class LlmSettings:
    """Instance-wide default LLM backend (``[llm]`` table, ``WWYD_LLM_<NAME>``).

    Empty values keep the built-in LocalAI default. ``api_key`` is never logged or shown.
    """

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = field(default=None, repr=False)
    price_in_per_m: float | None = None
    price_out_per_m: float | None = None
    # May a persona's own backend point at loopback/private/link-local addresses? True suits
    # a self-hosted instance (LocalAI on localhost); set false on multi-user hosted instances.
    allow_private_base_urls: bool = True

    def apply(self) -> None:
        """Install these settings as the instance backend (``wwyd.llm.presets``)."""
        from wwyd.llm.presets import set_instance_backend

        set_instance_backend(
            self.base_url,
            self.model,
            api_key=self.api_key,
            price_in_per_m=self.price_in_per_m,
            price_out_per_m=self.price_out_per_m,
            allow_private_base_urls=self.allow_private_base_urls,
        )


@dataclass(frozen=True)
class Thresholds:
    """Instance thresholds read by ingest, analyzers and compaction (SPEC §7.4, §10.6)."""

    analyzer_min_episodes: int = 100
    split_threshold: int = 20


_thresholds = Thresholds()


def configure_thresholds(value: Thresholds) -> None:
    """Install the instance thresholds (from :class:`Config`) for :func:`thresholds`."""
    global _thresholds
    _thresholds = value


def thresholds() -> Thresholds:
    return _thresholds


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    public_url: str = ""  # empty: http://127.0.0.1:<port>
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    smtp_from: str | None = None
    # Peers whose X-Forwarded-For header is trusted for the client IP (e.g. a local Caddy).
    # Empty (default): the socket peer address is always the client IP.
    trusted_proxies: list[str] = field(default_factory=list)
    # Instance limit defaults (SPEC §11): the ``[limits]`` table and ``WWYD_LIMIT_<NAME>``.
    limits: LimitDefaults = DEFAULTS
    # Instance default LLM backend (SPEC §12): the ``[llm]`` table and ``WWYD_LLM_<NAME>``.
    llm: LlmSettings = field(default_factory=LlmSettings)
    # ``[auth] allow_insecure_redirects`` / ``WWYD_AUTH_ALLOW_INSECURE_REDIRECTS`` (SPEC §9.1):
    # accept plain-http redirect URIs on non-loopback hosts (LAN deployments).
    allow_insecure_redirects: bool = True
    # Instance thresholds, not limits (SPEC §11): ``analyzer_min_episodes`` /
    # ``WWYD_ANALYZER_MIN_EPISODES`` and ``split_threshold`` / ``WWYD_SPLIT_THRESHOLD``.
    analyzer_min_episodes: int = 100
    split_threshold: int = 20

    def __post_init__(self) -> None:
        for name in ("analyzer_min_episodes", "split_threshold"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be at least 1")
        if not isinstance(self.allow_insecure_redirects, bool):
            self.allow_insecure_redirects = _as_bool(
                self.allow_insecure_redirects, "[auth] allow_insecure_redirects"
            )
        if isinstance(self.trusted_proxies, str):
            self.trusted_proxies = [p.strip() for p in self.trusted_proxies.split(",") if p.strip()]
        self.public_url = (self.public_url or f"http://127.0.0.1:{self.port}").rstrip("/")
        parts = urlsplit(self.public_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError(
                f"public_url must be an absolute http(s) URL, got {self.public_url!r}"
            )
        if parts.query or parts.fragment:
            raise ConfigError("public_url must not have a query string or fragment")
        if parts.scheme != "https" and not self.is_local:
            raise ConfigError(
                "public_url must use https unless it points at localhost/127.0.0.1 "
                "(OAuth requires an https issuer). Put wwyd behind a TLS proxy and set "
                "public_url = 'https://your.domain'."
            )

    @property
    def thresholds(self) -> Thresholds:
        return Thresholds(self.analyzer_min_episodes, self.split_threshold)

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).hostname or ""

    @property
    def is_local(self) -> bool:
        return self.public_host in LOCAL_HOSTS

    @property
    def secure_cookies(self) -> bool:
        return not (urlsplit(self.public_url).scheme == "http" and self.is_local)

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def mcp_url(self) -> str:
        return self.public_url + "/mcp"


_INT_KEYS = {"port", "smtp_port", "analyzer_min_episodes", "split_threshold"}
_LIMIT_FIELDS = {f.name: f for f in fields(LimitDefaults)}
_NONE_WORDS = {"none", "off", "unlimited"}


def _limit_value(name: str, raw: Any, source: str) -> int | float | None:
    """Coerce one limit from TOML (typed) or env (string); raise ConfigError when invalid."""
    where = f"limit {name} ({source})"
    nullable = name == "spend_cap_usd"
    is_float = name in ("spend_cap_usd", "llm_queue_timeout_s", "llm_call_timeout_s")
    if isinstance(raw, str):
        text = raw.strip().lower()
        if nullable and text in _NONE_WORDS:
            return None
        try:
            raw = float(text) if is_float else int(text)
        except ValueError:
            kind = "a number" if is_float else "an integer"
            extra = ' or "none"' if nullable else ""
            raise ConfigError(f"{where} must be {kind}{extra}, got {raw!r}") from None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ConfigError(f"{where} must be a number, got {type(raw).__name__}")
    if not is_float and raw != int(raw):
        raise ConfigError(f"{where} must be an integer, got {raw!r}")
    if not math.isfinite(raw) or raw < 0:
        raise ConfigError(f"{where} must be a finite number >= 0, got {raw!r}")
    if name in ("llm_concurrency", "sandbox_concurrency") and raw < 1:
        raise ConfigError(f"{where} must be at least 1")
    return float(raw) if is_float else int(raw)


def load_limits(table: Any = None, environ: dict[str, str] | None = None) -> LimitDefaults:
    """``DEFAULTS`` with the ``[limits]`` TOML table and ``WWYD_LIMIT_<NAME>`` env applied."""
    env = os.environ if environ is None else environ
    changes: dict[str, Any] = {}
    if table is not None:
        if not isinstance(table, dict):
            raise ConfigError("[limits] in wwyd.toml must be a table")
        for name, raw in table.items():
            if name not in _LIMIT_FIELDS:
                known = ", ".join(sorted(_LIMIT_FIELDS))
                raise ConfigError(f"unknown limit {name!r} in [limits]; known: {known}")
            changes[name] = _limit_value(name, raw, "wwyd.toml")
    for name in _LIMIT_FIELDS:
        var = f"WWYD_LIMIT_{name.upper()}"
        raw = env.get(var)
        if raw is not None and raw != "":
            changes[name] = _limit_value(name, raw, var)
    return replace(DEFAULTS, **changes) if changes else DEFAULTS


_LLM_FIELDS = {f.name for f in fields(LlmSettings)}
_LLM_PRICES = {"price_in_per_m", "price_out_per_m"}
_LLM_BOOLS = {"allow_private_base_urls"}


def load_llm(table: Any = None, environ: dict[str, str] | None = None) -> LlmSettings:
    """``LlmSettings`` from the ``[llm]`` TOML table with ``WWYD_LLM_<NAME>`` env applied."""
    env = os.environ if environ is None else environ
    values: dict[str, Any] = {}
    if table is not None:
        if not isinstance(table, dict):
            raise ConfigError("[llm] in wwyd.toml must be a table")
        for name, raw in table.items():
            if name not in _LLM_FIELDS:
                known = ", ".join(sorted(_LLM_FIELDS))
                raise ConfigError(f"unknown key {name!r} in [llm]; known: {known}")
            values[name] = raw
    for name in _LLM_FIELDS:
        raw = env.get(f"WWYD_LLM_{name.upper()}")
        if raw is not None and raw != "":
            values[name] = raw
    for name, raw in list(values.items()):
        if name in _LLM_BOOLS:
            if isinstance(raw, bool):
                continue
            text = str(raw).strip().lower() if isinstance(raw, str) else None
            if text in ("1", "true", "yes", "on"):
                values[name] = True
            elif text in ("0", "false", "no", "off"):
                values[name] = False
            else:
                raise ConfigError(f"[llm] {name} must be true or false")
        elif name in _LLM_PRICES:
            try:
                price = float(raw)
            except (TypeError, ValueError):
                raise ConfigError(f"[llm] {name} must be a number") from None
            if isinstance(raw, bool) or not math.isfinite(price) or price < 0:
                raise ConfigError(f"[llm] {name} must be a finite number >= 0")
            values[name] = price
        elif not isinstance(raw, str):
            raise ConfigError(f"[llm] {name} must be a string")
        else:
            values[name] = raw.strip() or None
    base_url = values.get("base_url")
    if base_url and urlsplit(base_url).scheme not in ("http", "https"):
        raise ConfigError("[llm] base_url must be an absolute http(s) URL")
    return LlmSettings(**values)


def _as_bool(raw: Any, where: str) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower() if isinstance(raw, str) else None
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{where} must be true or false")


_AUTH_FIELDS = {"allow_insecure_redirects"}


def load_auth(table: Any = None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Config values from the ``[auth]`` TOML table with ``WWYD_AUTH_<NAME>`` env applied."""
    env = os.environ if environ is None else environ
    values: dict[str, Any] = {}
    if table is not None:
        if not isinstance(table, dict):
            raise ConfigError("[auth] in wwyd.toml must be a table")
        for name, raw in table.items():
            if name not in _AUTH_FIELDS:
                known = ", ".join(sorted(_AUTH_FIELDS))
                raise ConfigError(f"unknown key {name!r} in [auth]; known: {known}")
            values[name] = raw
    for name in _AUTH_FIELDS:
        raw = env.get(f"WWYD_AUTH_{name.upper()}")
        if raw is not None and raw != "":
            values[name] = raw
    return {k: _as_bool(v, f"[auth] {k}") for k, v in values.items()}


def load_config(**overrides: Any) -> Config:
    """Merge defaults, ``wwyd.toml`` in the data dir, ``WWYD_*`` env vars and ``overrides``."""
    names = {f.name for f in fields(Config)} - {"limits", "llm"} - _AUTH_FIELDS
    values: dict[str, Any] = {}
    limits_table: Any = None
    llm_table: Any = None
    auth_table: Any = None
    toml_path = data_dir() / CONFIG_FILENAME
    if toml_path.exists():
        try:
            loaded = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{toml_path} is not valid TOML: {exc}") from None
        values.update({k: v for k, v in loaded.items() if k in names})
        limits_table = loaded.get("limits")
        llm_table = loaded.get("llm")
        auth_table = loaded.get("auth")
    for name in names:
        raw = os.environ.get(f"WWYD_{name.upper()}")
        if raw is not None and raw != "":
            values[name] = raw
    values.update(load_auth(auth_table))
    values.update({k: v for k, v in overrides.items() if v is not None})
    for key in _INT_KEYS & values.keys():
        try:
            values[key] = int(values[key])
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be an integer") from None
    values.setdefault("limits", load_limits(limits_table))
    values.setdefault("llm", load_llm(llm_table))
    return Config(**values)
