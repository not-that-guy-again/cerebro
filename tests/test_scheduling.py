from __future__ import annotations

import pytest

from cerebro.runtime.scheduling import (
    Cadence,
    ScheduleParseError,
    ScheduleSpec,
    Weekday,
)


def test_parse_hourly() -> None:
    spec = ScheduleSpec.parse("hourly")
    assert spec.cadence is Cadence.HOURLY
    assert spec.minute == 0
    assert spec.hour is None
    assert spec.day_of_week is None


def test_parse_daily() -> None:
    spec = ScheduleSpec.parse("daily@06:30")
    assert spec.cadence is Cadence.DAILY
    assert spec.hour == 6
    assert spec.minute == 30
    assert spec.day_of_week is None


def test_parse_daily_single_digit_hour() -> None:
    spec = ScheduleSpec.parse("daily@9:05")
    assert spec.hour == 9
    assert spec.minute == 5


def test_parse_weekly() -> None:
    spec = ScheduleSpec.parse("weekly:mon@09:00")
    assert spec.cadence is Cadence.WEEKLY
    assert spec.hour == 9
    assert spec.minute == 0
    assert spec.day_of_week is Weekday.MON


def test_parse_weekly_case_insensitive() -> None:
    spec = ScheduleSpec.parse("weekly:FRI@17:30")
    assert spec.day_of_week is Weekday.FRI


@pytest.mark.parametrize(
    ("dow_name", "weekday"),
    [
        ("mon", Weekday.MON),
        ("tue", Weekday.TUE),
        ("wed", Weekday.WED),
        ("thu", Weekday.THU),
        ("fri", Weekday.FRI),
        ("sat", Weekday.SAT),
        ("sun", Weekday.SUN),
    ],
)
def test_parse_weekly_all_days(dow_name: str, weekday: Weekday) -> None:
    spec = ScheduleSpec.parse(f"weekly:{dow_name}@12:00")
    assert spec.day_of_week is weekday


def test_parse_monthly_last_business_day() -> None:
    spec = ScheduleSpec.parse("monthly:last-business-day@23:00")
    assert spec.cadence is Cadence.MONTHLY_LAST_BUSINESS_DAY
    assert spec.hour == 23
    assert spec.minute == 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_parse_empty() -> None:
    with pytest.raises(ScheduleParseError, match="empty"):
        ScheduleSpec.parse("")


def test_parse_unknown_form() -> None:
    with pytest.raises(ScheduleParseError, match="unrecognized"):
        ScheduleSpec.parse("biennial@06:00")


def test_parse_invalid_time_format() -> None:
    with pytest.raises(ScheduleParseError, match="invalid time"):
        ScheduleSpec.parse("daily@noon")


def test_parse_invalid_time_seconds_present() -> None:
    with pytest.raises(ScheduleParseError, match="invalid time"):
        ScheduleSpec.parse("daily@06:00:00")


def test_parse_hour_out_of_range() -> None:
    with pytest.raises(ScheduleParseError, match="hour out of range"):
        ScheduleSpec.parse("daily@25:00")


def test_parse_minute_out_of_range() -> None:
    with pytest.raises(ScheduleParseError, match="minute out of range"):
        ScheduleSpec.parse("daily@06:60")


def test_parse_weekly_unknown_dow() -> None:
    with pytest.raises(ScheduleParseError, match="unknown day-of-week"):
        ScheduleSpec.parse("weekly:funday@09:00")


def test_parse_weekly_missing_time() -> None:
    with pytest.raises(ScheduleParseError, match="missing time-of-day"):
        ScheduleSpec.parse("weekly:mon")


def test_parse_monthly_unsupported_kind() -> None:
    with pytest.raises(ScheduleParseError, match="unsupported monthly cadence"):
        ScheduleSpec.parse("monthly:first-day@06:00")


def test_parse_monthly_missing_time() -> None:
    with pytest.raises(ScheduleParseError, match="missing time-of-day"):
        ScheduleSpec.parse("monthly:last-business-day")
