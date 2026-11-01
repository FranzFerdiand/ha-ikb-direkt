"""Sensor platform for IKB Direkt Smart Meter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import IKBDirektCoordinator


@dataclass(frozen=True, kw_only=True)
class IKBDirektSensorEntityDescription(SensorEntityDescription):
    """Describes IKB Direkt sensor entity."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSOR_DESCRIPTIONS: tuple[IKBDirektSensorEntityDescription, ...] = (
    IKBDirektSensorEntityDescription(
        key="yesterday_consumption",
        translation_key="yesterday_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("yesterday_consumption_kwh"),
    ),
    IKBDirektSensorEntityDescription(
        key="latest_reading",
        translation_key="latest_reading",
        device_class=None,
        icon="mdi:lightning-bolt",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data.get("latest_reading_kwh"),
    ),
    IKBDirektSensorEntityDescription(
        key="latest_reading_timestamp",
        translation_key="latest_reading_timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: (
            datetime.fromisoformat(data["latest_reading_time"])
            if data.get("latest_reading_time")
            else None
        ),
    ),
    IKBDirektSensorEntityDescription(
        key="total_imported_energy",
        translation_key="total_imported_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("total_imported_kwh"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up IKB Direkt sensor entities from a config entry."""
    coordinator: IKBDirektCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        IKBDirektSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class IKBDirektSensor(CoordinatorEntity[IKBDirektCoordinator], SensorEntity):
    """Representation of an IKB Direkt sensor entity."""

    entity_description: IKBDirektSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IKBDirektCoordinator,
        entry: ConfigEntry,
        description: IKBDirektSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        meter = coordinator.meter

        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter.counting_point)},
            name=f"IKB Zähler {meter.meter_number}",
            manufacturer=MANUFACTURER,
            model="Kaifa Smart Meter",
            serial_number=meter.meter_number,
        )

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
