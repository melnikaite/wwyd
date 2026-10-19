# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
from datetime import UTC, datetime

import pytest

from wwyd.errors import Invalid, LLMUnavailable, RateLimited, SpendCap, TooLarge
from wwyd.limits import DEFAULTS, Limiter, LLMGate, estimate_cost, get_limiter
from wwyd.store import personas

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self, dt: datetime) -> None:
        self.t = dt.timestamp()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock(datetime(2026, 3, 31, 23, 59, 0, tzinfo=UTC))


@pytest.fixture
def lim(conn, clock):
    return Limiter(conn, clock=clock)


def test_effective_ceiling_replaces_default(lim, user):
    uid = user.user_id
    assert lim.effective_limits(uid) == DEFAULTS.user_limits()
    lim.set_admin_ceilings(uid, {"predict_per_day": 1000, "rpm_subject": 30})
    eff = lim.effective_limits(uid)
    # A ceiling replaces the default, upwards (larger plan) as well as downwards.
    assert eff["predict_per_day"] == 1000 and eff["rpm_subject"] == 30
    lim.set_own_limits(uid, {"rpm_subject": 10, "spend_cap_usd": 1.5})
    eff = lim.effective_limits(uid)
    assert eff["rpm_subject"] == 10 and eff["spend_cap_usd"] == 1.5
    lim.set_admin_ceilings(uid, {"rpm_subject": 5})
    assert lim.effective_limits(uid)["rpm_subject"] == 5
    lim.set_admin_ceilings(uid, {"rpm_subject": None})
    assert lim.ceilings(uid) == {"predict_per_day": 1000}
    assert lim.own_limits(uid) == {"rpm_subject": 10, "spend_cap_usd": 1.5}


def test_user_cannot_raise(lim, user):
    uid = user.user_id
    with pytest.raises(Invalid):
        lim.set_own_limits(uid, {"predict_per_day": 501})
    lim.set_admin_ceilings(uid, {"predict_per_day": 1000})
    lim.set_own_limits(uid, {"predict_per_day": 800})  # above the default, under the ceiling
    assert lim.effective_limits(uid)["predict_per_day"] == 800
    with pytest.raises(Invalid):
        lim.set_own_limits(uid, {"predict_per_day": 1001})
    lim.set_own_limits(uid, {"predict_per_day": None})
    lim.set_admin_ceilings(uid, {"predict_per_day": 100})
    with pytest.raises(Invalid):
        lim.set_own_limits(uid, {"predict_per_day": 101})
    for bad in ({"nope": 1}, {"batch_records": -1}, {"batch_records": 1.5}, {"rpm_subject": True}):
        with pytest.raises(Invalid):
            lim.set_own_limits(uid, bad)
    lim.set_own_limits(uid, {"predict_per_day": 100})
    lim.set_own_limits(uid, {"predict_per_day": None})
    assert lim.own_limits(uid) == {}


def test_rpm_hits_and_refills(lim, user, clock):
    uid, pid = user.user_id, user.persona_id
    lim.set_own_limits(uid, {"rpm_subject": 2})
    lim.check_request(uid, pid, "tok1", "search")
    lim.check_request(uid, pid, "tok1", "search")
    with pytest.raises(RateLimited) as e:
        lim.check_request(uid, pid, "tok1", "search")
    assert e.value.to_payload()["retry_after"] == 30
    lim.check_request(uid, pid, "tok2", "search")  # other subject has its own bucket
    clock.advance(30)
    lim.check_request(uid, pid, "tok1", "search")
    assert lim.usage(uid)["calls:search"] == 4


def test_rpm_persona_bucket(lim, user):
    uid, pid = user.user_id, user.persona_id
    lim.set_own_limits(uid, {"rpm_persona": 3})
    for i in range(3):
        lim.check_request(uid, pid, f"tok{i}", "x")
    with pytest.raises(RateLimited):
        lim.check_request(uid, pid, "tok9", "x")


def test_predict_caps_and_audience_isolation(lim, user, clock):
    uid, pid = user.user_id, user.persona_id
    lim.set_own_limits(uid, {"predict_per_day": 3, "predict_per_audience_per_day": 2})
    for _ in range(2):
        lim.check_predict(uid, pid, "aud_a")
        lim.record_predict(uid, pid, "aud_a")
    with pytest.raises(RateLimited) as e:
        lim.check_predict(uid, pid, "aud_a")
    assert e.value.retry_after == 60  # 23:59 -> midnight
    lim.check_predict(uid, pid, "aud_b")
    lim.record_predict(uid, pid, "aud_b")
    with pytest.raises(RateLimited):
        lim.check_predict(uid, pid, "aud_b")  # persona cap of 3 reached
    clock.advance(60)  # new UTC day
    lim.check_predict(uid, pid, "aud_a")


def test_llm_tokens_per_day(lim, user, clock):
    uid, pid = user.user_id, user.persona_id
    lim.set_own_limits(uid, {"llm_tokens_per_day": 100})
    lim.check_llm_budget(uid, pid)
    lim.record_llm_usage(uid, pid, 60, 40, None)
    with pytest.raises(RateLimited):
        lim.check_llm_budget(uid, pid)
    clock.advance(120)
    lim.check_llm_budget(uid, pid)


