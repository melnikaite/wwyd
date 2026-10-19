# SPDX-License-Identifier: AGPL-3.0-or-later
"""Limits wired into the server, config, web pages and /healthz (SPEC §11, §9.5)."""

from __future__ import annotations

from typing import Any

import pytest
from authkit import PASSWORD, csrf_of, login, make_web, web  # noqa: F401
from conftest import call, call_error, make_server
from mcp import Client

from wwyd import limits
from wwyd.auth import tokens
from wwyd.auth.scopes import parse_scope
from wwyd.config import ConfigError, load_config, load_limits
from wwyd.limits import DEFAULTS, get_limiter

pytestmark = pytest.mark.anyio


def _ping(server) -> None:
    @server.scoped_tool(["owner", "read", "respond"])
    async def ping_limits() -> dict[str, Any]:
        """Dummy."""
        return {"ok": True}


# --- per-call rate limit, in memory ------------------------------------------------------


async def test_per_subject_bucket_in_memory(conn, user):
    get_limiter(conn).set_own_limits(user.user_id, {"rpm_subject": 2})
    server = make_server(conn, user)
    _ping(server)
    async with Client(server) as c:
        assert (await call(c, "ping_limits"))["ok"]
        await call(c, "ping_limits")
        err = await call_error(c, "ping_limits")
    assert err["code"] == "rate_limited" and err["retry_after"] >= 1
    assert get_limiter(conn).usage(user.user_id)["calls:ping_limits"] == 2


async def test_per_persona_bucket_in_memory(conn, user):
    get_limiter(conn).set_own_limits(user.user_id, {"rpm_persona": 1})
    server = make_server(conn, user)
    _ping(server)
    async with Client(server) as c:
        await call(c, "ping_limits")
        err = await call_error(c, "ping_limits")
    assert err["code"] == "rate_limited"


async def test_predict_escalates_instead_of_error(conn, user):
    get_limiter(conn).set_own_limits(user.user_id, {"rpm_subject": 0})
    async with Client(make_server(conn, user)) as c:
        out = await call(c, "predict", {"situation": "x", "audience": "x"})
    assert out == {
        "type": "escalate",
        "escalate": "rate_limited",
        "reason": out["reason"],
        "hints": [],
    }


# --- per-call rate limit over HTTP with two API keys -----------------------------------------


def _keys(web, n: int = 2) -> tuple[str, list[str]]:  # noqa: F811
    _ping(web.state.mcp)
    u = web.user()
    keys = [
        tokens.create_api_key(web.conn, u.id, f"k{i}", parse_scope("owner"))[1] for i in range(n)
    ]
    return u.id, keys


async def _ping_http(web, key: str):  # noqa: F811, ANN202
    async with web.mcp(key) as c:
        return await c.call_tool("ping_limits", {})


async def test_per_key_bucket_http(web):  # noqa: F811
    uid, (a, b) = _keys(web)
    get_limiter(web.conn).set_admin_ceilings(uid, {"rpm_subject": 1})
    assert not (await _ping_http(web, a)).is_error
    res = await _ping_http(web, a)
    assert res.is_error and res.structured_content["error"]["code"] == "rate_limited"
    assert not (await _ping_http(web, b)).is_error  # other key, own bucket


async def test_per_persona_bucket_http(web):  # noqa: F811
    uid, (a, b) = _keys(web)
    get_limiter(web.conn).set_own_limits(uid, {"rpm_persona": 2})
    assert not (await _ping_http(web, a)).is_error
    assert not (await _ping_http(web, b)).is_error
    res = await _ping_http(web, b)
    assert res.structured_content["error"]["code"] == "rate_limited"


# --- ceiling semantics --------------------------------------------------------------------


def test_admin_ceiling_may_exceed_default(conn, user):
    lim = get_limiter(conn)
    lim.set_admin_ceilings(user.user_id, {"rpm_subject": 600})
    assert lim.effective_limits(user.user_id)["rpm_subject"] == 600
    lim.set_own_limits(user.user_id, {"rpm_subject": 100})
    assert lim.effective_limits(user.user_id)["rpm_subject"] == 100


# --- config --------------------------------------------------------------------------------


