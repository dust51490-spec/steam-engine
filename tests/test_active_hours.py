"""The window math of the active hours (active_hours.py): open or not at a wall clock time, a window
across midnight, start == end, the next start (strictly later, by the local clock, also on the days
daylight saving time begins and ends) and the times of the settings page. No Home Assistant
instance needed; the behaviour of the jobs inside and outside the window is in test_jobs_power.py."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.steam_engine.active_hours import is_open, next_at, parse_time

BERLIN = ZoneInfo("Europe/Berlin")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 10, hour, minute, tzinfo=BERLIN)


def test_inside_and_outside() -> None:
    start, end = time(8, 0), time(20, 0)
    assert not is_open(start, end, _at(7, 59))
    assert is_open(start, end, _at(8, 0))  # the start belongs to the window
    assert is_open(start, end, _at(12, 0))
    assert is_open(start, end, _at(19, 59))
    assert not is_open(start, end, _at(20, 0))  # the end does not
    assert not is_open(start, end, _at(22, 0))


def test_window_across_midnight() -> None:
    start, end = time(22, 0), time(6, 0)
    assert is_open(start, end, _at(22, 0))
    assert is_open(start, end, _at(23, 30))
    assert is_open(start, end, _at(0, 0))
    assert is_open(start, end, _at(5, 59))
    assert not is_open(start, end, _at(6, 0))
    assert not is_open(start, end, _at(12, 0))
    assert not is_open(start, end, _at(21, 59))


def test_start_equal_to_end_is_all_day() -> None:
    for hour in (0, 8, 13, 23):
        assert is_open(time(8, 0), time(8, 0), _at(hour))


def test_next_start() -> None:
    # The example of the feature: an update at 22:00 starts at 08:00 the next day
    assert next_at(time(8, 0), _at(22, 0), BERLIN) == datetime(2026, 6, 11, 6, 0, tzinfo=UTC)
    # Later the same day
    assert next_at(time(8, 0), _at(3, 0), BERLIN) == datetime(2026, 6, 10, 6, 0, tzinfo=UTC)
    # Strictly later: at the start itself the next one is a day ahead
    assert next_at(time(8, 0), _at(8, 0), BERLIN) == datetime(2026, 6, 11, 6, 0, tzinfo=UTC)
    # Across midnight: the end of 22:00-06:00 seen at 23:00
    assert next_at(time(6, 0), _at(23, 0), BERLIN) == datetime(2026, 6, 11, 4, 0, tzinfo=UTC)
    # A time of another zone (UTC) is read by the clock of the given zone
    assert next_at(time(8, 0), datetime(2026, 6, 10, 20, 0, tzinfo=UTC), BERLIN) == datetime(
        2026, 6, 11, 6, 0, tzinfo=UTC
    )


def test_next_start_on_the_days_daylight_saving_time_changes() -> None:
    # Spring (29 March 2026, 02:00 -> 03:00): from 21:00 the day before only 10 hours to 08:00
    spring_eve = datetime(2026, 3, 28, 21, 0, tzinfo=BERLIN)
    start = next_at(time(8, 0), spring_eve, BERLIN)
    assert start == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)
    assert start.astimezone(BERLIN).hour == 8
    assert start - spring_eve == timedelta(hours=10)
    # Autumn (25 October 2026, 03:00 -> 02:00): 12 hours
    autumn_eve = datetime(2026, 10, 24, 21, 0, tzinfo=BERLIN)
    start = next_at(time(8, 0), autumn_eve, BERLIN)
    assert start == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)
    assert start.astimezone(BERLIN).hour == 8
    assert start - autumn_eve == timedelta(hours=12)
    # A time in the skipped hour moves on by that hour (02:30 is 03:30 summer time)
    gap = next_at(time(2, 30), datetime(2026, 3, 29, 0, 0, tzinfo=BERLIN), BERLIN)
    assert gap == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    # A time in the repeated hour counts at its first occurrence (02:30 summer time)
    twice = next_at(time(2, 30), datetime(2026, 10, 25, 0, 0, tzinfo=BERLIN), BERLIN)
    assert twice == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    # The day after a change, the usual offset again
    assert next_at(time(8, 0), datetime(2026, 3, 29, 9, 0, tzinfo=BERLIN), BERLIN) == datetime(
        2026, 3, 30, 6, 0, tzinfo=UTC
    )


def test_times_of_the_settings_page() -> None:
    assert parse_time("08:00:00", "20:00:00") == time(8, 0)
    assert parse_time("22:30", "20:00:00") == time(22, 30)
    assert parse_time("", "20:00:00") == time(20, 0)
    assert parse_time(None, "20:00:00") == time(20, 0)
    assert parse_time("late", "20:00:00") == time(20, 0)
