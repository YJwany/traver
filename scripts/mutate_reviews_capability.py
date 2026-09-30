"""Mutation harness for the reviews capability and the shared element-age statistic.

The three rules this follows live in `_mutation_harness.py` now, because all five harnesses in this
directory had reimplemented them and three of them had got one wrong — this one, with the most
thorough version, was the model for the shared runner:

  1. The targeted tests must PASS on the unmutated tree *first*. A mutation that does not fire and
     a guard that does not work look identical from the outside; without this check the harness
     cannot tell them apart. Every distinct per-case target is baseline-checked, not just the
     harness-wide one.
  2. Every file is restored at the start of *every* iteration, not only at the end. Otherwise case
     N runs against case N-1's mutation and caught mutations get reported as uncaught.
  3. The restore reads a sidecar backup rather than whatever is currently on disk, and happens in a
     `finally`. This one was added last, after a crash mid-run left a mutation on disk and the next
     run adopted it as "the original" — the fix and the defect being the same file is not a
     situation a harness should be able to create.

Each case names the suite expected to notice it. That is a claim, not decoration: running an
unrelated suite lets a different test fail for a different reason, which looks exactly like the
mutation being caught.
"""

from __future__ import annotations

from pathlib import Path

from _mutation_harness import ROOT, Case, run_harness

AGG = ROOT / "apps/api/src/travel_api/services/aggregation.py"
MODELS = ROOT / "apps/api/src/travel_api/domain/models.py"
FRESH = ROOT / "apps/api/src/travel_api/services/freshness.py"
EXPORT = ROOT / "apps/api/src/travel_api/services/export.py"
OVERPASS = ROOT / "apps/api/src/travel_api/providers/free/overpass.py"

TOUCHED = (AGG, MODELS, FRESH, EXPORT, OVERPASS)

REVIEW_TESTS = "tests/unit/test_reviews_capability.py"
EXPORT_TESTS = "tests/unit/test_export_documents.py"
FRESH_TESTS = "tests/unit/test_freshness.py"
FREE_TESTS = "tests/unit/test_free_providers.py"

# (name, file, old, new, tests that must fail)
RAW_CASES: list[tuple[str, Path, str, str, str]] = [
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

#: Built from the tuples above so the anchored `old` strings stay exactly as written. The per-case
#: test target is preserved: it is what makes "which suite notices this" an explicit claim rather
#: than "some test somewhere failed".
CASES = [Case(name, path, old, new, tests) for name, path, old, new, tests in RAW_CASES]

if __name__ == "__main__":
    raise SystemExit(
        run_harness(
            files=TOUCHED,
            cases=CASES,
            test_targets="tests/unit/test_reviews_capability.py",
        )
    )
