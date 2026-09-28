"""Mutation probes for the refresh-job work.

Each mutation is applied, the tests are run, and the file is restored. A mutation that
survives means the tests are not actually pinning the behaviour.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVICE = ROOT / "apps/api/src/travel_api/services/trip_service.py"
ROUTES = ROOT / "apps/api/src/travel_api/api/routes.py"

MUTATIONS = [
    (
        "owner filter dropped from the pre-job trip lookup",
        SERVICE,
        "        trip = self.get(trip_id, owner_id=owner_id)\n        strategy =",
        "        trip = self.get(trip_id)\n        strategy =",
    ),
    (
        "job kind reported as a plain plan",
        SERVICE,
        "            kind=JobKind.REFRESH,",
        "            kind=JobKind.PLAN,",
    ),
    (
        "target_trip_id dropped, so a client cannot tell what is being re-planned",
        SERVICE,
        "            target_trip_id=trip_id,",
        "            target_trip_id=None,",
    ),
    (
        "the missing-trip 404 swallowed, so a job is created for an id that never existed",
        ROUTES,
        "    except TripNotFoundError as exc:\n"
        "        # Raised before any job exists: a refresh of a missing (or another owner's) trip\n"
        "        # fails here rather than becoming a job that dies a minute later.\n"
        '        raise HTTPException(status_code=404, detail="trip not found") from exc\n'
        "    return job_as_dict(job)",
        "    except TripNotFoundError as exc:\n"
        "        raise HTTPException(status_code=200, detail=str(exc)) from exc\n"
        "    return job_as_dict(job)",
    ),
    (
        "progress accepted but not forwarded, so a refresh goes silent after its first step",
        SERVICE,
        "            owner_id=owner_id,\n            progress=progress,\n        )",
        "            owner_id=owner_id,\n        )",
    ),
]


def run_tests() -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/integration/test_planning_job_api.py", "-q"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(ROOT / "apps/api/src"),
            "PYTHONUTF8": "1",
        },
    )
    tail = [line for line in result.stdout.splitlines() if "passed" in line or "failed" in line]
    return result.returncode, tail[-1] if tail else result.stdout[-200:]


def main() -> None:
    baseline_code, baseline = run_tests()
    print(f"baseline: {baseline}")
    if baseline_code != 0:
        raise SystemExit("baseline is not green; fix that first")

    failures = 0
    for label, path, old, new in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP  {label}: anchor not found")
            failures += 1
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, summary = run_tests()
        finally:
            path.write_text(original, encoding="utf-8")
        caught = code != 0
        print(f"{'CAUGHT' if caught else 'SURVIVED'}  {label}\n        {summary}")
        if not caught:
            failures += 1

    code, summary = run_tests()
    print(f"\nrestored: {summary}")
    if failures or code != 0:
        raise SystemExit(f"{failures} mutation(s) survived or restore failed")
    print("all mutations caught")


if __name__ == "__main__":
    main()
