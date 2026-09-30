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

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(r"C:\Users\27875\Desktop\Traver")
OPTIMIZER = ROOT / "apps/api/src/travel_api/services/route_optimizer.py"
BASE = ROOT / "apps/api/src/travel_api/providers/base.py"
AGGREGATION = ROOT / "apps/api/src/travel_api/services/aggregation.py"
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


def run() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS.split(), "-q", "--no-header", "-p", "no:logging"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "apps/api/src"), "PYTHONUTF8": "1"},
    )
    return proc.returncode, proc.stdout[-900:]


def main() -> int:
    paths = (OPTIMIZER, BASE, AGGREGATION)
    originals = {p: p.read_text(encoding="utf-8") for p in paths}

    def restore() -> None:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    restore()
    code, out = run()
    if code != 0:
        print(f"BASELINE FAILS — the tests do not pass on the fixed tree:\n{out}")
        return 1
    print("baseline (fixed tree): all tests pass\n")

    uncaught: list[str] = []
    for name, target, old, new in MUTATIONS:
        restore()
        text = target.read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"NOT APPLIED ({text.count(old)} matches): {name}")
            uncaught.append(name)
            continue
        target.write_text(text.replace(old, new), encoding="utf-8")
        code, _ = run()
        print(f"{'caught' if code != 0 else 'NOT CAUGHT':11} {name}")
        if code == 0:
            uncaught.append(name)

    restore()
    print()
    if uncaught:
        print(f"{len(uncaught)} mutation(s) not accounted for:")
        for name in uncaught:
            print(f"  - {name}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
