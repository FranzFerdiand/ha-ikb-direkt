"""Config flow checks run against real Home Assistant classes in HA CI."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("homeassistant")
from custom_components.ikb_direkt.config_flow import IKBDirektConfigFlow
from custom_components.ikb_direkt.api import MeterPoint


def meter(kind="Verbrauch", raw="{}"):
    return MeterPoint("AT1", "M1", kind, "", "", raw, kind)


def flow():
    obj = IKBDirektConfigFlow()
    obj.hass = MagicMock()
    obj.async_set_unique_id = AsyncMock()
    obj._abort_if_unique_id_configured = MagicMock()
    obj.async_create_entry = MagicMock(side_effect=lambda **kwargs: kwargs)
    obj.async_show_form = MagicMock(side_effect=lambda **kwargs: kwargs)
    return obj


@pytest.mark.asyncio
async def test_meter_selection_preserves_type_days_and_deduplicates_months():
    obj = flow()
    client = MagicMock()
    client.login = AsyncMock()
    client.get_meters = AsyncMock(return_value=[meter(), meter(raw='{"month":2}'), meter("Einspeisung")])
    client.close = AsyncMock()
    with patch("custom_components.ikb_direkt.config_flow.IKBDirektClient", return_value=client):
        result = await obj.async_step_user({"username":"u", "password":"p", "historical_days":100})
    assert result["step_id"] == "meter"
    assert len(obj._meters) == 2
    client.close.assert_awaited_once()
    result = await obj.async_step_meter({"meter_id":"AT1|Einspeisung"})
    assert result["data"]["reading_type"] == "Einspeisung"
    assert result["data"]["historical_days"] == 100


@pytest.mark.asyncio
async def test_failed_setup_closes_temporary_client():
    from custom_components.ikb_direkt.api import IKBAuthenticationError
    obj = flow()
    client = MagicMock()
    client.login = AsyncMock(side_effect=IKBAuthenticationError("bad credentials"))
    client.close = AsyncMock()
    with patch("custom_components.ikb_direkt.config_flow.IKBDirektClient", return_value=client):
        result = await obj.async_step_user({"username":"u", "password":"p"})
    assert result["errors"] == {"base":"invalid_auth"}
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_reauth_checks_meter_and_updates_existing_entry():
    obj = flow()
    entry = SimpleNamespace(entry_id="entry", data={"username":"u", "password":"old", "meter_id":"AT1"})
    obj._reauth_entry = entry
    obj.hass.config_entries.async_reload = AsyncMock()
    obj.async_abort = MagicMock(side_effect=lambda **kwargs: kwargs)
    client = MagicMock()
    client.get_meters = AsyncMock(return_value=[meter()])
    client.close = AsyncMock()
    with patch("custom_components.ikb_direkt.config_flow.IKBDirektClient", return_value=client):
        result = await obj.async_step_reauth_confirm({"password":"new"})
    assert result["reason"] == "reauth_successful"
    assert obj.hass.config_entries.async_update_entry.call_args.kwargs["data"]["password"] == "new"
    obj.hass.config_entries.async_reload.assert_awaited_once_with("entry")
    client.close.assert_awaited_once()
