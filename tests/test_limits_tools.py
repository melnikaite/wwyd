# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
from conftest import call, call_error

pytestmark = pytest.mark.anyio


async def test_settings_tools_limits(client):
    s = await call(client, "get_settings")
    assert s["limits"]["effective"]["predict_per_day"] == 500
    assert s["limits"]["ceilings"] == {} and s["limits"]["own"] == {}
    assert s["spend_capped"] is False and s["spend_capped_at"] is None
    assert "LLM backend" in s["privacy_note"]
    s = await call(client, "update_settings", {"limits": {"rpm_subject": 20}})
    assert s["limits"]["own"] == {"rpm_subject": 20}
    assert s["limits"]["effective"]["rpm_subject"] == 20
    err = await call_error(client, "update_settings", {"limits": {"rpm_subject": 61}})
    assert err["code"] == "invalid"
