"""DataUpdateCoordinator for IKB Direkt Smart Meter."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import UnitOfEnergy
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
    import homeassistant.util.dt as dt_util
except ImportError:
    ConfigEntry = Any  # type: ignore[assignment, misc]
    HomeAssistant = Any  # type: ignore[assignment, misc]
    class UnitOfEnergy:  # type: ignore[no-redef]
        KILO_WATT_HOUR = "kWh"
    class UpdateFailed(Exception):  # type: ignore[no-redef]
        pass
    class DataUpdateCoordinator:  # type: ignore[no-redef, type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass
        def __class_getitem__(cls, item: Any) -> Any:
            return cls

    class DtUtil:
        DEFAULT_TIME_ZONE = timezone.utc
        @staticmethod
        def now() -> datetime:
            return datetime.now(timezone.utc)
    dt_util = DtUtil()  # type: ignore[assignment]


try:
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
        StatisticMeanType,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
        get_last_statistics,
    )
    HAVE_RECORDER = True
except ImportError:
    try:
        from homeassistant.components.recorder.models import (
            StatisticData,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            get_last_statistics,
        )
        HAVE_RECORDER = True
        StatisticMeanType = None
    except ImportError:
        HAVE_RECORDER = False
        StatisticMeanType = None
        from dataclasses import dataclass

        @dataclass
        class StatisticData:  # type: ignore[no-redef]
            start: datetime
            state: float
            sum: float

        @dataclass
        class StatisticMetaData:  # type: ignore[no-redef]
            has_mean: bool
            has_sum: bool
            name: str
            source: str
            statistic_id: str
            unit_of_measurement: str
            mean_type: Any = None


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

        new_kwh = sum(
            r.kwh
            for r in readings
            if last_stat_start is None or r.start_datetime > last_stat_start
        )
        total_sum = last_stat_sum + new_kwh

        return {
            "latest_reading_time": latest_reading.end_datetime.isoformat(),
            "latest_reading_kwh": round(latest_reading.kwh, 4),
            "yesterday_consumption_kwh": round(yesterday_kwh, 3),
            "total_imported_kwh": round(total_sum, 3),
        }

    async def _async_import_statistics(
        self,
        readings: list[Reading],
        last_stat_sum: float,
        last_stat_start: datetime | None,
    ) -> None:
        """Process readings and push them to async_add_external_statistics."""
        try:
            if StatisticMeanType is not None:
                metadata = StatisticMetaData(
                    has_mean=False,
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"IKB {self.meter.meter_number} {self.meter.reading_type}",
                    source=DOMAIN,
                    statistic_id=self.statistic_id,
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                )
            else:
                metadata = StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    name=f"IKB {self.meter.meter_number} {self.meter.reading_type}",
                    source=DOMAIN,
                    statistic_id=self.statistic_id,
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                )
        except TypeError:
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"IKB {self.meter.meter_number} {self.meter.reading_type}",
                source=DOMAIN,
                statistic_id=self.statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )

        statistics = aggregate_readings_to_hourly_statistics(
            readings, last_stat_sum=last_stat_sum, last_stat_start=last_stat_start
        )

        if statistics:
            _LOGGER.info(
                "Inserting %d hourly statistics points for %s (new cumulative sum: %.3f kWh)",
                len(statistics),
                self.statistic_id,
                statistics[-1].sum,
            )
            async_add_external_statistics(self.hass, metadata, statistics)
        else:
            _LOGGER.debug("No new statistics points to insert for %s", self.statistic_id)


def aggregate_readings_to_hourly_statistics(
    readings: list[Reading],
    last_stat_sum: float = 0.0,
    last_stat_start: datetime | None = None,
) -> list[StatisticData]:
    """Aggregate 15-minute readings into hourly StatisticData points for HA recorder.

    Home Assistant requires all external statistics timestamps to be aligned to
    the top of the hour (minute=0, second=0, microsecond=0).
    """
    hourly_buckets: dict[datetime, float] = {}
    for r in readings:
        h_start = r.start_datetime.replace(minute=0, second=0, microsecond=0)
        hourly_buckets[h_start] = hourly_buckets.get(h_start, 0.0) + r.kwh

    statistics: list[StatisticData] = []
    running_sum = last_stat_sum

    for h_start in sorted(hourly_buckets.keys()):
        h_kwh = hourly_buckets[h_start]
        if last_stat_start is not None and h_start <= last_stat_start:
            continue

        running_sum += h_kwh
        statistics.append(
            StatisticData(
                start=h_start,
                state=round(h_kwh, 4),
                sum=round(running_sum, 4),
            )
        )

    return statistics