def _flag(conn, pid):
    return conn.execute("SELECT spend_capped_at FROM personas WHERE id = ?", (pid,)).fetchone()[0]


def test_spend_cap_sets_and_clears(conn, lim, user, clock):
    uid, pid = user.user_id, user.persona_id
    lim.set_own_limits(uid, {"spend_cap_usd": 1.0})
    lim.record_llm_usage(uid, pid, 10, 10, 0.6)
    lim.check_llm_budget(uid, pid)
    assert _flag(conn, pid) is None
    lim.record_llm_usage(uid, pid, 10, 10, 0.5)
    assert _flag(conn, pid) is not None
    with pytest.raises(SpendCap):
        lim.check_llm_budget(uid, pid)
    # Raising the cap clears the flag lazily.
    lim.set_own_limits(uid, {"spend_cap_usd": None})
    lim.check_llm_budget(uid, pid)
    assert _flag(conn, pid) is None
    lim.set_own_limits(uid, {"spend_cap_usd": 1.0})
    with pytest.raises(SpendCap):
        lim.check_llm_budget(uid, pid)
    clock.advance(120)  # April: new month
    lim.check_llm_budget(uid, pid)
    assert _flag(conn, pid) is None


def test_local_backend_never_capped(lim, user):
    uid, pid = user.user_id, user.persona_id
    lim.set_own_limits(uid, {"spend_cap_usd": 0.0001})
    lim.record_llm_usage(uid, pid, 1000, 1000, None)
    lim.check_llm_budget(uid, pid)


def test_estimate_cost():
    assert estimate_cost(1_000_000, 500_000, 1.0, 2.0) == pytest.approx(2.0)
    assert estimate_cost(10, 10, None, None) is None


def test_ingest_limits(lim, user, clock):
    uid, pid = user.user_id, user.persona_id
    with pytest.raises(TooLarge):
        lim.check_ingest(uid, pid, 1001, 10)
    with pytest.raises(TooLarge):
        lim.check_ingest(uid, pid, 1, 2 * 1024 * 1024 + 1)
    lim.set_own_limits(uid, {"ingest_bytes_per_day": 1000})
    lim.check_ingest(uid, pid, 5, 600)
    lim.record_ingest(uid, pid, 5, 600)
    with pytest.raises(RateLimited):
        lim.check_ingest(uid, pid, 5, 600)
    lim.check_ingest(uid, pid, 1, 400)
    clock.advance(60)
    lim.check_ingest(uid, pid, 5, 600)


def test_usage_counters(lim, user, clock):
    uid, pid = user.user_id, user.persona_id
    lim.record_llm_usage(uid, pid, 10, 5, 0.01)
    lim.record_llm_usage(uid, pid, 1, 2, 0.02)
    lim.record_ingest(uid, pid, 3, 300)
    day1 = lim.usage(uid)
    assert day1["llm_tokens_in"] == 11 and day1["llm_tokens_out"] == 7
    assert day1["cost_usd"] == pytest.approx(0.03) and day1["ingest_records"] == 3
    clock.advance(120)
    lim.record_ingest(uid, pid, 2, 200)
    r = lim.usage_range(uid, "2026-03-31", "2026-04-01")
    assert set(r["days"]) == {"2026-03-31", "2026-04-01"}
    assert r["totals"]["ingest_bytes"] == 500 and r["totals"]["ingest_records"] == 5
    assert lim.usage(uid, "2026-03-31") == day1


async def test_job_slot(lim, user):
    uid, pid = user.user_id, user.persona_id
    async with lim.job_slot(uid, pid):
        with pytest.raises(RateLimited):
            async with lim.job_slot(uid, pid):
                pass
    async with lim.job_slot(uid, pid):
        pass
    lim.set_admin_ceilings(uid, {"concurrent_jobs": 5})  # replaces the default of 1
    lim.set_own_limits(uid, {"concurrent_jobs": 2})
    with pytest.raises(Invalid):
        lim.set_own_limits(uid, {"concurrent_jobs": 6})


async def test_gate_queue_timeout():
    gate = LLMGate(1, queue_timeout_s=0.05, call_timeout_s=1)
    async with gate.slot():
        with pytest.raises(LLMUnavailable):
            async with gate.slot():
                pass
    async with gate.slot():
        pass


async def test_gate_call_timeout():
    gate = LLMGate(2, queue_timeout_s=1, call_timeout_s=0.05)
    with pytest.raises(LLMUnavailable):
        await gate.run(asyncio.sleep(1))
    assert await gate.run(asyncio.sleep(0, result=7)) == 7
    # Slots were released.
    assert gate._sem._value == 2


def test_get_limiter_cached(conn):
    assert get_limiter(conn) is get_limiter(conn)


def test_store_settings_limits(conn, user):
    s = personas.update_settings(conn, user.persona_id, limits={"predict_per_day": 10})
    assert s["limits"]["own"] == {"predict_per_day": 10}
    assert s["limits"]["effective"]["predict_per_day"] == 10
    assert s["spend_capped"] is False and s["usage_today"] == {} and s["privacy_note"]
    with pytest.raises(Invalid):
        personas.update_settings(conn, user.persona_id, limits={"predict_per_day": 10_000})
