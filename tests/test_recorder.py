"""Run the coordinator against Home Assistant and a real SQLite Recorder."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries
from homeassistant.setup import async_setup_component
from homeassistant import loader
from homeassistant.helpers.recorder import async_initialize_recorder
from homeassistant.helpers import frame
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period

from custom_components.ikb_direkt.api import MeterPoint
from custom_components.ikb_direkt.coordinator import IKBDirektCoordinator, UpdateFailed
from tests.test_statistics import intervals


@pytest.fixture
async def recorder_hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config.time_zone = "Europe/Vienna"
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    loader.async_setup(hass)
    frame.async_setup(hass)
    async_initialize_recorder(hass)
    assert await async_setup_component(hass, "recorder", {
        "recorder": {"db_url": f"sqlite:///{tmp_path / 'test.db'}"}
    })
    await hass.async_start()
    await hass.async_block_till_done()
    await hass.async_add_executor_job(get_instance(hass).block_till_done)
    yield hass
    await hass.async_stop()


async def read_rows(hass, ids):
    await hass.async_add_executor_job(get_instance(hass).block_till_done)
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(1970, 1, 1, tzinfo=timezone.utc),
        None, set(ids), "hour", {"energy": "kWh"}, {"state", "sum"})


async def test_real_recorder_corrections_costs_and_empty_response(recorder_hass):
    hass = recorder_hass
    meter = MeterPoint("AT1", "M1", "Verbrauch", "", "", "{}", "Test")
    client = SimpleNamespace(get_meters=AsyncMock(return_value=[meter]), get_load_profile=AsyncMock())
    entry = SimpleNamespace(data={}, options={"tariff_model": "fixed", "energy_price": .2})
    coordinator = IKBDirektCoordinator(hass, client, meter, entry)
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
    client.get_load_profile.return_value = intervals(hour)
    data = await coordinator._async_update_data()
    rows = await read_rows(hass, [coordinator.statistic_id, coordinator.cost_statistic_id])
    assert data["total_imported_kwh"] == rows[coordinator.statistic_id][-1]["sum"] == 1
    assert rows[coordinator.cost_statistic_id][-1]["sum"] == .2

    client.get_load_profile.return_value = intervals(hour, (.5,) * 4) + intervals(hour + timedelta(hours=1))
    data = await coordinator._async_update_data()
    rows = await read_rows(hass, [coordinator.statistic_id, coordinator.cost_statistic_id])
    assert len(rows[coordinator.statistic_id]) == 2
    assert data["total_imported_kwh"] == rows[coordinator.statistic_id][-1]["sum"] == 3
    assert rows[coordinator.cost_statistic_id][-1]["sum"] == .6

    # The same response must neither double-count nor create extra rows.
    assert (await coordinator._async_update_data())["total_imported_kwh"] == 3
    await hass.async_add_executor_job(get_instance(hass).block_till_done)
    # Prices change even when the portal has no data; use persisted energy.
    entry.options["energy_price"] = .4
    client.get_load_profile.return_value = []
    data = await coordinator._async_update_data()
    rows = await read_rows(hass, [coordinator.cost_statistic_id])
    assert rows[coordinator.cost_statistic_id][-1]["sum"] == 1.2
    assert data["total_imported_kwh"] == 3
    assert data["yesterday_data_delivered"] is False
    assert data["latest_reading_time"] is None

    # A recorder read error must not become a reset-to-zero import.
    with patch("custom_components.ikb_direkt.coordinator.statistics_during_period", side_effect=RuntimeError("DB offline")):
        with pytest.raises(UpdateFailed, match="Cannot read"):
            await coordinator._async_update_data()
