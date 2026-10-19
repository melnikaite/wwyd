# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import pytest

from wwyd.app import transport_security
from wwyd.config import Config, ConfigError, load_config


def test_defaults(data_dir):
    config = load_config()
    assert (config.host, config.port, config.public_url) == (
        "127.0.0.1",
        8740,
        "http://127.0.0.1:8740",
    )
    assert config.mcp_url == "http://127.0.0.1:8740/mcp"
    assert not config.secure_cookies and not config.smtp_enabled and config.trusted_proxies == []


def test_toml_then_env_then_overrides(data_dir, monkeypatch):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "wwyd.toml").write_text(
        'public_url = "https://wwyd.example/"\nport = 9000\ntrusted_proxies = ["127.0.0.1"]\n'
    )
    config = load_config()
    assert config.public_url == "https://wwyd.example" and config.port == 9000
    assert config.secure_cookies and config.trusted_proxies == ["127.0.0.1"]
    monkeypatch.setenv("WWYD_PORT", "9100")
    monkeypatch.setenv("WWYD_TRUSTED_PROXIES", "10.0.0.0/8, 127.0.0.1")
    config = load_config(host="0.0.0.0")
    assert config.port == 9100 and config.host == "0.0.0.0"
    assert config.trusted_proxies == ["10.0.0.0/8", "127.0.0.1"]


def test_http_issuer_only_on_localhost():
    Config(public_url="http://localhost:8740")
    with pytest.raises(ConfigError, match="https"):
        Config(public_url="http://wwyd.example")
    with pytest.raises(ConfigError):
        Config(public_url="https://wwyd.example/?x=1")


def test_transport_security_hosts():
    remote = transport_security(Config(public_url="https://wwyd.example"))
    assert "wwyd.example" in remote.allowed_hosts and "127.0.0.1:*" not in remote.allowed_hosts
    assert remote.allowed_origins == ["https://wwyd.example"]
    local = transport_security(Config())
    assert "127.0.0.1:*" in local.allowed_hosts and "localhost:*" in local.allowed_hosts


def test_llm_table_and_env(data_dir, monkeypatch):
    from wwyd.llm import presets

    assert load_config().llm.base_url is None  # empty: built-in LocalAI default
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "wwyd.toml").write_text(
        '[llm]\nbase_url = "https://llm.example/v1/"\nmodel = "m1"\nprice_in_per_m = 0.5\n'
    )
    monkeypatch.setenv("WWYD_LLM_MODEL", "m2")
    monkeypatch.setenv("WWYD_LLM_API_KEY", "sk-secret-value")
    monkeypatch.setenv("WWYD_LLM_PRICE_OUT_PER_M", "1.5")
    llm = load_config().llm
    assert (llm.base_url, llm.model, llm.price_in_per_m, llm.price_out_per_m) == (
        "https://llm.example/v1/",
        "m2",
        0.5,
        1.5,
    )
    assert "sk-secret-value" not in repr(load_config())
    try:
        llm.apply()
        backend = presets.instance_backend()
        assert backend.base_url == "https://llm.example/v1" and backend.api_key == "sk-secret-value"
    finally:
        presets.set_instance_backend()
    monkeypatch.setenv("WWYD_LLM_PRICE_IN_PER_M", "cheap")
    with pytest.raises(ConfigError):
        load_config()
