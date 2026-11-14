"""DataUpdateCoordinator for IKB Direkt Smart Meter."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import calendar
import logging
from zoneinfo import ZoneInfo
from typing import Any

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from homeassistant.components.recorder import get_instance
    from homeassistant.const import UnitOfEnergy
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
    import homeassistant.util.dt as dt_util
except ImportError:
    class ConfigEntryAuthFailed(Exception):
        pass
    ConfigEntry = Any  # type: ignore[assignment, misc]
    HomeAssistant = Any  # type: ignore[assignment, misc]
    class UnitOfEnergy:  # type: ignore[no-redef]
        KILO_WATT_HOUR = "kWh"
    class UpdateFailed(Exception):  # type: ignore[no-redef]
        pass
    class DataUpdateCoordinator:  # type: ignore[no-redef, type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.hass = args[0]
            self.data = None
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
        statistics_during_period,
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
            statistics_during_period,
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


from .api import IKBAuthenticationError, IKBDirektClient, MeterPoint, Reading
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
    """Reconcile delivered intervals with the complete persisted hourly history."""

    def __init__(self, hass, client, meter, config_entry) -> None:
        super().__init__(hass, _LOGGER, name=f"IKB Direkt ({meter.counting_point})",
                         update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL))
        self.client = client
        self.meter = meter
        self.config_entry = config_entry
        self.statistic_id = f"{DOMAIN}:{meter.counting_point.lower()}_{meter.reading_type.lower()}"
        self.cost_statistic_id = f"{self.statistic_id}_kosten"

    def _option(self, key, default):
        return self.config_entry.options.get(key, self.config_entry.data.get(key, default))

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.now()
        today = now.date()
        days = self._option(CONF_HISTORICAL_DAYS, DEFAULT_HISTORICAL_DAYS)
        existing = {}
        if HAVE_RECORDER:
            # A failed read must never be mistaken for an empty DB and reset sums.
            try:
                existing = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period, self.hass,
                    datetime(1970, 1, 1, tzinfo=timezone.utc), None,
                    {self.statistic_id, self.cost_statistic_id}, "hour",
                    {"energy": "kWh"}, {"state", "sum"},
                )
            except Exception as err:
                raise UpdateFailed(f"Cannot read IKB statistics: {err}") from err

        old_energy = existing.get(self.statistic_id, [])
        fetch_start = today - timedelta(days=days)
        if old_energy:
            # After a long outage, fill the entire gap as well as the correction window.
            last_start = max(_parse_statistic_start(s["start"]) for s in old_energy)
            fetch_start = min(fetch_start, last_start.astimezone(dt_util.DEFAULT_TIME_ZONE).date())
        try:
            discovered = await self.client.get_meters()
            matching = [m for m in discovered if m.counting_point == self.meter.counting_point
                        and m.reading_type == self.meter.reading_type]
            readings = await self.client.get_load_profile(
                matching or [self.meter], fetch_start, today, RESOLUTION_15MIN)
        except IKBAuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Error communicating with IKB Direkt: {err}") from err

        # Only complete, closed hours up to delivery evidence are eligible.
        delivered = delivered_hourly_readings(readings, now)
        energy = reconcile_statistics(old_energy, delivered)
        if HAVE_RECORDER:
            self._write_changed(self.statistic_id, "kWh", "energy", energy, old_energy)
            model = self._option(CONF_TARIFF_MODEL, TARIFF_MODEL_NONE)
            if model != TARIFF_MODEL_NONE:
                # Rebuild from persisted ENERGY, not a bounded portal window or cost sums.
                # This repairs gaps and applies edited prices consistently to all history.
                hourly = [Reading(int(s["start"].timestamp()),
                                  int(s["start"].timestamp()) + 3600, s["state"])
                          for s in energy]
                costs = calculate_hourly_costs(
                    hourly, model, float(self._option(CONF_BASE_PRICE_YEAR, 0)),
                    float(self._option(CONF_ENERGY_PRICE, 0)),
                    float(self._option(CONF_OFFPEAK_PRICE, 0)),
                    offpeak_start_hour=self._option("offpeak_start_hour", 10),
                    offpeak_end_hour=self._option("offpeak_end_hour", 16),
                    confirmed=True,
                )
                self._write_changed(self.cost_statistic_id, "EUR", None, costs,
                                    existing.get(self.cost_statistic_id, []))

        yesterday = today - timedelta(days=1)
        local_tz = dt_util.DEFAULT_TIME_ZONE
        day_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=local_tz)
        day_end = datetime.combine(today, datetime.min.time(), tzinfo=local_tz)
        yesterday_hours = [s for s in energy
                           if day_start <= s["start"].astimezone(local_tz) < day_end]
        expected_hours = int((day_end.timestamp() - day_start.timestamp()) / 3600)
        # A single positive reading does not prove a full day has arrived.
        yesterday_complete = len(yesterday_hours) == expected_hours
        positive = [r for r in readings if r.kwh > 0 and r.end_datetime <= now]
        latest = max(positive, key=lambda r: r.start_timestamp) if positive else None
        daily = {}
        for s in energy:
            d = s["start"].astimezone(local_tz).date()
            if d < today:
                daily[d] = daily.get(d, 0) + s["state"]
        last_day = max(daily) if daily else None
        return {
            "latest_reading_time": latest.end_datetime.isoformat() if latest else None,
            "latest_reading_kwh": round(latest.kwh, 4) if latest else None,
            "yesterday_consumption_kwh": round(sum(s["state"] for s in yesterday_hours), 3)
                if yesterday_complete else None,
            "yesterday_date": yesterday.isoformat(),
            "yesterday_data_delivered": yesterday_complete,
            "last_reported_date": last_day.isoformat() if last_day else None,
            "last_reported_kwh": round(daily[last_day], 3) if last_day else None,
            "total_imported_kwh": energy[-1]["sum"] if energy else 0.0,
        }

    def _write_changed(self, statistic_id, unit, unit_class, rows, previous):
        old = {_parse_statistic_start(s["start"]): s for s in previous}
        changed = [s for s in rows if s["start"] not in old or any(
            old[s["start"]].get(k) != s[k] for k in ("state", "sum"))]
        if not changed:
            return
        metadata = dict(source=DOMAIN, statistic_id=statistic_id,
                        name=f"IKB {self.meter.meter_number} {self.meter.reading_type}"
                             + (" Kosten" if unit == "EUR" else ""),
                        has_sum=True, unit_of_measurement=unit, unit_class=unit_class)
        if StatisticMeanType is not None:
            metadata["mean_type"] = StatisticMeanType.NONE
        else:
            metadata["has_mean"] = False
        async_add_external_statistics(self.hass, metadata, changed)


def delivered_hourly_readings(readings: list[Reading], now: datetime | None = None) -> dict[datetime, float]:
    """IKB zeros are ambiguous: never confirm trailing zeros by age alone.

    Zero hours preceding a later positive value are treated as delivered. Require
    full interval coverage; later responses can correct every accepted hour.
    """
    now = now or datetime.now(timezone.utc)
    valid = [r for r in readings if r.end_datetime <= now]
    positive = [r for r in valid if r.kwh > 0]
    if not positive:
        return {}
    horizon = max(r.start_datetime for r in positive).replace(minute=0, second=0, microsecond=0)
    buckets = {}
    for r in valid:
        hour = r.start_datetime.replace(minute=0, second=0, microsecond=0)
        if hour <= horizon and hour + timedelta(hours=1) <= now:
            buckets.setdefault(hour, {})[r.start_timestamp] = r
    delivered = {}
    for hour, intervals in buckets.items():
        cursor = int(hour.timestamp())
        for r in sorted(intervals.values(), key=lambda r: r.start_timestamp):
            if r.start_timestamp != cursor or r.end_timestamp <= cursor:
                break
            cursor = r.end_timestamp
        else:
            if cursor == int(hour.timestamp()) + 3600:
                delivered[hour] = sum(r.kwh for r in intervals.values())
    return delivered


def reconcile_statistics(previous, delivered) -> list[StatisticData]:
    """Replace hours and propagate corrections through every subsequent sum."""
    old = sorted(previous, key=lambda s: _parse_statistic_start(s["start"]))
    hours = {_parse_statistic_start(s["start"]): float(s["state"]) for s in old}
    hours.update(delivered)
    # Preserve a pre-existing baseline (e.g. after manual statistics pruning).
    baseline = float(old[0]["sum"]) - float(old[0]["state"]) if old else 0.0
    return _cumulative_statistics(hours, baseline)


def _cumulative_statistics(hours, baseline=0.0):
    result = []
    running = baseline
    for hour, value in sorted(hours.items()):
        running += value
        result.append(StatisticData(start=hour, state=round(value, 4), sum=round(running, 4)))
    return result


def aggregate_readings_to_hourly_statistics(readings, last_stat_sum=0.0, last_stat_start=None):
    """Aggregate only delivered complete hours; coordinator handles reconciliation."""
    hours = delivered_hourly_readings(readings)
    if last_stat_start is not None:
        hours = {h: value for h, value in hours.items() if h > last_stat_start}
    return _cumulative_statistics(hours, last_stat_sum)


def calculate_hourly_costs(readings, tariff_model, base_price_year, energy_price,
                           offpeak_price, last_cost_sum=0.0, last_cost_start=None,
                           *, offpeak_start_hour=10, offpeak_end_hour=16, confirmed=False):
    """Calculate costs in Austrian civil time, including zero-energy confirmed hours."""
    if tariff_model == TARIFF_MODEL_NONE:
        return []
    if confirmed:
        hours = {r.start_datetime: r.kwh for r in readings}
    else:
        hours = delivered_hourly_readings(readings)
    costs = {}
    for hour, kwh in hours.items():
        if last_cost_start is not None and hour <= last_cost_start:
            continue
        local = hour.astimezone(ZoneInfo("Europe/Vienna"))
        price = energy_price
        if tariff_model == TARIFF_MODEL_DUAL_SUMMER_NOON and 4 <= local.month <= 9:
            if offpeak_start_hour <= local.hour < offpeak_end_hour:
                price = offpeak_price
        hours_in_year = (366 if calendar.isleap(local.year) else 365) * 24
        costs[hour] = kwh * price + base_price_year / hours_in_year
    return _cumulative_statistics(costs, last_cost_sum)
