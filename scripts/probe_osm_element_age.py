"""Measure how old OSM's individual elements are, versus the snapshot the API reports.

Run this when questioning the 时效性 claims about OpenStreetMap. It is the instrument behind
three statements the codebase makes in prose, so they can be re-checked rather than believed:

1. ``osm3s.timestamp_osm_base`` is the *database snapshot* time, not the age of the data.
2. A snapshot minutes old routinely contains elements last edited years ago.
3. The element age distribution is wide enough that a median and an oldest are both needed.

The first version of the export note claimed these snapshots are 「通常只有几分钟」 old. That was
measured in one city, printed directly beneath a table row reading 27 hours, and is wrong for
成都, whose mirror served a 146-day-old snapshot. Hence a script rather than a remembered number.

Usage:

    python scripts/probe_osm_element_age.py
    python scripts/probe_osm_element_age.py --area 杭州西湖 --area 成都市中心

Exits non-zero if no area produced a usable answer, so a CI or manual run cannot report success
while having measured nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)


@dataclass(frozen=True)
class Area:
    name: str
    bbox: str
    key: str
    value: str


#: Two categories, because hotels and restaurants are edited on different rhythms and one
#: sample could be a quirk of the category rather than of the source.
AREAS = (
    Area("杭州西湖", "30.23,120.13,30.27,120.17", "tourism", "attraction"),
    Area("成都市中心", "30.63,104.03,30.68,104.09", "tourism", "attraction"),
    Area("丽江古城", "26.86,100.22,26.89,100.25", "amenity", "restaurant"),
    Area("北京市中心", "39.90,116.38,39.93,116.43", "amenity", "restaurant"),
    Area("上海人民广场", "31.22,121.46,31.25,121.49", "amenity", "restaurant"),
)

HORIZON_DAYS = 1.0  # the POI listing horizon, from FreshnessPolicy


def build_query(area: Area, limit: int) -> str:
    # `center` before `meta`: reverse the two and Overpass rejects the query, and without
    # `center` every way comes back with no coordinates at all and is dropped.
    return (
        "[out:json][timeout:40];"
        f'nwr["{area.key}"="{area.value}"]({area.bbox})->.items;'
        f".items out center meta {limit};"
    )


async def fetch(client: httpx.AsyncClient, area: Area, limit: int) -> dict | None:
    """Try each mirror in turn. A 504 from this service is ordinary, not a verdict."""
    for attempt in range(len(MIRRORS) * 2):
        endpoint = MIRRORS[attempt % len(MIRRORS)]
        try:
            response = await client.post(endpoint, data={"data": build_query(area, limit)})
        except httpx.HTTPError as exc:
            print(f"    {endpoint.split('/')[2]}: {type(exc).__name__}")
        else:
            if response.status_code == 200:
                return response.json()
            print(f"    {endpoint.split('/')[2]}: HTTP {response.status_code}")
        await asyncio.sleep(5)
    return None


def report(area: Area, payload: dict) -> bool:
    snapshot_raw = (payload.get("osm3s") or {}).get("timestamp_osm_base")
    elements = [e for e in (payload.get("elements") or []) if isinstance(e, dict)]
    now = datetime.now(timezone.utc)

    stamps = []
    for element in elements:
        raw = element.get("timestamp")
        if isinstance(raw, str):
            try:
                stamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                continue

    kinds: dict[str, int] = {}
    for element in elements:
        kinds[str(element.get("type"))] = kinds.get(str(element.get("type")), 0) + 1

    print(f"  elements            {len(elements)}  {kinds}")
    if not stamps:
        print("  no per-element timestamps: did the query ask for `meta`?")
        return False

    ages_days = sorted((now - stamp).total_seconds() / 86400 for stamp in stamps)
    median = statistics.median(ages_days)
    oldest = ages_days[-1]
    over_horizon = sum(1 for age in ages_days if age > HORIZON_DAYS)

    if snapshot_raw:
        try:
            snapshot = datetime.fromisoformat(snapshot_raw.replace("Z", "+00:00"))
            snapshot_days = (now - snapshot).total_seconds() / 86400
            print(f"  snapshot age        {snapshot_days * 1440:>12.2f} minutes  ({snapshot_raw})")
            print(
                f"  median / snapshot   {median / snapshot_days if snapshot_days else float('inf'):>12,.0f}x"
            )
        except ValueError:
            print(f"  snapshot age        unparseable: {snapshot_raw!r}")
    else:
        print("  snapshot age        not published")

    print(f"  element age median  {median:>12.1f} days")
    print(f"  element age oldest  {oldest:>12.1f} days")
    print(f"  over {HORIZON_DAYS:g}-day horizon {over_horizon:>7} / {len(ages_days)}")
    one_year = sum(1 for age in ages_days if age > 365)
    five_years = sum(1 for age in ages_days if age > 365 * 5)
    print(f"  older than 1 year   {one_year:>12} / {len(ages_days)}")
    print(f"  older than 5 years  {five_years:>12} / {len(ages_days)}")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--area",
        action="append",
        default=None,
        help="Area name to probe (repeatable). Defaults to all built-in areas.",
    )
    parser.add_argument("--limit", type=int, default=200, help="Elements per area.")
    args = parser.parse_args()

    selected = [a for a in AREAS if args.area is None or a.name in set(args.area)]
    missing = sorted(set(args.area or []) - {a.name for a in AREAS})
    if missing:
        print(f"unknown areas: {missing}")
        print(f"known: {[a.name for a in AREAS]}")
        return 2

    measured = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0),
        headers={"User-Agent": "TraverProbe/1.0 (measuring OSM element age)"},
    ) as client:
        for area in selected:
            print(f"\n{area.name}  [{area.key}={area.value}]")
            payload = await fetch(client, area, args.limit)
            if payload is None:
                print("  gave up: every mirror failed")
                continue
            if report(area, payload):
                measured += 1
            await asyncio.sleep(3)

    print()
    if measured == 0:
        print("measured nothing: this run proves nothing either way")
        return 1
    print(
        f"measured {measured} area(s). The claim under test is that the median element age is "
        f"orders of magnitude larger than the snapshot age — compare the two lines above."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
