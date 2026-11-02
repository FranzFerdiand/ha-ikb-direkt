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
        from typing import TypedDict

        class StatisticData(TypedDict, total=False):  # type: ignore[no-redef]
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
            unit_class: str | None = None
            mean_type: Any = None


from .api import IKBDirektClient, MeterPoint, Reading
from .const import (
    CONF_BASE_PRICE_YEAR,
    CONF_ENERGY_PRICE,
    CONF_HISTORICAL_DAYS,
    CONF_OFFPEAK_PRICE,
    CONF_TARIFF_MODEL,
    DEFAULT_HISTORICAL_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    RESOLUTION_15MIN,
    TARIFF_MODEL_DUAL_SUMMER_NOON,
    TARIFF_MODEL_FIXED,
    TARIFF_MODEL_NONE,
)

_LOGGER = logging.getLogger(__name__)


def _parse_statistic_start(s: Any) -> datetime:
    """Parse start timestamp or datetime from recorder statistic."""
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=timezone.utc)
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


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
        self.cost_statistic_id = (
            f"{DOMAIN}:{meter.counting_point.lower()}_{meter.reading_type.lower()}_kosten"
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest readings and insert them into Home Assistant Long-Term Statistics."""
        now = dt_util.now()
        today = now.date()
        historical_days = self.config_entry.options.get(
            CONF_HISTORICAL_DAYS,
            self.config_entry.data.get(CONF_HISTORICAL_DAYS, DEFAULT_HISTORICAL_DAYS),
        )
        tariff_model = self.config_entry.options.get(
            CONF_TARIFF_MODEL,
            self.config_entry.data.get(CONF_TARIFF_MODEL, TARIFF_MODEL_NONE),
        )

        last_stat: dict[str, Any] | None = None
        last_stat_sum = 0.0
        last_stat_start: datetime | None = None
        oldest_stat_start: datetime | None = None

        last_cost_sum = 0.0
        last_cost_start: datetime | None = None
        oldest_cost_start: datetime | None = None
        needs_cost_backfill = False

        if HAVE_RECORDER:
            # Query consumption statistics
            try:
                stats = await self.hass.async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1000,
                    self.statistic_id,
                    True,
                    {"sum", "state"},
                )
                if self.statistic_id in stats and stats[self.statistic_id]:
                    stat_list = stats[self.statistic_id]
                    sorted_stats = sorted(
                        stat_list,
                        key=lambda s: _parse_statistic_start(s.get("start")),
                        reverse=True,
                    )
                    chosen_stat = sorted_stats[0]
                    for st in sorted_stats:
                        st_state = st.get("state")
                        if st_state is not None and float(st_state) > 0.0:
                            chosen_stat = st
                            break

                    last_stat_sum = float(chosen_stat.get("sum", 0.0) or 0.0)
                    parsed_start = _parse_statistic_start(chosen_stat.get("start"))
                    if parsed_start != datetime.min.replace(tzinfo=timezone.utc):
                        last_stat_start = parsed_start

                    parsed_oldest = _parse_statistic_start(sorted_stats[-1].get("start"))
                    if parsed_oldest != datetime.min.replace(tzinfo=timezone.utc):
                        oldest_stat_start = parsed_oldest
            except Exception as err:
                _LOGGER.warning("Could not query last statistics from recorder: %s", err)

            # Query cost statistics if tariff model is configured
            if tariff_model and tariff_model != TARIFF_MODEL_NONE:
                try:
                    cost_stats = await self.hass.async_add_executor_job(
                        get_last_statistics,
                        self.hass,
                        1000,
                        self.cost_statistic_id,
                        True,
                        {"sum", "state"},
                    )
                    if self.cost_statistic_id in cost_stats and cost_stats[self.cost_statistic_id]:
                        cost_stat_list = cost_stats[self.cost_statistic_id]
                        sorted_cost_stats = sorted(
                            cost_stat_list,
                            key=lambda s: _parse_statistic_start(s.get("start")),
                            reverse=True,
                        )
                        c_chosen = sorted_cost_stats[0]
                        last_cost_sum = float(c_chosen.get("sum", 0.0) or 0.0)
                        p_c_start = _parse_statistic_start(c_chosen.get("start"))
                        if p_c_start != datetime.min.replace(tzinfo=timezone.utc):
                            last_cost_start = p_c_start

                        p_oldest_c = _parse_statistic_start(sorted_cost_stats[-1].get("start"))
                        if p_oldest_c != datetime.min.replace(tzinfo=timezone.utc):
                            oldest_cost_start = p_oldest_c
                except Exception as err:
                    _LOGGER.warning("Could not query last cost statistics from recorder: %s", err)

                earliest_target = datetime.combine(
                    today - timedelta(days=historical_days),
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                )
                if last_cost_start is None:
                    needs_cost_backfill = True
                    _LOGGER.info(
                        "No cost statistics found for %s. Triggering initial cost backfill for %d days",
                        self.cost_statistic_id,
                        historical_days,
                    )
                elif (
                    oldest_stat_start is not None
                    and oldest_cost_start is not None
                    and oldest_cost_start > oldest_stat_start + timedelta(days=2)
                ):
                    needs_cost_backfill = True
                    _LOGGER.info(
                        "Incomplete cost statistics detected for %s (starts %s vs consumption starts %s). Triggering full cost backfill",
                        self.cost_statistic_id,
                        oldest_cost_start.isoformat(),
                        oldest_stat_start.isoformat(),
                    )
                elif (
                    oldest_cost_start is not None
                    and oldest_cost_start > earliest_target + timedelta(days=2)
                    and historical_days >= 7
                ):
                    needs_cost_backfill = True
                    _LOGGER.info(
                        "Cost statistics for %s do not cover historical window (starts %s, target %s). Triggering full cost backfill",
                        self.cost_statistic_id,
                        oldest_cost_start.isoformat(),
                        earliest_target.isoformat(),
                    )

        # Determine start date for fetching
        if last_stat_start is not None and not needs_cost_backfill:
            # Re-fetch from 2 days prior to last known reading to handle delayed grid corrections
            fetch_start_date = (last_stat_start - timedelta(days=2)).date()
        else:
            fetch_start_date = today - timedelta(days=historical_days)
            if oldest_stat_start is not None and oldest_stat_start.date() < fetch_start_date:
                fetch_start_date = oldest_stat_start.date()

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
                "yesterday_date": (today - timedelta(days=1)).isoformat(),
                "yesterday_data_delivered": False,
                "last_reported_date": None,
                "last_reported_kwh": None,
                "total_imported_kwh": last_stat_sum,
            }

        # Import into Long-Term Statistics
        if HAVE_RECORDER:
            # If cost backfill is needed and existing cost stats were incomplete/absent,
            # we reset last_cost_start and last_cost_sum so calculate_hourly_costs recalculates
            # the full period from the beginning.
            effective_cost_sum = 0.0 if needs_cost_backfill else last_cost_sum
            effective_cost_start = None if needs_cost_backfill else last_cost_start

            await self._async_import_statistics(
                readings=readings,
                last_stat_sum=last_stat_sum,
                last_stat_start=last_stat_start,
                last_cost_sum=effective_cost_sum,
                last_cost_start=effective_cost_start,
                is_backfill=needs_cost_backfill,
            )

        # Determine last positive reading (kwh > 0.0) to ignore trailing placeholder zeros
        positive_readings = [r for r in readings if r.kwh > 0.0]
        if positive_readings:
            latest_reading = positive_readings[-1]
            max_valid_hour: datetime | None = latest_reading.start_datetime.replace(
                minute=0, second=0, microsecond=0
            )
        else:
            latest_reading = readings[-1]
            max_valid_hour = None

        # Yesterday evaluation (using Home Assistant local timezone)
        yesterday = today - timedelta(days=1)
        yesterday_readings = [
            r
            for r in readings
            if r.start_datetime.astimezone(dt_util.DEFAULT_TIME_ZONE).date() == yesterday
        ]
        yesterday_has_data = any(r.kwh > 0.0 for r in yesterday_readings)
        yesterday_kwh = (
            round(sum(r.kwh for r in yesterday_readings), 3)
            if yesterday_has_data
            else None
        )

        # Find most recent date prior to today with verified positive data
        daily_kwh: dict[date, float] = {}
        daily_has_pos: dict[date, bool] = {}
        for r in readings:
            r_date = r.start_datetime.astimezone(dt_util.DEFAULT_TIME_ZONE).date()
            if r_date >= today:
                continue
            daily_kwh[r_date] = daily_kwh.get(r_date, 0.0) + r.kwh
            if r.kwh > 0.0:
                daily_has_pos[r_date] = True

        last_reported_date: date | None = None
        last_reported_kwh: float | None = None
        for d in sorted(daily_kwh.keys(), reverse=True):
            if daily_has_pos.get(d, False):
                last_reported_date = d
                last_reported_kwh = round(daily_kwh[d], 3)
                break

        new_kwh = sum(
            r.kwh
            for r in readings
            if (last_stat_start is None or r.start_datetime > last_stat_start)
            and (max_valid_hour is None or r.start_datetime <= max_valid_hour)
        )
        total_sum = last_stat_sum + new_kwh

        return {
            "latest_reading_time": latest_reading.end_datetime.isoformat(),
            "latest_reading_kwh": round(latest_reading.kwh, 4) if latest_reading else None,
            "yesterday_consumption_kwh": yesterday_kwh,
            "yesterday_date": yesterday.isoformat(),
            "yesterday_data_delivered": yesterday_has_data,
            "last_reported_date": last_reported_date.isoformat() if last_reported_date else None,
            "last_reported_kwh": last_reported_kwh,
            "total_imported_kwh": round(total_sum, 3),
        }

    async def _async_import_statistics(
        self,
        readings: list[Reading],
        last_stat_sum: float,
        last_stat_start: datetime | None,
        last_cost_sum: float = 0.0,
        last_cost_start: datetime | None = None,
        is_backfill: bool = False,
    ) -> None:
        """Process readings and push them to async_add_external_statistics."""
        metadata_kwargs: dict[str, Any] = {
            "has_mean": False,
            "has_sum": True,
            "name": f"IKB {self.meter.meter_number} {self.meter.reading_type}",
            "source": DOMAIN,
            "statistic_id": self.statistic_id,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            "unit_class": "energy",
        }
        if StatisticMeanType is not None:
            metadata_kwargs["mean_type"] = StatisticMeanType.NONE

        try:
            metadata = StatisticMetaData(**metadata_kwargs)
        except TypeError:
            metadata_kwargs.pop("unit_class", None)
            metadata_kwargs.pop("mean_type", None)
            metadata = StatisticMetaData(**metadata_kwargs)

        statistics = aggregate_readings_to_hourly_statistics(
            readings, last_stat_sum=last_stat_sum, last_stat_start=last_stat_start
        )

        if statistics:
            last_stat_sum_val = (
                statistics[-1]["sum"]
                if isinstance(statistics[-1], dict)
                else getattr(statistics[-1], "sum", 0.0)
            )
            _LOGGER.info(
                "Inserting %d hourly statistics points for %s (new cumulative sum: %.3f kWh)",
                len(statistics),
                self.statistic_id,
                last_stat_sum_val,
            )
            async_add_external_statistics(self.hass, metadata, statistics)
        else:
            _LOGGER.debug("No new statistics points to insert for %s", self.statistic_id)

        # Import monetary cost statistics if a tariff model is configured
        tariff_model = self.config_entry.options.get(
            CONF_TARIFF_MODEL,
            self.config_entry.data.get(CONF_TARIFF_MODEL, TARIFF_MODEL_NONE),
        )
        if tariff_model and tariff_model != TARIFF_MODEL_NONE:
            await self._async_import_cost_statistics(
                readings=readings,
                last_cost_sum=last_cost_sum,
                last_cost_start=last_cost_start,
                is_backfill=is_backfill,
            )

    async def _async_import_cost_statistics(
        self,
        readings: list[Reading],
        last_cost_sum: float = 0.0,
        last_cost_start: datetime | None = None,
        is_backfill: bool = False,
    ) -> None:
        """Calculate and insert monetary cost statistics."""
        tariff_model = self.config_entry.options.get(
            CONF_TARIFF_MODEL,
            self.config_entry.data.get(CONF_TARIFF_MODEL, TARIFF_MODEL_NONE),
        )
        base_price_year = float(
            self.config_entry.options.get(
                CONF_BASE_PRICE_YEAR,
                self.config_entry.data.get(CONF_BASE_PRICE_YEAR, 0.0),
            )
        )
        energy_price = float(
            self.config_entry.options.get(
                CONF_ENERGY_PRICE,
                self.config_entry.data.get(CONF_ENERGY_PRICE, 0.0),
            )
        )
        offpeak_price = float(
            self.config_entry.options.get(
                CONF_OFFPEAK_PRICE,
                self.config_entry.data.get(CONF_OFFPEAK_PRICE, 0.0),
            )
        )

        if not is_backfill and last_cost_start is None:
            try:
                stats = await self.hass.async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1000,
                    self.cost_statistic_id,
                    True,
                    {"sum", "state"},
                )
                if self.cost_statistic_id in stats and stats[self.cost_statistic_id]:
                    stat_list = stats[self.cost_statistic_id]
                    sorted_stats = sorted(
                        stat_list,
                        key=lambda s: _parse_statistic_start(s.get("start")),
                        reverse=True,
                    )
                    chosen = sorted_stats[0]
                    last_cost_sum = float(chosen.get("sum", 0.0) or 0.0)
                    p_start = _parse_statistic_start(chosen.get("start"))
                    if p_start != datetime.min.replace(tzinfo=timezone.utc):
                        last_cost_start = p_start
            except Exception as err:
                _LOGGER.warning("Could not query last cost statistics from recorder: %s", err)

        cost_statistics = calculate_hourly_costs(
            readings=readings,
            tariff_model=tariff_model,
            base_price_year=base_price_year,
            energy_price=energy_price,
            offpeak_price=offpeak_price,
            last_cost_sum=last_cost_sum,
            last_cost_start=last_cost_start,
        )

        if cost_statistics:
            cost_metadata_kwargs: dict[str, Any] = {
                "has_mean": False,
                "has_sum": True,
                "name": f"IKB {self.meter.meter_number} Kosten",
                "source": DOMAIN,
                "statistic_id": self.cost_statistic_id,
                "unit_of_measurement": "EUR",
            }
            if StatisticMeanType is not None:
                cost_metadata_kwargs["mean_type"] = StatisticMeanType.NONE

            try:
                cost_metadata = StatisticMetaData(**cost_metadata_kwargs)
            except TypeError:
                cost_metadata_kwargs.pop("mean_type", None)
                cost_metadata = StatisticMetaData(**cost_metadata_kwargs)

            last_cost_val = (
                cost_statistics[-1]["sum"]
                if isinstance(cost_statistics[-1], dict)
                else getattr(cost_statistics[-1], "sum", 0.0)
            )
            _LOGGER.info(
                "Inserting %d cost statistics points for %s (new cumulative sum: %.3f EUR)",
                len(cost_statistics),
                self.cost_statistic_id,
                last_cost_val,
            )
            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)


def calculate_hourly_costs(
    readings: list[Reading],
    tariff_model: str,
    base_price_year: float,
    energy_price: float,
    offpeak_price: float,
    last_cost_sum: float = 0.0,
    last_cost_start: datetime | None = None,
) -> list[StatisticData]:
    """Calculate hourly monetary costs (EUR) from readings based on generic tariff model."""
    positive_readings = [r for r in readings if r.kwh > 0.0]
    if not positive_readings:
        return []

    max_valid_hour = positive_readings[-1].start_datetime.replace(
        minute=0, second=0, microsecond=0
    )

    # Base price per hour = base_price_year / 8760 hours/year
    base_price_hourly = (base_price_year / 8760.0) if base_price_year > 0 else 0.0

    hourly_readings: dict[datetime, list[Reading]] = {}
    for r in readings:
        h_start = r.start_datetime.replace(minute=0, second=0, microsecond=0)
        if h_start > max_valid_hour:
            continue
        hourly_readings.setdefault(h_start, []).append(r)

    statistics: list[StatisticData] = []
    running_sum = last_cost_sum

    for h_start in sorted(hourly_readings.keys()):
        if last_cost_start is not None and h_start <= last_cost_start:
            continue

        r_list = hourly_readings[h_start]
        hour_cost = base_price_hourly

        for r in r_list:
            active_price = energy_price
            if tariff_model == TARIFF_MODEL_DUAL_SUMMER_NOON:
                # Local time check for Austria/Central Europe: April 1 to Sept 30, between 10:00 and 16:00
                r_local = r.start_datetime.astimezone(dt_util.DEFAULT_TIME_ZONE)
                if 4 <= r_local.month <= 9 and 10 <= r_local.hour < 16:
                    active_price = offpeak_price if offpeak_price > 0 else energy_price

            hour_cost += r.kwh * active_price

        running_sum += hour_cost
        statistics.append(
            StatisticData(
                start=h_start,
                state=round(hour_cost, 4),
                sum=round(running_sum, 4),
            )
        )

    return statistics


def aggregate_readings_to_hourly_statistics(
    readings: list[Reading],
    last_stat_sum: float = 0.0,
    last_stat_start: datetime | None = None,
) -> list[StatisticData]:
    """Aggregate 15-minute readings into hourly StatisticData points for HA recorder.

    Home Assistant requires all external statistics timestamps to be aligned to
    the top of the hour (minute=0, second=0, microsecond=0).

    Trailing zero-placeholder intervals resulting from IKB portal delivery delays
    are discarded so they do not pollute long-term statistics with unverified 0.0 values.
    """
    positive_readings = [r for r in readings if r.kwh > 0.0]
    if not positive_readings:
        return []

    max_valid_hour = positive_readings[-1].start_datetime.replace(
        minute=0, second=0, microsecond=0
    )

    hourly_buckets: dict[datetime, float] = {}
    for r in readings:
        h_start = r.start_datetime.replace(minute=0, second=0, microsecond=0)
        if h_start > max_valid_hour:
            continue
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

