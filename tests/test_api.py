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
    option_text = "AT0051000602000000000000000042791 - 1KFM0200317655 Verbrauch (IME, 01.10.2026 - 31.10.2026)"

    meter = MeterPoint.from_option(option_val, option_text)

    assert meter.counting_point == "AT0051000602000000000000000042791"
    assert meter.meter_number == "1KFM0200317655"
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
    # Verify top-of-the-hour alignment
    assert stats[0].start.minute == 0
    assert stats[0].start.second == 0
    assert stats[0].start.microsecond == 0
    assert stats[0].state == 1.0  # 4 * 0.25
    assert stats[0].sum == 11.0  # 10.0 + 1.0

    assert stats[1].start.minute == 0
    assert stats[1].start.second == 0
    assert stats[1].state == 1.0  # 2 * 0.50
    assert stats[1].sum == 12.0  # 11.0 + 1.0

