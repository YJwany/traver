"""Prove the prose guard fails on the English sentence it was written for.

A guard that cannot fail is decoration. This imports the checker and feeds it the original
string, then feeds it the Chinese replacement, so the test asserts what it is supposed to
assert rather than merely passing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/unit"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api/src"))

from test_user_facing_prose import _english_words  # noqa: E402

OLD = "5 leg(s) use distance-based travel estimates, not measured routes"
NEW = "5 段路程按距离估算，不是实测路线"
OLD_PROFILE = "免费路线源（OSRM）的步行 profile 可以算，通常是网络或限流问题"
NEW_PROFILE = "免费路线源（OSRM）的步行路线类型可以算，通常是网络或限流问题"
LEGITIMATE = "接入高德 Web 服务 Key（公交/地铁是免费源完全没有的能力）"

problems: list[str] = []

caught = _english_words(OLD)
print(f"  old itinerary sentence -> flagged {sorted(caught)}")
if not caught:
    problems.append("the guard does not flag the English sentence it exists to catch")

passed = _english_words(NEW)
print(f"  new Chinese sentence   -> flagged {sorted(passed)}")
if passed:
    problems.append(f"the guard flags the Chinese replacement: {sorted(passed)}")

caught_profile = _english_words(OLD_PROFILE)
print(f"  'profile' leak         -> flagged {sorted(caught_profile)}")
if "profile" not in caught_profile:
    problems.append("the guard does not flag the 'profile' leak")

if _english_words(NEW_PROFILE):
    problems.append("the guard flags the corrected remedy")

allowed = _english_words(LEGITIMATE)
print(f"  高德 Web 服务 Key       -> flagged {sorted(allowed)}")
if allowed:
    problems.append(f"the guard rejects 高德's own product name: {sorted(allowed)}")

if problems:
    print(f"\n{len(problems)} problem(s):")
    for problem in problems:
        print(f"  - {problem}")
    raise SystemExit(1)
print("\nOK: the guard flags the leak, accepts the fix, and allows the product name")
