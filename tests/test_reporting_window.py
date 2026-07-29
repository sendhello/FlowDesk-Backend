"""Reporting window arithmetic (UC-10; US-15).

Pure unit tests: no database, no fixtures. They run even when TEST_DATABASE_URL is unset,
like tests/test_jwt.py. The timezone/calendar logic is the riskiest part of Sprint 3 and
its failure mode is silent, so it must be testable without infrastructure.

Dates are pinned to 2026 so the assertions never drift with the calendar.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import ValidationError
from app.services import analytics_service
from app.services.analytics_service import (
    DEFAULT_WEEKS,
    MAX_WEEKS,
    reporting_tz,
    resolve_window,
    today_local,
    week_series,
    week_start,
    window_bounds,
)

MELBOURNE = ZoneInfo("Australia/Melbourne")


# ---- week_start -----------------------------------------------------------------


def test_week_start_of_a_monday_is_itself():
    assert week_start(date(2026, 7, 27)) == date(2026, 7, 27)  # Monday


def test_week_start_of_a_tuesday_is_the_previous_monday():
    assert week_start(date(2026, 7, 28)) == date(2026, 7, 27)


def test_week_start_of_a_sunday_is_the_previous_monday():
    """ISO weeks end on Sunday, so a Sunday belongs to the week that began six days ago —
    this is the case a naive "start of week = Sunday" implementation gets wrong."""
    assert week_start(date(2026, 8, 2)) == date(2026, 7, 27)


def test_week_start_crosses_the_year_boundary():
    """2026-01-01 is a Thursday, so its week began in the previous year."""
    assert week_start(date(2026, 1, 1)) == date(2025, 12, 29)


def test_week_start_is_always_a_monday():
    day = date(2026, 3, 1)
    for offset in range(40):
        assert week_start(day + timedelta(days=offset)).weekday() == 0


# ---- resolve_window -------------------------------------------------------------


def test_default_window_is_twelve_weeks():
    week_from, week_to = resolve_window(None, None)

    assert week_series(week_from, week_to) == week_series(week_from, week_to)
    assert (week_to - week_from).days // 7 + 1 == DEFAULT_WEEKS


def test_default_window_ends_in_the_current_local_week():
    _, week_to = resolve_window(None, None)

    assert week_to == week_start(today_local())


def test_from_only_defaults_to_today(monkeypatch):
    week_from, week_to = resolve_window(date(2026, 7, 1), None)

    assert week_from == date(2026, 6, 29)  # Monday of the week containing 1 July
    assert week_to == week_start(today_local())


def test_to_only_gets_a_twelve_week_window():
    week_from, week_to = resolve_window(None, date(2026, 7, 29))

    assert week_to == date(2026, 7, 27)
    assert (week_to - week_from).days // 7 + 1 == DEFAULT_WEEKS


def test_from_snaps_back_to_its_monday():
    """Otherwise the first bar is silently partial and the trend lies."""
    week_from, _ = resolve_window(date(2026, 7, 30), date(2026, 8, 5))

    assert week_from == date(2026, 7, 27)


def test_to_snaps_forward_to_its_own_weeks_monday():
    """`to` snaps to the Monday of ITS week, so the last week is included whole."""
    _, week_to = resolve_window(date(2026, 7, 1), date(2026, 7, 30))

    assert week_to == date(2026, 7, 27)


def test_from_after_to_is_rejected():
    with pytest.raises(ValidationError) as exc:
        resolve_window(date(2026, 8, 1), date(2026, 7, 1))

    assert exc.value.details["reason"] == "invalid_date_range"
    assert exc.value.status_code == 422


def test_range_over_the_cap_is_rejected():
    """Without this, `?from=1000-01-01` is a valid request for tens of thousands of
    buckets."""
    week_to = date(2026, 7, 27)
    too_far = week_to - timedelta(weeks=MAX_WEEKS)

    with pytest.raises(ValidationError) as exc:
        resolve_window(too_far, week_to)

    assert exc.value.details["reason"] == "date_range_too_large"
    assert exc.value.details["max_weeks"] == MAX_WEEKS


def test_range_of_exactly_the_cap_is_accepted():
    week_to = date(2026, 7, 27)
    week_from = week_to - timedelta(weeks=MAX_WEEKS - 1)

    resolved_from, resolved_to = resolve_window(week_from, week_to)

    assert len(week_series(resolved_from, resolved_to)) == MAX_WEEKS


def test_same_day_range_is_one_week():
    week_from, week_to = resolve_window(date(2026, 7, 29), date(2026, 7, 29))

    assert week_from == week_to == date(2026, 7, 27)


# ---- window_bounds --------------------------------------------------------------


def test_lower_bound_is_local_midnight():
    """Monday 00:00 Melbourne is 14:00 UTC the previous day in AEST (UTC+10)."""
    lower, _ = window_bounds(date(2026, 7, 27), date(2026, 7, 27))

    assert lower == datetime(2026, 7, 27, 0, 0, tzinfo=MELBOURNE)
    assert lower.utcoffset() == timedelta(hours=10)


def test_upper_bound_is_exclusive_and_one_week_past_week_to():
    """Half-open, so an incident at exactly midnight is never counted twice."""
    _, upper = window_bounds(date(2026, 7, 27), date(2026, 7, 27))

    assert upper == datetime(2026, 8, 3, 0, 0, tzinfo=MELBOURNE)


def test_bounds_span_a_dst_transition_with_different_offsets():
    """Melbourne leaves daylight saving on 2026-04-05. A window crossing it must carry the
    correct UTC offset at EACH edge, which is what tz-aware arithmetic buys us."""
    lower, upper = window_bounds(date(2026, 3, 30), date(2026, 4, 6))

    assert lower.utcoffset() == timedelta(hours=11)  # AEDT
    assert upper.utcoffset() == timedelta(hours=10)  # AEST


# ---- week_series ----------------------------------------------------------------


def test_week_series_length_and_order():
    series = week_series(date(2026, 7, 6), date(2026, 7, 27))

    assert series == [
        date(2026, 7, 6),
        date(2026, 7, 13),
        date(2026, 7, 20),
        date(2026, 7, 27),
    ]


def test_week_series_entries_are_all_mondays():
    series = week_series(date(2026, 1, 5), date(2026, 12, 28))

    assert all(day.weekday() == 0 for day in series)
    assert len(set(series)) == len(series)


def test_week_series_of_a_single_week():
    assert week_series(date(2026, 7, 27), date(2026, 7, 27)) == [date(2026, 7, 27)]


# ---- reporting_tz ---------------------------------------------------------------


def test_reporting_tz_defaults_to_melbourne():
    assert reporting_tz() == MELBOURNE


def test_reporting_tz_follows_the_setting(monkeypatch):
    """Proves the setting is actually wired, not just declared."""
    monkeypatch.setattr(analytics_service.settings, "reporting_timezone", "UTC")

    assert reporting_tz() == ZoneInfo("UTC")
