"""Run one place name through the real decision and print all four outcomes.

Run this after touching ``place_matching``, ``free/places.py`` or ``amap/place_resolver.py``.
It is not a substitute for the tests — it is the thing the tests cannot be: a look at what a
reader would actually see for a name, with the full result rather than the one field an
assertion happened to check.

The four cases below are the ones that exist, and printing them side by side is the point.
Three of them are easy to confuse in a diff and impossible to confuse in output:

* ``located`` — found here, with a coordinate;
* ``elsewhere`` — found, and not here, with the distance that says so;
* ``not_found`` — looked, and the source had nothing;
* ``not_attempted`` — nobody looked, and the reason says why.

The last two are where the honesty lives. A plan built with no key produces ``not_attempted``
constantly, and rendering it as ``not_found`` turns "we have no source for this" into "this
place does not exist" — a claim about the world made out of a fact about our configuration.

No network and no key: the geocoders are stubbed. The output is therefore about the decision,
not about any particular service's data. ``--live`` is deliberately not offered — a probe that
needs a key is one that stops being run.

Exit status is non-zero if any of the four outcomes is missing, so this can be a gate rather
than something a person has to read carefully.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api" / "src"))

from travel_api.domain.models import (  # noqa: E402
    Coordinates,
    DataDomain,
    ObservationBasis,
    PlaceResolutionStatus,
    SourceReference,
    SourceType,
    TripProfile,
)
from travel_api.providers.free.geo import GeoPlace, GeoResolutionError  # noqa: E402
from travel_api.providers.free.places import (  # noqa: E402
    FreePlaceResolver,
    nominatim_precision,
    open_meteo_precision,
)
from travel_api.providers.place_matching import (  # noqa: E402
    MAX_DISTANCE_FROM_DESTINATION_KM,
    PRECISION_AREA,
    PRECISION_SPECIFIC,
    PlaceCandidate,
    decide,
    not_attempted,
)

#: 杭州's centre. The destination every case is scoped to.
HANGZHOU = Coordinates(latitude=30.2741, longitude=120.1551)
#: 惠州's West Lake — a real namesake about 950 km away.
HUIZHOU = Coordinates(latitude=23.1115, longitude=114.4162)

OPEN_METEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


class _StubHttp:
    """A transport that answers from a table and records what it was asked."""

    def __init__(self, table: dict) -> None:
        self.table = table
        self.asked: list[tuple[str, dict]] = []

    async def get_json(self, url: str, params: dict | None = None):
        self.asked.append((url, dict(params or {})))
        value = self.table.get(url)
        return value(params or {}) if callable(value) else value


class _StubGeoResolver:
    """The destination geocoder the free stack shares, stubbed."""

    def __init__(self, place: GeoPlace | None) -> None:
        self.place = place
        self.asked: list[str] = []

    async def resolve(self, destination: str) -> GeoPlace:
        self.asked.append(destination)
        if self.place is None:
            raise GeoResolutionError(f"无法解析目的地坐标: {destination}")
        return self.place


def _geo_place(destination: str = "杭州") -> GeoPlace:
    now = datetime.now(timezone.utc)
    return GeoPlace(
        name=destination,
        city=destination,
        country="中国",
        country_code="CN",
        latitude=HANGZHOU.latitude,
        longitude=HANGZHOU.longitude,
        timezone="Asia/Shanghai",
        bbox=(29.0, 119.0, 31.0, 121.0),
        source_reference=SourceReference(
            source="open-meteo-geocoding",
            source_type=SourceType.OPEN_DATA,
            source_id="1",
            observed_at=now,
            observed_at_basis=ObservationBasis.SOURCE_SILENT,
            fetched_at=now,
            domain=DataDomain.GEO_FACT,
        ),
    )


def _profile() -> TripProfile:
    return TripProfile(
        destination="杭州",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
    )


def _open_meteo(name: str, latitude: float, longitude: float, feature: str = "PPLA") -> dict:
    return {
        "results": [
            {
                "id": 1,
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
                "feature_code": feature,
                "admin1": "某省",
                "country_code": "CN",
            }
        ]
    }


def _show(label: str, resolved) -> str:
    """One line per outcome, with the fields that decide whether a reader can trust it."""
    parts = [f"{label:<22} {resolved.status.value:<14}"]
    if resolved.coordinates is not None:
        parts.append(f"@ {resolved.coordinates.latitude:.4f},{resolved.coordinates.longitude:.4f}")
    else:
        parts.append("@ —")
    if resolved.straight_line_from_centre_km is not None:
        parts.append(f"{resolved.straight_line_from_centre_km:>7.1f} km")
    else:
        parts.append(f"{'—':>10}")
    if resolved.match_confidence is not None:
        parts.append(f"conf {resolved.match_confidence:.2f}")
    else:
        parts.append("conf  —  ")
    if resolved.source_reference is not None:
        ref = resolved.source_reference
        parts.append(f"| {ref.source:<22} basis={ref.observed_at_basis.value}")
    else:
        parts.append(f"| {'(no lookup)':<22}")
    print("  ".join(parts))
    print(f"{'':24}reason: {resolved.reason}")
    if resolved.display_name:
        print(f"{'':24}matched as: {resolved.display_name}")
    return resolved.status


async def main() -> int:
    print("=" * 100)
    print("place resolution probe — the decision, not any particular service's data")
    print("=" * 100)

    seen: set[PlaceResolutionStatus] = set()

    # 1. Found here. 灵隐寺 sits ~6 km west of the city centre.
    print("\n[1] a landmark inside the destination")
    resolver = FreePlaceResolver(
        _StubHttp({OPEN_METEO_URL: _open_meteo("灵隐寺", 30.2408, 120.1010, "PPL")}),  # type: ignore[arg-type]
        geo_resolver=_StubGeoResolver(_geo_place()),  # type: ignore[arg-type]
    )
    result = await resolver.resolve_places(["灵隐寺"], _profile())
    seen.add(_show("灵隐寺 (nearby)", result.data[0]))
    print(f"{'':24}sources asked: {resolver.last_sources}")

    # 2. Found, and not here. The second source is asked because the first answered in the
    #    wrong province, and the park it finds nearby is the one the note meant.
    print("\n[2] a namesake 950 km away, and a real one nearby")
    resolver = FreePlaceResolver(
        _StubHttp(
            {
                OPEN_METEO_URL: _open_meteo("西湖", HUIZHOU.latitude, HUIZHOU.longitude),
                NOMINATIM_URL: [],
            }
        ),  # type: ignore[arg-type]
        geo_resolver=_StubGeoResolver(_geo_place()),  # type: ignore[arg-type]
    )
    result = await resolver.resolve_places(["西湖"], _profile())
    seen.add(_show("西湖 (Huizhou, 950 km)", result.data[0]))
    print(f"{'':24}sources asked: {resolver.last_sources}")

    # 3. Looked, nothing there.
    print("\n[3] looked up, nothing returned")
    resolver = FreePlaceResolver(
        _StubHttp({OPEN_METEO_URL: lambda params: None, NOMINATIM_URL: []}),  # type: ignore[arg-type]
        geo_resolver=_StubGeoResolver(_geo_place()),  # type: ignore[arg-type]
    )
    result = await resolver.resolve_places(["查不到的地方"], _profile())
    seen.add(_show("查不到的地方", result.data[0]))

    # 4. Nobody looked. This is the one that must never render as case 3.
    print("\n[4] no source configured — nobody looked")
    resolver = FreePlaceResolver(
        _StubHttp({}),  # type: ignore[arg-type]
        geo_resolver=_StubGeoResolver(None),  # type: ignore[arg-type]
    )
    result = await resolver.resolve_places(["灵隐寺"], _profile())
    seen.add(_show("灵隐寺 (no source)", result.data[0]))
    print(f"{'':24}errors: {result.errors}")

    # The shared decision, directly, so a change in ranking is visible without a transport.
    print("\n[5] precision before distance, on the same name")
    district = PlaceCandidate(
        source="amap",
        source_id="district",
        latitude=30.2700,
        longitude=120.1500,
        precision=PRECISION_AREA,
        confidence=0.85,
        display_name="西湖区",
    )
    scenic = PlaceCandidate(
        source="amap",
        source_id="scenic",
        latitude=30.2408,
        longitude=120.1010,
        precision=PRECISION_SPECIFIC,
        confidence=0.85,
        display_name="西湖风景名胜区",
    )
    chosen = decide(
        "西湖", [district, scenic], destination=HANGZHOU, source_type=SourceType.OFFICIAL_API
    )
    seen.add(_show("西湖 (two matches)", chosen))
    print(f"{'':24}the district is {abs(30.2741 - 30.2700):.4f}° nearer, and lost on precision")

    print("\n[6] the same code path with no name at all")
    for entry in not_attempted(["  "], "提取到的内容不是一个可用于查询的地名"):
        seen.add(_show("(blank)", entry))

    print("\n" + "-" * 100)
    print(f"precision bands in play: SPECIFIC={PRECISION_SPECIFIC} AREA={PRECISION_AREA}")
    print(
        f"open-meteo PPLA -> {open_meteo_precision('PPLA')}; nominatim park -> {nominatim_precision('park')}"
    )
    print(f"rejects beyond {MAX_DISTANCE_FROM_DESTINATION_KM:.0f} km from the destination")

    missing = set(PlaceResolutionStatus) - seen
    if missing:
        print("\nFAIL: these outcomes never occurred, so this run proves nothing about them:")
        for status in sorted(missing):
            print(f"  - {status.value}")
        return 1
    print(f"\nOK: all {len(seen)} outcomes produced ({', '.join(sorted(s.value for s in seen))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
