"""Do the travel-mode speed tests actually pin the mode?

Written after the fix, so the risk is that they pass for an unrelated reason. This restores the
old derivation — speed from ``profile.pace``, ``travel_mode`` never read — and requires the new
suite to fail.

It also mutates each speed constant, the disclosure text, and the pace default, because a test
that reads a constant from the module under test can be satisfied by any value the module holds.

Two lessons from the earlier harnesses are applied here, because both cost time:

  * ``target`` is an explicit path per case, not a substring match on the case *name*. The first
    version guessed the file by looking for "derive from pace" in a name that said "stops
    deriving from pace", found nothing, and reported the case as unapplied — the string it was
    looking for was present all along.
  * every case restores all files before running, and the baseline is asserted first. A harness
    that only reports "caught" cannot distinguish a working guard from a working harness.
"""

from __future__ import annotations

from _mutation_harness import ROOT, Case, run_harness

FILES = (
    ROOT / "apps/api/src/travel_api/services/route_optimizer.py",
    ROOT / "apps/api/src/travel_api/providers/base.py",
    ROOT / "apps/api/src/travel_api/services/aggregation.py",
)
OPTIMIZER, BASE, AGGREGATION = FILES
TESTS = "tests/unit/test_travel_mode_speed.py"

OLD_DERIVE = """def _derive_minutes(straight_line_meters: float, profile: TripProfile) -> int:
    road_meters = straight_line_meters * _ROAD_DETOUR_FACTOR
    speed, _ = assumed_speed_for(profile)
    return max(1, ceil(road_meters / speed))"""

#: (name, file, old, new)
MUTATIONS = [
    (
        "M1 the original defect: the speed follows the pace, not the mode",
        OPTIMIZER,
        OLD_DERIVE,
        """def _derive_minutes(straight_line_meters: float, profile: TripProfile) -> int:
    road_meters = straight_line_meters * _ROAD_DETOUR_FACTOR
    speed = (
        _WALKING_METERS_PER_MINUTE
        if profile.pace in (Pace.RELAXED, Pace.BALANCED)
        else _DRIVING_METERS_PER_MINUTE
    )
    return max(1, ceil(road_meters / speed))""",
    ),
    (
        "M2 transit is assumed to be as slow as walking",
        OPTIMIZER,
        "_TRANSIT_METERS_PER_MINUTE = 250.0  # ~15 km/h",
        "_TRANSIT_METERS_PER_MINUTE = 75.0  # ~15 km/h",
    ),
    (
        "M3 the assumed speed ignores the mode and always uses walking",
        OPTIMIZER,
        "    mode = resolve_travel_mode(profile)\n"
        "    speed = _ASSUMED_SPEED_BY_MODE.get(mode, _DRIVING_METERS_PER_MINUTE)",
        "    mode = resolve_travel_mode(profile)\n    speed = _WALKING_METERS_PER_MINUTE",
    ),
    (
        "M4 the disclosure keeps claiming 按距离估算 without naming the speed",
        OPTIMIZER,
        'conflicts.append(f"{estimated_legs} 段路程按距离估算（{speed_text}），不是实测路线")',
        'conflicts.append(f"{estimated_legs} 段路程按距离估算，不是实测路线")',
    ),
    (
        "M5 an unset mode stops deriving from pace (RELAXED now means drive)",
        BASE,
        "    return TravelMode.WALK if profile.pace in (Pace.RELAXED, Pace.BALANCED) "
        "else TravelMode.DRIVE",
        "    return TravelMode.DRIVE",
    ),
    (
        "M6 the disclosure ignores the mode and always says walking",
        OPTIMIZER,
        '    return int(speed), _ASSUMED_SPEED_TEXT.get(mode, "按驾车速度估算")',
        '    return int(speed), "步行约 4.5 km/h"',
    ),
]


CASES = [Case(name, target, old, new) for name, target, old, new in MUTATIONS]

if __name__ == "__main__":
    raise SystemExit(run_harness(files=FILES, cases=CASES, test_targets=TESTS))
