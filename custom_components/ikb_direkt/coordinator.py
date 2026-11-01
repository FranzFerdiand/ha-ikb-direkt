"""DataUpdateCoordinator for IKB Direkt Smart Meter."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

try:
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
        get_last_statistics,
    )
    HAVE_RECORDER = True
except ImportError:
    HAVE_RECORDER = False

from .api import IKBDirektClient, MeterPoint, Reading
from .const import (
    CONF_HISTORICAL_DAYS,
    DEFAULT_HISTORICAL_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    RESOLUTION_15MIN,
)

_LOGGER = logging.getLogger(__name__)


class IKBDirektCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage polling of IKB Direkt Smart Meter readings."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: IKBDirektClient,
        meter: MeterPoint,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"IKB Direkt ({meter.counting_point})",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.meter = meter
        self.config_entry = config_entry
        self.statistic_id = f"{DOMAIN}:{meter.counting_point.lower()}_{meter.reading_type.lower()}"

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest readings and insert them into Home Assistant Long-Term Statistics."""
        now = dt_util.now()
        today = now.date()
        historical_days = self.config_entry.options.get(
            CONF_HISTORICAL_DAYS,
            self.config_entry.data.get(CONF_HISTORICAL_DAYS, DEFAULT_HISTORICAL_DAYS),
        )

        last_stat: dict[str, Any] | None = None
        last_stat_sum = 0.0
        last_stat_start: datetime | None = None

        if HAVE_RECORDER:
            try:
                stats = await self.hass.async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1,
                    self.statistic_id,
                    True,
                    {"sum", "state"},
                )
                if self.statistic_id in stats and stats[self.statistic_id]:
                    last_stat = stats[self.statistic_id][0]
                    last_stat_sum = float(last_stat.get("sum", 0.0) or 0.0)
                    start_val = last_stat.get("start")
                    if isinstance(start_val, (int, float)):
                        last_stat_start = datetime.fromtimestamp(start_val, tz=timezone.utc)
                    elif isinstance(start_val, datetime):
                        last_stat_start = start_val
            except Exception as err:
                _LOGGER.warning("Could not query last statistics from recorder: %s", err)

        # Determine start date for fetching
        if last_stat_start is not None:
            # Re-fetch from 2 days prior to last known reading to handle delayed grid corrections
            fetch_start_date = (last_stat_start - timedelta(days=2)).date()
        else:
            fetch_start_date = today - timedelta(days=historical_days)

        fetch_end_date = today

        _LOGGER.debug(
            "Fetching IKB load profile from %s to %s for %s",
            fetch_start_date,
            fetch_end_date,
            self.meter.counting_point,
        )

        try:
            readings = await self.client.get_load_profile(
                meter=self.meter,
                start_date=fetch_start_date,
                end_date=fetch_end_date,
                resolution=RESOLUTION_15MIN,
            )
        except Exception as err:
            raise UpdateFailed(f"Error communicating with IKB Direkt: {err}") from err

        if not readings:
            _LOGGER.debug("No readings received from IKB Direkt for %s", self.meter.counting_point)
            return self.data or {
                "latest_reading_time": None,
                "latest_reading_kwh": None,
                "yesterday_consumption_kwh": None,
                "total_imported_kwh": last_stat_sum,
            }

        # Import into Long-Term Statistics
        if HAVE_RECORDER:
            await self._async_import_statistics(readings, last_stat_sum, last_stat_start)

        # Compute summary values for sensor entities
        latest_reading = readings[-1]
        yesterday = today - timedelta(days=1)
        yesterday_kwh = sum(
            r.kwh
            for r in readings
            if r.start_datetime.astimezone(dt_util.DEFAULT_TIME_ZONE).date() == yesterday
        )

        return {
            "latest_reading_time": latest_reading.end_datetime.isoformat(),
            "latest_reading_kwh": round(latest_reading.kwh, 4),
            "yesterday_consumption_kwh": round(yesterday_kwh, 3),
            "total_imported_kwh": round(last_stat_sum + sum(r.kwh for r in readings), 3),
        }

    async def _async_import_statistics(
        self,
        readings: list[Reading],
        last_stat_sum: float,
        last_stat_start: datetime | None,
    ) -> None:
        """Process readings and push them to async_add_external_statistics."""
        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"IKB {self.meter.meter_number} {self.meter.reading_type}",
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

        statistics: list[StatisticData] = []
        running_sum = last_stat_sum

        for r in readings:
            r_start = r.start_datetime
            if last_stat_start is not None and r_start <= last_stat_start:
                continue

            running_sum += r.kwh
            statistics.append(
                StatisticData(
                    start=r_start,
                    state=r.kwh,
                    sum=running_sum,
                )
            )

        if statistics:
            _LOGGER.info(
                "Inserting %d new statistics points for %s (new cumulative sum: %.3f kWh)",
                len(statistics),
                self.statistic_id,
                running_sum,
            )
            async_add_external_statistics(self.hass, metadata, statistics)
        else:
            _LOGGER.debug("No new statistics points to insert for %s", self.statistic_id)
