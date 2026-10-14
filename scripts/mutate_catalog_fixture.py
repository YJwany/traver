"""Does the settings-page fixture's contract test actually catch a drifted fixture? Put the
drift back and see.

`provider-catalog.test.tsx` carries a hand-written copy of what `/api/v1/providers/catalog`
sends, and two Python tests compare it against the live response. A comparison is only worth
having if it fails when the copy drifts — and this fixture has drifted twice already, both times
in the direction of describing a world the server had left behind (高德 as `planned`; room rates
as a dead end after RollingGo made them unlockable).

The second of those is why this harness exists: the drift was live for a whole session, the
suite stayed green, and only a manual reading of the fixture found it. So each case restores a
plausible drift and requires the contract test to notice.

    python scripts/mutate_catalog_fixture.py
"""

from __future__ import annotations

from _mutation_harness import ROOT, Case, run_harness

FIXTURE = ROOT / "apps/web/app/components/provider-catalog.test.tsx"
TESTS = "tests/unit/test_web_api_contract.py"

CASES = [
    Case(
        "M1 room rates are put back in the fixture's dead-end gap list",
        FIXTURE,
        """    {
      capability: "search_reviews",
      label: "评价与评分",
      note: "评价与评分需要「带发布时间的意见」，本工具尚未接入任何这样的来源。因此酒店与餐厅的评分字段始终为空，而不是默认值。",
      closable_by: "source",
    },""",
        """    {
      capability: "search_room_offers",
      label: "房型与报价",
      note: "房型与实时房价没有任何已接入的来源能提供它，因此预算里的住宿部分是估算额度。",
      closable_by: "source",
    },""",
    ),
    Case(
        "M2 RollingGo is described as unimplemented",
        FIXTURE,
        """  status: "needs_credentials",
  capabilities: ["search_hotels", "search_room_offers"],""",
        """  status: "planned",
  capabilities: ["search_hotels", "search_room_offers"],""",
    ),
    Case(
        "M3 the fixture drops the capability RollingGo exists to add",
        FIXTURE,
        '  capabilities: ["search_hotels", "search_room_offers"],',
        '  capabilities: ["search_hotels"],',
    ),
]

if __name__ == "__main__":
    raise SystemExit(run_harness(files=(FIXTURE,), cases=CASES, test_targets=TESTS))
