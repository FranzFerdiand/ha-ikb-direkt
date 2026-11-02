"""Unit tests for IKB Direkt API client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ikb_direkt.api import (
    IKBAuthenticationError,
    IKBDirektClient,
    MeterPoint,
    Reading,
)
from custom_components.ikb_direkt.const import RESOLUTION_15MIN


def test_meter_point_from_option() -> None:
    """Test parsing MeterPoint from HTML option."""
    option_val = json.dumps({
        "LType": "MLI",
        "MecId": "12345",
        "DevId": "67890",
        "ObiId": "61",
        "PeriodBDate": "01.10.2026 00:15",
        "PeriodEDate": "31.10.2026 00:00",
    })
    option_text = "AT0051000000000000000000000000000 - 1KFM0000000000 Verbrauch (IME, 01.10.2026 - 31.10.2026)"

    meter = MeterPoint.from_option(option_val, option_text)

    assert meter.counting_point == "AT0051000000000000000000000000000"
    assert meter.meter_number == "1KFM0000000000"
    assert meter.reading_type == "Verbrauch"
    assert meter.period_start == "01.10.2026 00:15"
    assert meter.period_end == "31.10.2026 00:00"
    assert meter.raw_json == option_val


def test_reading_properties() -> None:
    """Test reading timestamps and properties."""
    # 1792880100 is end, 1792879200 is start (900 seconds diff = 15 mins)
    reading = Reading(
        start_timestamp=1792879200,
        end_timestamp=1792880100,
        kwh=0.013,
    )

    assert reading.kwh == 0.013
    assert reading.start_timestamp == 1792879200
    assert reading.end_timestamp == 1792880100
    assert reading.start_datetime.timestamp() == 1792879200
    assert reading.end_datetime.timestamp() == 1792880100


@pytest.mark.asyncio
async def test_get_load_profile_parsing() -> None:
    """Test parsing load profile series response."""
    meter = MeterPoint(
        counting_point="AT005100...",
        meter_number="1KFM...",
        reading_type="Verbrauch",
        period_start="01.10.2026",
        period_end="31.10.2026",
        raw_json="{}",
        display_name="Test Meter",
    )

    client = IKBDirektClient("user@example.com", "secret")
    client._is_logged_in = True
    client._csrf_token = "mock-csrf"

    fake_response = [
        {
            "Period": "Test",
            "data": [
                [1792880100, 0.013, 1792879200, 1792880100],
                [1792881000, 0.022, 1792880100, 1792881000],
            ],
        }
    ]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=fake_response)

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_resp
    mock_cm.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.get.return_value = mock_cm
    client._session = mock_session

    readings = await client.get_load_profile(
        meter, "25.10.2026", "26.10.2026", RESOLUTION_15MIN
    )

    assert len(readings) == 2
    assert readings[0].kwh == 0.013
    assert readings[0].start_timestamp == 1792879200
    assert readings[1].kwh == 0.022
    assert readings[1].start_timestamp == 1792880100


@pytest.mark.asyncio
async def test_get_meters_parsing() -> None:
    """Test extracting meters from loadprofile ajax content."""
    client = IKBDirektClient("user@example.com", "secret")
    client._is_logged_in = True
    client._csrf_token = "mock-csrf"

    mock_page_resp = AsyncMock()
    mock_page_resp.status = 200

    opt_json = json.dumps({"LType": "MLI", "PeriodBDate": "01.01.2026", "PeriodEDate": "01.11.2026"})
    fake_ajax = {
        "content": f"""
        <div>
            <select id="metCodes">
                <option value='{opt_json}'>AT0051000 - 1KFM999 Verbrauch</option>
            </select>
        </div>
        """
    }
    mock_ajax_resp = AsyncMock()
    mock_ajax_resp.status = 200
    mock_ajax_resp.json = AsyncMock(return_value=fake_ajax)

    def side_effect_get(url, **kwargs):
        cm = AsyncMock()
        if "page=loadprofile&ajax=y" in url:
            cm.__aenter__.return_value = mock_ajax_resp
        else:
            cm.__aenter__.return_value = mock_page_resp
        cm.__aexit__.return_value = None
        return cm

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.get.side_effect = side_effect_get
    client._session = mock_session

    meters = await client.get_meters()
    assert len(meters) == 1
    assert meters[0].counting_point == "AT0051000"
    assert meters[0].meter_number == "1KFM999"
    assert meters[0].reading_type == "Verbrauch"


def test_aggregate_readings_to_hourly_statistics() -> None:
    """Test aggregating 15-minute readings into hourly buckets for HA recorder."""
    from custom_components.ikb_direkt.coordinator import aggregate_readings_to_hourly_statistics
    from datetime import datetime, timezone

    # 4 readings in the 10:00 - 11:00 hour, 2 in 11:00 - 12:00
    base_ts = 1700000000  # arbitrary timestamp
    # Align to a known hour
    base_dt = datetime.fromtimestamp(base_ts, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    start_ts = int(base_dt.timestamp())

    readings = [
        # Hour 0
        Reading(start_timestamp=start_ts, end_timestamp=start_ts + 900, kwh=0.25),
        Reading(start_timestamp=start_ts + 900, end_timestamp=start_ts + 1800, kwh=0.25),
        Reading(start_timestamp=start_ts + 1800, end_timestamp=start_ts + 2700, kwh=0.25),
        Reading(start_timestamp=start_ts + 2700, end_timestamp=start_ts + 3600, kwh=0.25),
        # Hour 1
        Reading(start_timestamp=start_ts + 3600, end_timestamp=start_ts + 4500, kwh=0.50),
        Reading(start_timestamp=start_ts + 4500, end_timestamp=start_ts + 5400, kwh=0.50),
    ]

    stats = aggregate_readings_to_hourly_statistics(readings, last_stat_sum=10.0)

    assert len(stats) == 2
    # Verify top-of-the-hour alignment (StatisticData is a TypedDict/dict)
    s0_start = stats[0]["start"] if isinstance(stats[0], dict) else stats[0].start
    s0_state = stats[0]["state"] if isinstance(stats[0], dict) else stats[0].state
    s0_sum = stats[0]["sum"] if isinstance(stats[0], dict) else stats[0].sum
    assert s0_start.minute == 0
    assert s0_start.second == 0
    assert s0_start.microsecond == 0
    assert s0_state == 1.0  # 4 * 0.25
    assert s0_sum == 11.0  # 10.0 + 1.0

    s1_start = stats[1]["start"] if isinstance(stats[1], dict) else stats[1].start
    s1_state = stats[1]["state"] if isinstance(stats[1], dict) else stats[1].state
    s1_sum = stats[1]["sum"] if isinstance(stats[1], dict) else stats[1].sum
    assert s1_start.minute == 0
    assert s1_start.second == 0
    assert s1_state == 1.0  # 2 * 0.50
    assert s1_sum == 12.0  # 11.0 + 1.0


def test_trailing_zeros_filtered_in_statistics() -> None:
    """Test that trailing zero intervals from portal delay are filtered out."""
    from custom_components.ikb_direkt.coordinator import aggregate_readings_to_hourly_statistics
    from datetime import datetime, timezone

    base_ts = 1700000000
    base_dt = datetime.fromtimestamp(base_ts, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    start_ts = int(base_dt.timestamp())

    readings = [
        # Hour 0 (valid)
        Reading(start_timestamp=start_ts, end_timestamp=start_ts + 900, kwh=0.25),
        Reading(start_timestamp=start_ts + 900, end_timestamp=start_ts + 1800, kwh=0.25),
        Reading(start_timestamp=start_ts + 1800, end_timestamp=start_ts + 2700, kwh=0.25),
        Reading(start_timestamp=start_ts + 2700, end_timestamp=start_ts + 3600, kwh=0.25),
        # Hour 1 (valid)
        Reading(start_timestamp=start_ts + 3600, end_timestamp=start_ts + 4500, kwh=0.50),
        Reading(start_timestamp=start_ts + 4500, end_timestamp=start_ts + 5400, kwh=0.50),
        Reading(start_timestamp=start_ts + 5400, end_timestamp=start_ts + 6300, kwh=0.00),
        Reading(start_timestamp=start_ts + 6300, end_timestamp=start_ts + 7200, kwh=0.00),
        # Hour 2 (trailing unread dummy zeros from portal delay)
        Reading(start_timestamp=start_ts + 7200, end_timestamp=start_ts + 8100, kwh=0.00),
        Reading(start_timestamp=start_ts + 8100, end_timestamp=start_ts + 9000, kwh=0.00),
        Reading(start_timestamp=start_ts + 9000, end_timestamp=start_ts + 9900, kwh=0.00),
        Reading(start_timestamp=start_ts + 9900, end_timestamp=start_ts + 10800, kwh=0.00),
    ]

    stats = aggregate_readings_to_hourly_statistics(readings, last_stat_sum=10.0)

    # Hour 2 must NOT be included in statistics
    assert len(stats) == 2
    s1_sum = stats[1]["sum"] if isinstance(stats[1], dict) else stats[1].sum
    assert s1_sum == 12.0


def test_all_zeros_returns_empty_statistics() -> None:
    """Test that if all readings are zero, no statistics are generated."""
    from custom_components.ikb_direkt.coordinator import aggregate_readings_to_hourly_statistics

    readings = [
        Reading(start_timestamp=1700000000, end_timestamp=1700000900, kwh=0.0),
        Reading(start_timestamp=1700000900, end_timestamp=1700001800, kwh=0.0),
    ]

    stats = aggregate_readings_to_hourly_statistics(readings, last_stat_sum=10.0)
    assert len(stats) == 0


def test_calculate_hourly_costs_fixed() -> None:
    """Test cost calculation with fixed tariff model."""
    from custom_components.ikb_direkt.coordinator import calculate_hourly_costs
    from datetime import datetime, timezone

    base_ts = 1700000000
    base_dt = datetime.fromtimestamp(base_ts, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    start_ts = int(base_dt.timestamp())

    readings = [
        Reading(start_timestamp=start_ts, end_timestamp=start_ts + 900, kwh=1.0),
        Reading(start_timestamp=start_ts + 900, end_timestamp=start_ts + 1800, kwh=1.0),
        Reading(start_timestamp=start_ts + 1800, end_timestamp=start_ts + 2700, kwh=1.0),
        Reading(start_timestamp=start_ts + 2700, end_timestamp=start_ts + 3600, kwh=1.0),
    ]

    # 4 kWh * 0.12 EUR/kWh = 0.48 EUR, base price 0 EUR
    costs = calculate_hourly_costs(
        readings=readings,
        tariff_model="fixed",
        base_price_year=0.0,
        energy_price=0.12,
        offpeak_price=0.0,
        last_cost_sum=5.0,
    )

    assert len(costs) == 1
    c0_state = costs[0]["state"] if isinstance(costs[0], dict) else costs[0].state
    c0_sum = costs[0]["sum"] if isinstance(costs[0], dict) else costs[0].sum
    assert c0_state == 0.48
    assert c0_sum == 5.48


def test_calculate_hourly_costs_dual_summer_noon() -> None:
    """Test cost calculation with dual summer noon offpeak tariff."""
    from custom_components.ikb_direkt.coordinator import calculate_hourly_costs
    from datetime import datetime, timezone

    # 2026-06-15 11:00 UTC (June = month 6, summer period, 11:00 = noon interval)
    dt_summer_noon = datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)
    ts = int(dt_summer_noon.timestamp())

    readings = [
        Reading(start_timestamp=ts, end_timestamp=ts + 3600, kwh=10.0),
    ]

    costs = calculate_hourly_costs(
        readings=readings,
        tariff_model="dual_summer_noon",
        base_price_year=0.0,
        energy_price=0.12,
        offpeak_price=0.06,
        last_cost_sum=0.0,
    )

    assert len(costs) == 1
    c0_state = costs[0]["state"] if isinstance(costs[0], dict) else costs[0].state
    # 10 kWh * 0.06 offpeak = 0.60 EUR
    assert c0_state == 0.60


def test_parse_statistic_start_helper() -> None:
    """Test helper parsing various statistic start formats."""
    from custom_components.ikb_direkt.coordinator import _parse_statistic_start
    from datetime import datetime, timezone

    # int / float timestamp
    dt = _parse_statistic_start(1700000000)
    assert dt.tzinfo is not None
    assert int(dt.timestamp()) == 1700000000

    # naive datetime gets UTC
    dt_naive = datetime(2026, 10, 1, 12, 0)
    dt_res = _parse_statistic_start(dt_naive)
    assert dt_res.tzinfo == timezone.utc

    # aware datetime preserved
    dt_aware = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert _parse_statistic_start(dt_aware) == dt_aware


def test_calculate_hourly_costs_with_base_price() -> None:
    """Test that annual base price is correctly distributed across hours."""
    from custom_components.ikb_direkt.coordinator import calculate_hourly_costs
    from datetime import datetime, timezone

    base_ts = 1700000000
    base_dt = datetime.fromtimestamp(base_ts, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    start_ts = int(base_dt.timestamp())

    readings = [
        Reading(start_timestamp=start_ts, end_timestamp=start_ts + 3600, kwh=0.5),
    ]

    # base_price_year = 87.60 EUR -> 0.01 EUR/hour base + 0.5 kWh * 0.20 EUR = 0.10 EUR -> 0.11 EUR total
    costs = calculate_hourly_costs(
        readings=readings,
        tariff_model="fixed",
        base_price_year=87.60,
        energy_price=0.20,
        offpeak_price=0.0,
        last_cost_sum=0.0,
    )

    assert len(costs) == 1
    c0_state = costs[0]["state"] if isinstance(costs[0], dict) else costs[0].state
    c0_sum = costs[0]["sum"] if isinstance(costs[0], dict) else costs[0].sum
    assert c0_state == 0.11
    assert c0_sum == 0.11



