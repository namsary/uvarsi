import datetime
from zoneinfo import ZoneInfo

import pytest

from app import plan_calendar


UTC = datetime.timezone.utc


@pytest.mark.parametrize(
    ("instant", "expected_day", "expected_week"),
    (
        (
            datetime.datetime(2026, 8, 31, 0, 30, tzinfo=ZoneInfo("Europe/Bratislava")),
            datetime.date(2026, 8, 31),
            "2026-08-31",
        ),
        (
            datetime.datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
            datetime.date(2026, 3, 29),
            "2026-03-23",
        ),
        (
            datetime.datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
            datetime.date(2026, 3, 29),
            "2026-03-23",
        ),
        (
            datetime.datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
            datetime.date(2026, 10, 25),
            "2026-10-19",
        ),
        (
            datetime.datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
            datetime.date(2026, 10, 25),
            "2026-10-19",
        ),
    ),
)
def test_bratislava_calendar_uses_local_day_and_monday_across_midnight_and_dst(
        instant, expected_day, expected_week):
    assert plan_calendar.bratislava_day(instant) == expected_day
    assert plan_calendar.bratislava_monday(instant) == expected_week


def test_bratislava_calendar_treats_a_legacy_naive_timestamp_as_a_utc_instant(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    legacy = datetime.datetime(2026, 8, 30, 22, 30)

    assert plan_calendar.utc_instant(legacy) == legacy.replace(tzinfo=UTC)
    assert plan_calendar.bratislava_day(legacy) == datetime.date(2026, 8, 31)
    assert plan_calendar.bratislava_monday(legacy) == "2026-08-31"
