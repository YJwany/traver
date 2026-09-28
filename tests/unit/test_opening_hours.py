"""Unit tests for the OSM opening_hours parser.

The parser must never invent business hours: anything it cannot fully
understand has to come back as an empty list.
"""

from datetime import time

import pytest
from travel_api.providers.free.opening_hours import parse_opening_hours


def _window(value: str, weekday: int):
    return next(item for item in parse_opening_hours(value) if item.weekday == weekday)


def test_24_7_covers_every_weekday():
    windows = parse_opening_hours("24/7")
    assert len(windows) == 7
    assert {item.weekday for item in windows} == set(range(7))


def test_plain_time_range_applies_to_all_days():
    windows = parse_opening_hours("08:30-17:30")
    assert len(windows) == 7
    assert all(item.opens_at == time(8, 30) and item.closes_at == time(17, 30) for item in windows)


def test_weekday_range_is_limited_to_those_days():
    windows = parse_opening_hours("Mo-Fr 09:00-17:00")
    assert sorted(item.weekday for item in windows) == [0, 1, 2, 3, 4]


def test_saturday_only_rule():
    windows = parse_opening_hours("Sa 10:00-14:00")
    assert [item.weekday for item in windows] == [5]
    assert windows[0].opens_at == time(10, 0)


def test_comma_separated_days():
    windows = parse_opening_hours("Mo,We,Fr 09:00-12:00")
    assert sorted(item.weekday for item in windows) == [0, 2, 4]


def test_multiple_ranges_in_one_day():
    windows = parse_opening_hours("08:30-12:00,13:00-17:30")
    monday = [item for item in windows if item.weekday == 0]
    assert len(monday) == 2
    assert monday[0].closes_at == time(12, 0)
    assert monday[1].opens_at == time(13, 0)


def test_multi_rule_string_keeps_each_rule_scoped():
    windows = parse_opening_hours("Mo-Fr 09:00-17:00; Sa 10:00-14:00")
    assert _window("Mo-Fr 09:00-17:00; Sa 10:00-14:00", 0).opens_at == time(9, 0)
    assert _window("Mo-Fr 09:00-17:00; Sa 10:00-14:00", 5).opens_at == time(10, 0)
    assert not [item for item in windows if item.weekday == 6]


def test_wrapping_day_range():
    windows = parse_opening_hours("Fr-Mo 09:00-18:00")
    assert sorted(item.weekday for item in windows) == [0, 4, 5, 6]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "sunrise-sunset",
        "Mo-Fr off",
        "PH 10:00-12:00",
        "Jan-Mar 09:00-17:00",
        "week 1-10 09:00-17:00",
        "10:00-14:00 || 16:00-20:00",
        "Mo-Fr",
        "nonsense",
    ],
)
def test_unparseable_or_non_weekday_rules_are_not_guessed(value):
    assert parse_opening_hours(value) == []


def test_public_holiday_rule_is_dropped_but_weekdays_survive():
    windows = parse_opening_hours("Mo-Fr 09:00-17:00; PH off")
    assert sorted(item.weekday for item in windows) == [0, 1, 2, 3, 4]
