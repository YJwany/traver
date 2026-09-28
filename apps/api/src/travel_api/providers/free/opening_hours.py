"""Conservative parser for OpenStreetMap ``opening_hours`` values.

The full OSM opening_hours grammar is large (seasonal rules, week numbers,
sunrise/sunset, public-holiday modifiers, comments). Guessing at an
unsupported expression would invent a business fact, which is exactly what this
project forbids. So this parser handles the common unambiguous forms and
returns an empty list for anything it cannot fully understand.

Supported:
    24/7
    08:30-17:30
    08:30-12:00,13:00-17:30
    Mo-Fr 09:00-17:00
    Mo,We,Fr 09:00-17:00
    Mo-Su 09:00-18:00
    Mo-Fr 09:00-17:00; Sa 10:00-14:00
    Mo-Fr 09:00-17:00; PH off      (the PH rule is dropped, weekdays kept)
"""

from __future__ import annotations

import re
from datetime import time

from travel_api.domain.models import OpeningHours

#: OSM weekday abbreviations, Monday-first to match ``OpeningHours.weekday``.
WEEKDAYS: dict[str, int] = {
    "mo": 0,
    "tu": 1,
    "we": 2,
    "th": 3,
    "fr": 4,
    "sa": 5,
    "su": 6,
}

_TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_DAY_TOKEN_RE = re.compile(r"^(mo|tu|we|th|fr|sa|su)(?:\s*-\s*(mo|tu|we|th|fr|sa|su))?$")
#: Locates where the time part of a rule begins, so everything before it is the
#: day specification. Matching on the time is more robust than taking the first
#: whitespace token, which breaks for lists such as "Mo,We,Fr".
_CLOCK_RE = re.compile(r"\d{1,2}:\d{2}")


def _parse_clock(hour: str, minute: str) -> time | None:
    try:
        parsed = time(int(hour), int(minute))
    except ValueError:
        return None
    # 24:00 is legal in OSM but not representable as a datetime.time.
    return parsed


def _parse_time_ranges(value: str) -> list[tuple[time, time]] | None:
    ranges: list[tuple[time, time]] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _TIME_RANGE_RE.match(chunk)
        if match is None:
            return None
        opens = _parse_clock(match.group(1), match.group(2))
        closes = _parse_clock(match.group(3), match.group(4))
        if opens is None or closes is None:
            return None
        ranges.append((opens, closes))
    return ranges or None


def _parse_days(spec: str) -> list[int] | None:
    days: list[int] = []
    for token in spec.split(","):
        token = token.strip().lower()
        if not token:
            continue
        match = _DAY_TOKEN_RE.match(token)
        if match is None:
            return None
        start = WEEKDAYS[match.group(1)]
        end = WEEKDAYS[match.group(2)] if match.group(2) else start
        if start <= end:
            days.extend(range(start, end + 1))
        else:  # Wrapping range such as Fr-Mo.
            days.extend(range(start, 7))
            days.extend(range(0, end + 1))
    return sorted(set(days)) or None


def parse_opening_hours(value: str | None) -> list[OpeningHours]:
    """Return per-weekday opening windows, or ``[]`` when not fully understood."""
    if not value:
        return []

    text = value.strip()
    if not text:
        return []

    if text.lower() in {"24/7", "24/7 open", "mo-su 00:00-24:00"}:
        return [
            OpeningHours(weekday=day, opens_at=time(0, 0), closes_at=time(23, 59))
            for day in range(7)
        ]

    windows: dict[int, list[tuple[time, time]]] = {}

    for rule in text.split(";"):
        rule = rule.strip().rstrip(",").strip()
        if not rule:
            continue

        lowered = rule.lower()
        # Holiday-only rules carry no weekday information we can trust.
        if lowered.startswith("ph") or lowered.endswith("off"):
            continue

        clock = _CLOCK_RE.search(rule)
        if clock is None:
            # A rule with no clock time (for example a bare day list or
            # "sunrise-sunset") cannot be converted into fixed windows.
            return []

        day_spec = rule[: clock.start()].strip()
        time_spec = rule[clock.start() :].strip()

        days: list[int] | None = None
        if day_spec:
            days = _parse_days(day_spec)
            if days is None:
                return []

        ranges = _parse_time_ranges(time_spec)
        if ranges is None:
            return []

        for day in days if days is not None else range(7):
            windows.setdefault(day, []).extend(ranges)

    if not windows:
        return []

    result: list[OpeningHours] = []
    for day in range(7):
        for opens, closes in windows.get(day, []):
            result.append(OpeningHours(weekday=day, opens_at=opens, closes_at=closes))
    return result
