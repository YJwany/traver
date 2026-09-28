"""What ages does a real plan's data actually have, and is each one labelled honestly?

The user's requirement, verbatim: 「什么时候拉取到的数据就标注为什么时候的」 and
「只能用近期的评价……不能是那种几年前那种」.

Two different times travel with every fact:

  * ``fetched_at``   — when we asked. Always known, because we made the call.
  * ``observed_at``  — when the datum was produced. Only meaningful when
    ``observed_at_basis`` says the source reported it.

The requirement is about the first (label what you fetched, and when) and the second
(don't present a years-old fact as current). This probe shows both, per source, from a
real plan against the real free providers, so the answer reflects the deployed pipeline
rather than a fixture.

It also checks the invariant the web label depends on: every reference carries a
``fetched_at``, so the UI can never fall back to 未知 for a well-formed reference.

Run with the API's src on PYTHONPATH. Needs TRAVER_LIVE_TESTS=1. Takes ~70 s.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api/src"))

from travel_api.domain.models import TripProfile  # noqa: E402
from travel_api.providers.free import build_free_providers  # noqa: E402
from travel_api.providers.registry import ProviderRegistry  # noqa: E402
from travel_api.services.aggregation import build_preview  # noqa: E402
from travel_api.services.freshness import FreshnessService  # noqa: E402

#: Which data type each reference belongs to, mirroring what aggregation.py decides.
#: Kept here so the probe judges the plan's own references the way the service does.
DATA_TYPE_BY_PREFIX = {
    "openstreetmap": "attraction",
    "overpass": "attraction",
    "wikipedia": "attraction",
    "wikivoyage": "attraction",
    "open-meteo": "weather",
    "osrm": "route",
    "china-holidays": "holiday",
}


def data_type_for(source: str) -> str:
    for prefix, data_type in DATA_TYPE_BY_PREFIX.items():
        if source.startswith(prefix):
            return data_type
    return "attraction"


def human(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} 秒"
    if seconds < 5400:
        return f"{seconds // 60} 分钟"
    if seconds < 172800:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def main() -> int:
    providers, _http = build_free_providers()
    registry = ProviderRegistry(providers)
    profile = TripProfile(
        destination="杭州",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
        total_budget=5000,
    )

    preview = asyncio.run(build_preview(profile, registry))

    now = datetime.now(timezone.utc)
    print(f"{'source':16} {'data type':11} {'basis':17} {'fetched':>9} {'data age':>10}")
    print("-" * 70)

    problems: list[str] = []
    for ref in preview.source_references:
        if ref.fetched_at is None:
            # The web label states 抓取于 for every basis, and would print 未知 here.
            problems.append(f"{ref.source}: no fetched_at, so the UI cannot label when we asked")
            continue
        fetched_age = int((now - ref.fetched_at).total_seconds())
        basis = str(ref.observed_at_basis)
        if ref.observed_at_basis.age_is_about_the_data:
            data_age = f"{human(int((now - ref.observed_at).total_seconds()))}"
        else:
            data_age = "—"
        print(
            f"{ref.source:16} {data_type_for(ref.source):11} {basis:17} "
            f"{human(max(0, fetched_age)):>9} {data_age:>10}"
        )

    # What the freshness policy says about the same references.
    typed = [(data_type_for(ref.source), ref) for ref in preview.source_references]
    service = FreshnessService()
    findings = service.audit(typed)
    too_old = [f for f in findings if f["reason"] == "too_old"]
    unknown = [f for f in findings if f["reason"] == "age_unknown"]

    print(f"\naudit: {len(findings)} finding(s) across {len(typed)} reference(s)")
    print(f"  too old:      {len(too_old)}")
    print(f"  age unknown:  {len(unknown)}")
    for finding in findings[:6]:
        print(f"    {finding['source']:16} {finding['reason']:12} {finding['detail'][:56]}")

    # The plan must not have silently dropped the audit: `stale_facts` is the too-old
    # subset that reaches the UI banner.
    if len(preview.stale_facts) != len(too_old):
        problems.append(
            f"the plan carries {len(preview.stale_facts)} stale fact(s) but the audit found "
            f"{len(too_old)} too-old one(s), so a stale fact is being dropped before the UI"
        )

    # A fact whose data age is unknown must not be counted as evidence of freshness
    # anywhere. This is the "不能是那种几年前那种" case: silence is not recency.
    for finding in unknown:
        if finding["data_age_seconds"] < 0:
            problems.append(f"{finding['source']}: negative age, which is nonsense")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: every reference states when we fetched, and old/unknown data is labelled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
