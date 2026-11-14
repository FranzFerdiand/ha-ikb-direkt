"""Regressions for delayed IKB deliveries, cumulative corrections and tariffs."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.ikb_direkt.api import Reading
from custom_components.ikb_direkt.coordinator import (
    calculate_hourly_costs, delivered_hourly_readings, reconcile_statistics,
)

START = datetime(2024, 6, 1, 8, tzinfo=timezone.utc)


def intervals(hour=START, values=(0.25, 0.25, 0.25, 0.25)):
    ts = int(hour.timestamp())
    return [Reading(ts + i * 900, ts + (i + 1) * 900, v) for i, v in enumerate(values)]


def test_partial_hour_then_delivery_then_correction_is_idempotent():
    first = reconcile_statistics([], delivered_hourly_readings(intervals()[:1]))
    assert first == []
    second = reconcile_statistics(first, delivered_hourly_readings(intervals()))
    assert second[-1]["sum"] == 1
    third = reconcile_statistics(second, delivered_hourly_readings(intervals()))
    assert third == second
    changed = reconcile_statistics(third, delivered_hourly_readings(intervals(values=(.5,) * 4)))
    assert changed[-1]["sum"] == 2


def test_correction_propagates_to_later_hours_and_retains_missing_history():
    old = reconcile_statistics([], {START: 1, START + timedelta(hours=1): 2})
    fixed = reconcile_statistics(old, {START: 3})
    assert [s["sum"] for s in fixed] == [3, 5]
    assert reconcile_statistics(fixed, {}) == fixed


def test_old_partial_hour_is_repaired():
    old = [{"start": START.timestamp(), "state": .25, "sum": .25}]
    fixed = reconcile_statistics(old, delivered_hourly_readings(intervals()))
    assert fixed[-1]["sum"] == 1


def test_placeholder_zero_hours_are_not_confirmed_by_age():
    assert delivered_hourly_readings(intervals(values=(0,) * 4)) == {}
    readings = intervals() + intervals(START + timedelta(hours=1), (0,) * 4)
    assert len(delivered_hourly_readings(readings)) == 1
    readings += intervals(START + timedelta(hours=2))
    assert delivered_hourly_readings(readings)[START + timedelta(hours=1)] == 0


def test_missing_interval_and_unfinished_hour_not_finalized():
    assert delivered_hourly_readings(intervals()[1:]) == {}
    assert delivered_hourly_readings(intervals(), now=START + timedelta(minutes=30)) == {}


def test_cost_backfill_over_1000_hours_and_price_correction():
    readings = [Reading(int((START + timedelta(hours=i)).timestamp()),
                        int((START + timedelta(hours=i + 1)).timestamp()), 1) for i in range(1500)]
    old = calculate_hourly_costs(readings, "fixed", 0, .1, 0, confirmed=True)
    new = calculate_hourly_costs(readings, "fixed", 0, .2, 0, confirmed=True)
    assert len(new) == 1500
    assert old[-1]["sum"] == 150
    assert new[-1]["sum"] == 300


@pytest.mark.parametrize("year,hours", [(2024, 8784), (2025, 8760)])
def test_base_fee_for_confirmed_zero_hour_and_leap_year(year, hours):
    start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    rows = calculate_hourly_costs([Reading(start, start + 3600, 0)], "fixed", hours, .2, 0, confirmed=True)
    assert rows[0]["state"] == 1


@pytest.mark.parametrize("utc_hour,expected", [(7, .2), (8, 0), (13, 0), (14, .2)])
def test_free_offpeak_uses_austrian_timezone(utc_hour, expected):
    start = int(START.replace(hour=utc_hour).timestamp())
    rows = calculate_hourly_costs([Reading(start, start + 3600, 1)], "dual_summer_noon", 0, .2, 0)
    assert rows[0]["state"] == expected


def test_custom_offpeak_window():
    start = int(START.timestamp())  # 10:00 Vienna, outside 12-14.
    rows = calculate_hourly_costs([Reading(start, start + 3600, 1)], "dual_summer_noon", 0, .2, .1,
                                 offpeak_start_hour=12, offpeak_end_hour=14)
    assert rows[0]["state"] == .2
