"""The IKB Direkt Smart Meter integration."""

from __future__ import annotations

import logging

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
except ImportError:
    # Enables importing api and const outside a full Home Assistant environment
    ConfigEntry = None  # type: ignore[assignment, misc]
    Platform = None  # type: ignore[assignment, misc]
    HomeAssistant = None  # type: ignore[assignment, misc]
    async_get_clientsession = None  # type: ignore[assignment]


from .api import IKBDirektClient, MeterPoint
from .const import CONF_METER_ID, CONF_PASSWORD, CONF_USERNAME, DOMAIN

if Platform is not None:
    PLATFORMS: list[Platform] = [Platform.SENSOR]
    from .coordinator import IKBDirektCoordinator
else:
    PLATFORMS = []
    IKBDirektCoordinator = None  # type: ignore[assignment, misc]




async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up IKB Direkt Smart Meter from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    meter_cp = entry.data[CONF_METER_ID]
    meter_raw = entry.data.get("meter_raw_json", "")
    meter_number = entry.data.get("meter_number", "")
    reading_type = entry.data.get("reading_type", "Verbrauch")
    display_name = entry.data.get("display_name", "")

    meter = MeterPoint(
        counting_point=meter_cp,
        meter_number=meter_number,
        reading_type=reading_type,
        period_start="",
        period_end="",
        raw_json=meter_raw,
        display_name=display_name,
    )

    session = async_get_clientsession(hass)
    client = IKBDirektClient(username=username, password=password, session=session)

    coordinator = IKBDirektCoordinator(
        hass=hass,
        client=client,
        meter=meter,
        config_entry=entry,
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an IKB Direkt config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: IKBDirektCoordinator | None = hass.data[DOMAIN].pop(
            entry.entry_id, None
        )
        if coordinator:
            await coordinator.client.close()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
