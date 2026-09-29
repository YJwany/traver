"""Mutation harness for the reviews capability and the shared element-age statistic.

Two rules this harness follows, both learned the hard way on the OSM work:

  1. It asserts the targeted tests PASS on the unmutated tree *first*. A mutation that does not
     fire and a guard that does not work look identical from the outside; without this check the
     harness cannot tell them apart.
  2. It restores every file at the start of *every* iteration, not just at the end. Otherwise
     case N's baseline runs case N-1's mutation and caught mutations get reported as uncaught.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(r"C:\Users\27875\Desktop\Traver")
AGG = ROOT / "apps/api/src/travel_api/services/aggregation.py"
MODELS = ROOT / "apps/api/src/travel_api/domain/models.py"
FRESH = ROOT / "apps/api/src/travel_api/services/freshness.py"
EXPORT = ROOT / "apps/api/src/travel_api/services/export.py"
OVERPASS = ROOT / "apps/api/src/travel_api/providers/free/overpass.py"

TOUCHED = [AGG, MODELS, FRESH, EXPORT, OVERPASS]

REVIEW_TESTS = "tests/unit/test_reviews_capability.py"
EXPORT_TESTS = "tests/unit/test_export_documents.py"
FRESH_TESTS = "tests/unit/test_freshness.py"
FREE_TESTS = "tests/unit/test_free_providers.py"

# (name, file, old, new, tests that must fail)
CASES: list[tuple[str, pathlib.Path, str, str, str]] = [
    (
        "M1 do not derive the review set's age from the reviews",
        AGG,
        "        if review_age is None:\n            aged_review_results.append(result)\n            continue",
        "        if True:\n            aged_review_results.append(result)\n            continue",
        REVIEW_TESTS,
    ),
    (
        "M2 drop the guard against a review about a subject not on the plan",
        AGG,
        "if isinstance(item, Review) and item.subject_id in known_subjects",
        "if isinstance(item, Review)",
        REVIEW_TESTS,
    ),
    (
        "M3 never call the review source",
        AGG,
        '        await _call_capability(registry, "search_reviews", review_subject_ids, profile)\n        if review_subject_ids\n        else []',
        "        []\n        if review_subject_ids\n        else []",
        REVIEW_TESTS,
    ),
    (
        "M4 report a set of zeros instead of None when nothing is dated",
        MODELS,
        "        stamps = [moment for moment in moments if moment is not None]\n        if not stamps:\n            return None",
        "        stamps = [moment for moment in moments if moment is not None]\n        if not stamps:\n            stamps = [now]",
        REVIEW_TESTS,
    ),
    (
        "M5 an undated review reports age 0 rather than None",
        MODELS,
        "        if self.published_at is None:\n            return None\n        moment = self.published_at",
        "        if self.published_at is None:\n            return 0\n        moment = self.published_at",
        REVIEW_TESTS,
    ),
    (
        "M6 a naive publication date is left naive and raises",
        MODELS,
        "        moment = self.published_at\n        if moment.tzinfo is None:\n            moment = moment.replace(tzinfo=timezone.utc)",
        "        moment = self.published_at",
        REVIEW_TESTS,
    ),
    (
        "M7 the review set is aged with the OSM verb",
        AGG,
        "            member_kind=ElementAgeKind.PUBLISHED,",
        "            member_kind=ElementAgeKind.EDITED,",
        REVIEW_TESTS + " " + EXPORT_TESTS,
    ),
    (
        "M8 the element-age finding is never emitted",
        FRESH,
        "        if element_age is None or not element_age.any_older_than_horizon:\n            return None",
        "        if True:\n            return None",
        FRESH_TESTS + " " + REVIEW_TESTS,
    ),
    (
        "M9 the shared statistic ignores the horizon",
        MODELS,
        "            older_than_horizon=sum(1 for age in ages if horizon and age > horizon),",
        "            older_than_horizon=0,",
        FRESH_TESTS + " " + REVIEW_TESTS,
    ),
    (
        "M10 the export always uses the OSM verb",
        EXPORT,
        '            if element_age.member_kind is ElementAgeKind.EDITED\n            else "最早一条写于"',
        '            if True\n            else "最早一条写于"',
        EXPORT_TESTS,
    ),
    (
        # Targeted at the producer in `overpass.py`, so the suite that pins *that* producer has
        # to be in the list. Naming only the export suite made this mutation look uncaught while
        # the guard existed — the harness, not the guard, was wrong.
        "M11 Overpass declares its members published rather than edited",
        OVERPASS,
        "            member_kind=ElementAgeKind.EDITED,",
        "            member_kind=ElementAgeKind.PUBLISHED,",
        FREE_TESTS + " " + EXPORT_TESTS,
    ),
    (
        "M12 the review horizon is removed so old opinions read as current",
        FRESH,
        '    "review": 18 * 30 * 86400,',
        "",
        REVIEW_TESTS,
    ),
]


def run(tests: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *tests.split(), "-q", "--no-header", "-p", "no:logging"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(ROOT / "apps/api/src"),
            "PYTHONUTF8": "1",
        },
    )
    return proc.returncode, proc.stdout[-1500:]


def main() -> int:
    originals = {path: path.read_text(encoding="utf-8") for path in TOUCHED}

    def restore() -> None:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    restore()
    print("== baseline (unmutated) ==")
    failed: list[str] = []
    for name, _path, _old, _new, tests in CASES:
        code, out = run(tests)
        if code != 0:
            print(f"  !! BASELINE FAILS for {name}: {tests}")
            print(out)
            return 1
    print("  all targeted suites pass unmutated\n")

    for name, path, old, new, tests in CASES:
        restore()
        text = path.read_text(encoding="utf-8")
        if old not in text:
            print(f"NOT APPLIED (text not found): {name}\n  in {path.name}")
            failed.append(name)
            continue
        if text.count(old) != 1:
            print(f"AMBIGUOUS ({text.count(old)} matches): {name}")
            failed.append(name)
            continue
        path.write_text(text.replace(old, new), encoding="utf-8")
        code, out = run(tests)
        status = "caught" if code != 0 else "NOT CAUGHT"
        print(f"{status:11} {name}")
        if code == 0:
            failed.append(name)
            print(out)

    restore()
    print()
    if failed:
        print(f"{len(failed)} of {len(CASES)} mutations NOT accounted for:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print(f"all {len(CASES)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