def test_load_limits_toml_and_env(data_dir, monkeypatch):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "wwyd.toml").write_text(
        "[limits]\npredict_per_day = 900\nspend_cap_usd = 12.5\n", encoding="utf-8"
    )
    monkeypatch.setenv("WWYD_LIMIT_RPM_SUBJECT", "30")
    monkeypatch.setenv("WWYD_LIMIT_PREDICT_PER_DAY", "1000")
    cfg = load_config()
    assert cfg.limits.predict_per_day == 1000  # env wins over toml
    assert cfg.limits.rpm_subject == 30 and cfg.limits.spend_cap_usd == 12.5
    assert cfg.limits.rpm_persona == DEFAULTS.rpm_persona
    assert load_limits(None, {"WWYD_LIMIT_SPEND_CAP_USD": "none"}).spend_cap_usd is None


@pytest.mark.parametrize(
    ("table", "env"),
    [
        ({"nope": 1}, {}),
        ({"rpm_subject": "a lot"}, {}),
        ({"rpm_subject": 1.5}, {}),
        ({"rpm_subject": -1}, {}),
        ({"rpm_subject": True}, {}),
        ({"llm_concurrency": 0}, {}),
        (None, {"WWYD_LIMIT_BATCH_RECORDS": "ten"}),
        ([], {}),
    ],
)
def test_load_limits_errors(table, env):
    with pytest.raises(ConfigError):
        load_limits(table, env)


def test_configure_installs_defaults(conn):
    custom = load_limits({"rpm_subject": 7, "llm_concurrency": 3}, {})
    try:
        limits.configure(custom)
        assert get_limiter(conn).defaults.rpm_subject == 7
        assert limits.get_gate().concurrency == 3
    finally:
        limits.configure(DEFAULTS)


async def test_create_app_installs_config_limits(conn):
    w, stack = await make_web(limits=load_limits({"rpm_persona": 9}, {}))
    try:
        async with stack:
            assert get_limiter(w.conn).defaults.rpm_persona == 9
    finally:
        limits.configure(DEFAULTS)
        w.conn.close()


# --- health --------------------------------------------------------------------------------


async def test_healthz(web):  # noqa: F811
    async with web.browser() as b:
        for _ in range(50):
            r = await b.get("/healthz")
            assert r.status_code == 200 and r.json() == {"status": "ok"}


# --- web pages -----------------------------------------------------------------------------


async def test_admin_sets_ceilings(web):  # noqa: F811
    web.user(admin=True)
    other = web.user("other@example.com")
    async with web.browser() as b:
        await login(b)
        page = await b.get("/admin")
        assert "Limits and usage today" in page.text
        url = f"/admin/users/{other.id}/limits"
        r = await b.post(
            url,
            data={
                "csrf": csrf_of(page.text),
                "limit_predict_per_day": "2000",
                "limit_rpm_subject": "",
            },
        )
        assert r.status_code == 303
        assert get_limiter(web.conn).ceilings(other.id) == {"predict_per_day": 2000}
        bad = await b.post(url, data={"csrf": "nope", "limit_predict_per_day": "1"})
        assert bad.status_code == 403
        r = await b.post(url, data={"csrf": csrf_of(page.text), "limit_rpm_subject": "-3"})
        assert r.status_code == 400
        r = await b.post(url, data={"csrf": csrf_of(page.text), "limit_predict_per_day": ""})
        assert get_limiter(web.conn).ceilings(other.id) == {}


async def test_non_admin_cannot_set_ceilings(web):  # noqa: F811
    web.user(admin=True)
    me = web.user("plain@example.com", admin=False)
    async with web.browser() as b:
        await login(b, "plain@example.com")
        page = await b.get("/account")
        r = await b.post(
            f"/admin/users/{me.id}/limits",
            data={"csrf": csrf_of(page.text), "limit_predict_per_day": "9999"},
        )
        assert r.status_code == 403
    assert get_limiter(web.conn).ceilings(me.id) == {}


async def test_account_own_limits_and_spend_flag(web):  # noqa: F811
    u = web.user()
    lim = get_limiter(web.conn)
    async with web.browser() as b:
        await login(b)
        page = await b.get("/account")
        assert "Limits and usage" in page.text and "spending limit is reached" not in page.text
        r = await b.post(
            "/account/limits", data={"csrf": csrf_of(page.text), "limit_rpm_subject": "10"}
        )
        assert r.status_code == 200 and lim.own_limits(u.id) == {"rpm_subject": 10}
        r = await b.post(
            "/account/limits", data={"csrf": csrf_of(page.text), "limit_rpm_subject": "61"}
        )
        assert r.status_code == 400 and "may not be higher than 60" in r.text
        lim.record_llm_usage(u.id, u.persona_id, 10, 10, 6.0)
        page = await b.get("/account")
        assert "spending limit is reached" in page.text
