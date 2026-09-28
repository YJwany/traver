"""Routing provider backed by the public OSRM demo server.

Critical constraint discovered by live testing
----------------------------------------------
The public demo server (``router.project-osrm.org``) serves **only the car
profile**. Requesting ``/route/v1/foot/...`` returns HTTP 200 with byte-identical
results to ``driving`` — a ~6 km cross-town leg comes back as ~7 minutes, i.e.
~40 km/h. Silently trusting that would put an eight-kilometre "walk" in the
itinerary, which is a fabricated fact.

So this provider:

* always routes with the profile the server actually implements (car);
* reports **real** distance and duration only for vehicle modes;
* for walking, keeps the measured road distance but derives the duration from an
  explicit walking speed and sets ``is_estimate=True``, so nothing downstream can
  present a car time as a pedestrian time.

The demo server is also community infrastructure with no SLA and a published
limit of at most 1 request/second (enforced in ``free/http.py``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from travel_api.domain.models import (
    DataDomain,
    Money,
    ObservationBasis,
    Pace,
    PriceType,
    ProviderHealth,
    ProviderResult,
    RouteLeg,
    RoutePoint,
    SourceReference,
    SourceType,
    TravelMode,
    TripProfile,
)
from travel_api.providers.free.attribution import OSRM_ATTRIBUTION as _OSRM_CREDIT
from travel_api.providers.free.http import FreeHttpClient

logger = logging.getLogger(__name__)

OSRM_BASE_URL = "https://router.project-osrm.org"

OSM_ATTRIBUTION = f"{_OSRM_CREDIT.text} ({_OSRM_CREDIT.licence})"

#: The profile the public demo server actually implements. Verified live: every
#: other profile name returns identical car results instead of failing.
OSRM_SUPPORTED_PROFILE = "driving"

#: TripConfig TravelMode -> the profile we ask OSRM for.
OSRM_PROFILE_BY_MODE: dict[TravelMode, str] = {
    TravelMode.WALK: OSRM_SUPPORTED_PROFILE,
    TravelMode.DRIVE: OSRM_SUPPORTED_PROFILE,
    TravelMode.TAXI: OSRM_SUPPORTED_PROFILE,
    TravelMode.TRANSIT: OSRM_SUPPORTED_PROFILE,
}

#: Average city walking speed, used to derive pedestrian durations because OSRM
#: cannot measure them. 4.5 km/h is a common planning figure.
WALKING_SPEED_METERS_PER_SECOND = 1.25

#: Rough per-kilometre taxi/transit fare, attached only to vehicle legs and
#: always flagged as an estimate.
ESTIMATED_COST_PER_KM_CNY = 2.5

#: The demo server rejects tables larger than 100 coordinates with HTTP 400
#: "Too many table coordinates"; we stay well inside that.
OSRM_MAX_TABLE_COORDINATES = 100


def mode_for_pace(pace: Pace) -> TravelMode:
    """Pick a travel mode: walking for relaxed/balanced city trips, driving otherwise."""
    return TravelMode.WALK if pace in (Pace.RELAXED, Pace.BALANCED) else TravelMode.DRIVE


class OsmRoutingProvider:
    """Serves the routing capability from the OSRM demo server."""

    def __init__(
        self,
        name: str = "osrm",
        *,
        http: FreeHttpClient,
        base_url: str = OSRM_BASE_URL,
        timeout_seconds: float = 30.0,
        ttl_seconds: int = 600,
        max_points: int = 25,
    ) -> None:
        self.name = name
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.ttl_seconds = ttl_seconds
        self.max_points = min(max_points, OSRM_MAX_TABLE_COORDINATES)
        self.last_profile: str | None = None

    async def calculate_routes(
        self, profile: TripProfile, points: list[RoutePoint]
    ) -> ProviderResult:
        if len(points) < 2:
            return ProviderResult(provider=self.name, data=[])

        mode = mode_for_pace(profile.pace)
        selected = points[: self.max_points]
        coordinates = ";".join(f"{point.longitude:.6f},{point.latitude:.6f}" for point in selected)

        profile_name = OSRM_PROFILE_BY_MODE.get(mode, OSRM_SUPPORTED_PROFILE)
        url = f"{self.base_url}/table/v1/{profile_name}/{coordinates}"
        payload = await self.http.get_json(url, params={"annotations": "distance,duration"})

        if not isinstance(payload, dict) or payload.get("code") != "Ok":
            code = payload.get("code") if isinstance(payload, dict) else None
            raise RuntimeError(f"OSRM returned code {code!r}")

        self.last_profile = profile_name
        reference = self._source_reference(mode=mode, point_count=len(selected))
        legs = self._build_legs(selected, payload, mode, reference)
        return ProviderResult(
            provider=self.name,
            data=legs,
            source_references=[reference] if legs else [],
        )

    def _build_legs(
        self,
        points: list[RoutePoint],
        payload: dict,
        mode: TravelMode,
        reference: SourceReference,
    ) -> list[RouteLeg]:
        distances = payload.get("distances") or []
        durations = payload.get("durations") or []
        legs: list[RouteLeg] = []

        for origin_index, origin in enumerate(points):
            for destination_index, destination in enumerate(points):
                if origin_index == destination_index:
                    continue
                distance = _matrix_value(distances, origin_index, destination_index)
                if distance is None:
                    continue
                distance_meters = max(0, int(round(distance)))

                duration_minutes, is_estimate = self._travel_minutes(
                    mode, distance_meters, durations, origin_index, destination_index
                )
                legs.append(
                    RouteLeg(
                        origin_id=origin.id,
                        destination_id=destination.id,
                        mode=mode,
                        distance_meters=distance_meters,
                        duration_minutes=duration_minutes,
                        cost=self._estimated_cost(distance_meters, mode, reference),
                        is_estimate=is_estimate,
                        source_reference=reference,
                    )
                )
        return legs

    @staticmethod
    def _travel_minutes(
        mode: TravelMode,
        distance_meters: int,
        durations: list,
        origin_index: int,
        destination_index: int,
    ) -> tuple[int, bool]:
        """Return (minutes, is_estimate).

        OSRM measures car travel. For walking we must not reuse that number, so
        the duration is derived from the road distance at a stated walking speed
        and flagged as an estimate.
        """
        if mode is TravelMode.WALK:
            seconds = distance_meters / WALKING_SPEED_METERS_PER_SECOND
            return max(1, int(round(seconds / 60))), True

        duration = _matrix_value(durations, origin_index, destination_index)
        if duration is None:
            # Distance known, duration missing: derive from a conservative
            # urban driving speed (25 km/h) and flag it.
            seconds = distance_meters / (25_000 / 3600)
            return max(1, int(round(seconds / 60))), True
        return max(1, int(round(duration / 60))), False

    @staticmethod
    def _estimated_cost(
        distance_meters: int, mode: TravelMode, reference: SourceReference
    ) -> Money | None:
        if mode is TravelMode.WALK:
            return None

        amount = (Decimal(distance_meters) / Decimal(1000)) * Decimal(
            str(ESTIMATED_COST_PER_KM_CNY)
        )
        return Money(
            value=amount.quantize(Decimal("0.01")),
            currency="CNY",
            price_type=PriceType.PER_PERSON,
            source_reference_id=reference.source_id,
            is_estimate=True,
        )

    def _source_reference(self, *, mode: TravelMode, point_count: int) -> SourceReference:
        now = datetime.now(timezone.utc)
        return SourceReference(
            source=self.name,
            source_type=SourceType.OPEN_DATA,
            source_id=f"osrm:{OSRM_SUPPORTED_PROFILE}:{point_count}",
            observed_at=now,
            observed_at_basis=ObservationBasis.LIVE_COMPUTED,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            request_context={
                "osrm_profile": OSRM_SUPPORTED_PROFILE,
                "travel_mode": mode.value,
                "point_count": point_count,
                "endpoint": self.base_url,
                "attribution": OSM_ATTRIBUTION,
                # Disclose the limitation in the data itself, not just in docs.
                "duration_source": (
                    "derived_walking_speed" if mode is TravelMode.WALK else "osrm_car_profile"
                ),
                "walking_speed_mps": WALKING_SPEED_METERS_PER_SECOND
                if mode is TravelMode.WALK
                else None,
                "note": "public demo server routes cars only; walking times are estimates",
            },
            data_version="osrm-live",
            confidence=0.6 if mode is TravelMode.WALK else 0.75,
            domain=DataDomain.GEO_FACT,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            status="healthy",
            latency_ms=0.0,
            detail=f"OSRM table service (profile={OSRM_SUPPORTED_PROFILE}, car-only server)",
        )


def _matrix_value(matrix: list, row: int, column: int):
    if not isinstance(matrix, list) or row >= len(matrix):
        return None
    row_values = matrix[row]
    if not isinstance(row_values, list) or column >= len(row_values):
        return None
    value = row_values[column]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
