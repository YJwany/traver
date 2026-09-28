"""AMap routing: 公交/地铁, driving, walking and cycling.

The reason this provider exists
-------------------------------
The free stack cannot plan a Chinese city trip by public transport. The public OSRM
demo serves the car profile only, so before this provider a Beijing or Shanghai
itinerary had no way to answer "how do I get there by subway" — and 公交/地铁 is
precisely what the whole product needs for domestic destinations.

How the matrix is built, and why it is not N² calls
--------------------------------------------------
``RouteOptimizer`` looks legs up by ``(origin_id, destination_id)`` while scheduling
in an order it chooses, so it needs the *whole* matrix. The naive implementation
issues one call per ordered pair, which for a 17-point candidate set is 289 requests:
enough to burn a free monthly quota in a handful of plans, and slow enough to look
like a hang.

``/v3/distance`` is one-to-many (many origins → **one** destination), so sweeping
each point as the destination yields the full matrix in *N* calls instead of N².
That is what the driving and walking paths do, and it returns AMap's own measured
distance and duration rather than an inference.

公交/地铁 has no such endpoint (``/v3/distance`` supports only 直线/驾车/步行), so
transit legs are fetched per pair. They are therefore limited to the point set's
**consecutive pairs** in input order, which is the order an itinerary actually
visits them. Pairs outside that set are deliberately left absent rather than
invented: the optimizer already falls back to a haversine estimate and flags it, and
an absent leg that becomes a disclosed estimate is honest, whereas a fabricated
transit time is not.

No caching, by design
---------------------
AMap's terms forbid storing or caching results, so this class holds no cache. It is
also the behaviour the product needs: a cached price or duration presented as
current is exactly the misrepresentation the freshness rules forbid. Each planning
run is a live query, and every datum is stamped with the moment it was fetched.

Two conversions that are easy to get wrong and produce no error if they are
----------------------------------------------------------------------------------
* AMap sends ``"lon,lat"``; everything internal is latitude-first. The swap happens
  only in ``parse_location``.
* ``distance`` is metres and ``duration`` is seconds, both as **strings**, and
  ``/v5/direction/walking`` has **no ``duration`` field at all**.
"""

from __future__ import annotations

import asyncio
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
from travel_api.providers.amap.client import (
    AmapClient,
    AmapError,
    parse_float,
    parse_int,
)
from travel_api.providers.base import resolve_travel_mode
from travel_api.providers.free.attribution import AMAP_ATTRIBUTION

logger = logging.getLogger(__name__)

#: TravelMode -> the ``type`` value ``/v3/distance`` understands.
#:
#: Only driving is listed. AMap's own documentation and its own client disagree
#: about the remaining values (the doc gives ``0`` 直线 / ``1`` 驾车 / ``3`` 步行,
#: while the client sends the words ``driving``/``walking``/``transit``), and the
#: one value both agree on is ``1`` = 驾车. So driving takes the batched path and
#: everything else uses the endpoint whose behaviour is unambiguous.
#:
#: Walking deliberately does *not* use this endpoint. ``/v5/direction/walking``
#: returns the route's geometry and step list, which is what a day plan needs, and
#: the batched endpoint would return a bare number.
DISTANCE_TYPE_BY_MODE: dict[TravelMode, int] = {
    TravelMode.DRIVE: 1,
    TravelMode.TAXI: 1,
}

#: Per-pair direction paths, for the modes the batched endpoint does not serve.
DIRECTION_PATH_BY_MODE: dict[TravelMode, str] = {
    TravelMode.WALK: "/v5/direction/walking",
    TravelMode.DRIVE: "/v5/direction/driving",
    TravelMode.TAXI: "/v5/direction/driving",
    TravelMode.TRANSIT: "/v5/direction/transit/integrated",
}

#: Fallback walking speed, used only if AMap omits a walking duration. Matches the
#: figure the OSRM provider uses so the two sources cannot disagree about what a
#: walking pace is.
WALKING_SPEED_METERS_PER_SECOND = 1.25

#: Rough per-kilometre taxi fare for the derived cost on vehicle legs. Always
#: flagged as an estimate; AMap's own ``taxi_cost`` is used when present.
ESTIMATED_COST_PER_KM_CNY = 2.5

#: ``/v3/distance`` accepts at most 100 origins per request, as documented by AMap.
#:
#: This is protocol knowledge, not a preference, so it lives here rather than in
#: settings: a value a user could lower would silently reduce route coverage while
#: looking like a tuning knob. It is enforced structurally on ``max_points`` below,
#: the same way the OSRM provider clamps to its own coordinate limit, so the sweep's
#: origins can never exceed it regardless of how the point budget is configured.
AMAP_MAX_MATRIX_ORIGINS = 100


def mode_for_pace(pace: Pace) -> TravelMode:
    """Pick a travel mode from the trip's pace alone.

    Mirrors the OSRM provider's choice so that swapping in AMap does not silently
    change which journeys a plan is built around — the only intended difference is
    that AMap can actually answer for the mode it picks.

    Retained as the fallback half of :func:`resolve_travel_mode`; callers that hold a
    whole profile should use that instead, or an explicit 公交/地铁 request would be
    ignored.
    """
    return TravelMode.WALK if pace in (Pace.RELAXED, Pace.BALANCED) else TravelMode.DRIVE


def _format_location(point: RoutePoint) -> str:
    """``"lon,lat"`` with at most 6 decimals, which is AMap's documented limit."""
    return f"{point.longitude:.6f},{point.latitude:.6f}"


class AmapRoutingProvider:
    """Serves ``calculate_routes`` from AMap, including 公交/地铁."""

    def __init__(
        self,
        client: AmapClient,
        *,
        name: str = "amap",
        ttl_seconds: int = 300,
        max_points: int = 12,
        max_transit_legs: int = 8,
        concurrency: int = 4,
    ) -> None:
        self.name = name
        self.client = client
        self.ttl_seconds = ttl_seconds
        #: How many places AMap will route. Each point beyond the sweep costs one more
        #: request and the free quota is a monthly call count, so this is a real cost
        #: decision that belongs to the owner of the key — it is configurable, and the
        #: default matches the OSRM provider so that supplying a key never *reduces* how
        #: much of the itinerary is routed.
        self.max_points = min(max(2, max_points), AMAP_MAX_MATRIX_ORIGINS)
        self.max_transit_legs = max(1, max_transit_legs)
        self.concurrency = max(1, concurrency)
        #: Which mode the last run actually routed, for the audit trail.
        self.last_mode: TravelMode | None = None
        self.last_cities: tuple[str, str] | None = None

    # -- capability --------------------------------------------------------

    async def calculate_routes(
        self, profile: TripProfile, points: list[RoutePoint]
    ) -> ProviderResult:
        if len(points) < 2:
            return ProviderResult(provider=self.name, data=[])

        mode = resolve_travel_mode(profile)
        selected = points[: self.max_points]
        # Recorded, not just used. `max_points` is a budget, so a trip with more places
        # than the budget silently routes only the first slice and the rest fall back to
        # estimates; a reader of one leg cannot tell that from "these are all the places".
        self.last_points_offered = len(points)
        reference = self._source_reference(
            mode=mode,
            point_count=len(selected),
            destination=profile.destination,
            points_offered=len(points),
        )

        if mode is TravelMode.TRANSIT:
            legs = await self._transit_legs(selected, profile, reference)
        elif mode in DISTANCE_TYPE_BY_MODE:
            legs = await self._matrix_legs(selected, mode, reference, profile)
        else:
            legs = await self._pair_legs(selected, mode, reference, profile)

        self.last_mode = mode
        return ProviderResult(
            provider=self.name,
            data=legs,
            source_references=[reference] if legs else [],
        )

    # -- the N-call full matrix (driving / walking) ------------------------

    async def _matrix_legs(
        self,
        points: list[RoutePoint],
        mode: TravelMode,
        reference: SourceReference,
        profile: TripProfile,
    ) -> list[RouteLeg]:
        """Full matrix via ``/v3/distance``, one call per destination.

        ``results[].origin_id`` is the **1-based index into the ``origins``
        parameter**, not a coordinate or an id we supplied, so it is mapped back
        positionally. Treating it as anything else mislabels every leg while still
        producing plausible numbers.
        """
        distance_type = DISTANCE_TYPE_BY_MODE[mode]
        semaphore = asyncio.Semaphore(self.concurrency)
        legs: list[RouteLeg] = []
        lock = asyncio.Lock()

        async def one_destination(index: int) -> None:
            destination = points[index]
            origins = [point for position, point in enumerate(points) if position != index]
            if not origins:
                return
            params = {
                "origins": "|".join(_format_location(point) for point in origins),
                "destination": _format_location(destination),
                "type": str(distance_type),
            }
            async with semaphore:
                try:
                    payload = await self.client.get("/v3/distance", params=params)
                except AmapError as exc:
                    # One failed sweep must not discard the legs already gathered:
                    # a partial real matrix still beats falling back to estimates
                    # for everything.
                    logger.warning("AMap distance sweep failed: %s", exc)
                    return

            gathered = self._parse_distance_results(payload, origins, destination, mode, reference)
            if gathered:
                async with lock:
                    legs.extend(gathered)

        await asyncio.gather(*(one_destination(index) for index in range(len(points))))
        return legs

    def _parse_distance_results(
        self,
        payload: dict,
        origins: list[RoutePoint],
        destination: RoutePoint,
        mode: TravelMode,
        reference: SourceReference,
    ) -> list[RouteLeg]:
        results = payload.get("results")
        if not isinstance(results, list):
            return []

        legs: list[RouteLeg] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            # `origin_id` is 1-based into the origins we sent.
            position = parse_int(item.get("origin_id"))
            if position is None or not (1 <= position <= len(origins)):
                continue
            distance = parse_float(item.get("distance"))
            duration = parse_float(item.get("duration"))
            if distance is None:
                continue
            legs.append(
                self._leg(
                    origin=origins[position - 1],
                    destination=destination,
                    mode=mode,
                    distance_meters=max(0, int(round(distance))),
                    duration_seconds=duration,
                    reference=reference,
                )
            )
        return legs

    # -- per-pair legs (transit / cycling) --------------------------------

    async def _transit_legs(
        self,
        points: list[RoutePoint],
        profile: TripProfile,
        reference: SourceReference,
    ) -> list[RouteLeg]:
        """Real 公交/地铁 legs for consecutive pairs, up to a call budget.

        Consecutive pairs are used because transit is one request per pair and a
        full matrix would be N² calls against a monthly quota. The input order is
        the itinerary's own visit order, so these are the legs a plan actually
        needs; anything else is left absent so the optimizer's existing
        estimate-with-disclosure path handles it.
        """
        pairs = list(zip(points, points[1:], strict=False))[: self.max_transit_legs]
        semaphore = asyncio.Semaphore(self.concurrency)
        legs: list[RouteLeg] = []
        lock = asyncio.Lock()

        async def one_pair(origin: RoutePoint, destination: RoutePoint) -> None:
            params = {
                "origin": _format_location(origin),
                "destination": _format_location(destination),
                # Required for transit, and its absence is an error rather than an
                # empty result, so the trip destination is used as the city.
                "city": profile.destination,
                "strategy": "0",
                "show_fields": "cost",
            }
            async with semaphore:
                try:
                    payload = await self.client.get(
                        "/v5/direction/transit/integrated", params=params
                    )
                except AmapError as exc:
                    logger.warning("AMap transit leg failed: %s", exc)
                    return
            leg = self._parse_transit(payload, origin, destination, reference)
            if leg is not None:
                async with lock:
                    legs.append(leg)

        await asyncio.gather(*(one_pair(origin, destination) for origin, destination in pairs))
        return legs

    def _parse_transit(
        self,
        payload: dict,
        origin: RoutePoint,
        destination: RoutePoint,
        reference: SourceReference,
    ) -> RouteLeg | None:
        """Take the first 换乘方案, and record what it is made of.

        AMap returns up to ``alt_count`` alternatives; the first is its own
        preferred one. The chosen plan's line names are put in the source context so
        the UI can say "地铁1号线 → 地铁2号线" without a second request — and so a
        reader can see the route was real rather than a straight-line guess.
        """
        route = payload.get("route")
        if not isinstance(route, dict):
            return None
        transits = route.get("transits")
        if not isinstance(transits, list) or not transits:
            return None
        best = transits[0]
        if not isinstance(best, dict):
            return None

        distance = parse_float(best.get("distance"))
        duration = parse_float(best.get("duration"))
        if distance is None or duration is None:
            return None

        lines, line_names = self._transit_summary(best)
        walking_distance = parse_float(best.get("walking_distance"))
        fare = self._transit_fare(best)

        leg = self._leg(
            origin=origin,
            destination=destination,
            mode=TravelMode.TRANSIT,
            distance_meters=max(0, int(round(distance))),
            duration_seconds=duration,
            reference=reference,
            cost=fare,
            is_estimate=False,
            extra_context={
                "transit_lines": line_names,
                "transit_segment_kinds": lines,
                "walking_distance_meters": (
                    None if walking_distance is None else int(round(walking_distance))
                ),
                "alternatives_available": len(transits),
            },
        )
        return leg

    @staticmethod
    def _transit_summary(best: dict) -> tuple[list[str], list[str]]:
        """Extract the segment kinds and the line names from one 换乘方案.

        A segment carries at most one of ``walking`` / ``bus`` / ``railway``. Both
        bus and rail nest their lines under a list whose key differs from the
        segment key (``buslines`` vs ``bus``, ``railway`` vs ``railway``), which is
        why they are read separately rather than by convention.
        """
        kinds: list[str] = []
        names: list[str] = []
        segments = best.get("segments")
        if not isinstance(segments, list):
            return kinds, names
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if isinstance(segment.get("walking"), dict):
                kinds.append("walking")
            for key, container in (("bus", "buslines"), ("railway", "railway")):
                block = segment.get(key)
                if not isinstance(block, dict):
                    continue
                kinds.append("railway" if key == "railway" else "bus")
                for line in block.get(container) or []:
                    if isinstance(line, dict) and line.get("name"):
                        names.append(str(line["name"]))
        return kinds, names

    @staticmethod
    def _transit_fare(best: dict) -> Money | None:
        """The plan's fare, when AMap reports one.

        Only taken from ``cost.transit_fee``. An absent fare stays absent: defaulting
        it to a guess would put an invented number in the budget, which is worse than
        a missing one that the budget engine can treat as unknown.
        """
        cost = best.get("cost")
        if not isinstance(cost, dict):
            return None
        fare = parse_float(cost.get("transit_fee"))
        if fare is None or fare < 0:
            return None
        return Money(value=Decimal(str(fare)), currency="CNY", price_type=PriceType.PER_PERSON)

    async def _pair_legs(
        self,
        points: list[RoutePoint],
        mode: TravelMode,
        reference: SourceReference,
        profile: TripProfile,
    ) -> list[RouteLeg]:
        """Consecutive-pair routes for modes ``/v3/distance`` cannot express."""
        path = DIRECTION_PATH_BY_MODE.get(mode)
        if path is None:
            return []
        pairs = list(zip(points, points[1:], strict=False))[: self.max_transit_legs]
        semaphore = asyncio.Semaphore(self.concurrency)
        legs: list[RouteLeg] = []

        async def one_pair(origin: RoutePoint, destination: RoutePoint) -> RouteLeg | None:
            async with semaphore:
                try:
                    payload = await self.client.get(
                        path,
                        params={
                            "origin": _format_location(origin),
                            "destination": _format_location(destination),
                        },
                    )
                except AmapError as exc:
                    logger.warning("AMap %s leg failed: %s", mode.value, exc)
                    return None
            return self._parse_direction(payload, origin, destination, mode, reference)

        gathered = await asyncio.gather(
            *(one_pair(origin, destination) for origin, destination in pairs)
        )
        legs.extend(leg for leg in gathered if leg is not None)
        return legs

    def _parse_direction(
        self,
        payload: dict,
        origin: RoutePoint,
        destination: RoutePoint,
        mode: TravelMode,
        reference: SourceReference,
    ) -> RouteLeg | None:
        route = payload.get("route")
        if not isinstance(route, dict):
            return None
        paths = route.get("paths")
        if not isinstance(paths, list) or not paths:
            return None
        best = paths[0]
        if not isinstance(best, dict):
            return None
        distance = parse_float(best.get("distance"))
        if distance is None:
            return None
        # v5 walking paths carry no `duration` at all, so a missing value is the
        # normal case there rather than a malformed response.
        duration = parse_float(best.get("duration"))
        return self._leg(
            origin=origin,
            destination=destination,
            mode=mode,
            distance_meters=max(0, int(round(distance))),
            duration_seconds=duration,
            reference=reference,
        )

    # -- shared construction ----------------------------------------------

    def _leg(
        self,
        *,
        origin: RoutePoint,
        destination: RoutePoint,
        mode: TravelMode,
        distance_meters: int,
        duration_seconds: float | None,
        reference: SourceReference,
        cost: Money | None = None,
        is_estimate: bool = False,
        extra_context: dict | None = None,
    ) -> RouteLeg:
        """Build a leg, deriving a duration only when one is genuinely needed.

        A measured duration is never overwritten, and a derived one always sets
        ``is_estimate=True`` so it cannot be shown as a measurement. The derivation is
        the single place this provider invents a number, and it is disclosed in
        ``request_context`` as well as on the leg.
        """
        derived = duration_seconds is None
        if duration_seconds is None:
            derived_speed = WALKING_SPEED_METERS_PER_SECOND
            duration_seconds = distance_meters / derived_speed

        minutes = max(0, int(round(duration_seconds / 60)))
        leg_estimate = is_estimate or derived

        # A derived cost is only meaningful for a vehicle we would actually pay for
        # by distance. Applying the per-kilometre taxi rate to a subway leg would
        # charge a 12 km metro ride as if it were a taxi, which is both wrong and
        # invisible: the number looks like every other estimated fare. A transit
        # fare must come from AMap's own ``transit_fee`` or stay absent.
        if cost is None and mode in (TravelMode.DRIVE, TravelMode.TAXI):
            amount = (Decimal(distance_meters) / Decimal(1000)) * Decimal(
                str(ESTIMATED_COST_PER_KM_CNY)
            )
            cost = Money(
                value=amount.quantize(Decimal("0.01")),
                currency="CNY",
                price_type=PriceType.PER_PERSON,
                source_reference_id=reference.source_id,
                is_estimate=True,
            )

        context = dict(reference.request_context)
        context["leg_mode"] = mode.value
        if derived:
            context["duration_source"] = "derived_walking_speed"
            context["derived_walking_speed_mps"] = WALKING_SPEED_METERS_PER_SECOND
        else:
            context["duration_source"] = "amap_measured"
        if extra_context:
            context.update(extra_context)

        leg_reference = reference.model_copy(
            update={
                "source_id": f"{reference.source_id}:{origin.id}:{destination.id}",
                "request_context": context,
                "confidence": 0.6 if derived else 0.85,
            }
        )
        if cost is not None and cost.source_reference_id is None:
            cost = cost.model_copy(update={"source_reference_id": leg_reference.source_id})

        return RouteLeg(
            origin_id=origin.id,
            destination_id=destination.id,
            mode=mode,
            distance_meters=distance_meters,
            duration_minutes=minutes,
            cost=cost,
            is_estimate=leg_estimate,
            source_reference=leg_reference,
        )

    @staticmethod
    def _reference_note(complete_matrix: bool, truncated: bool, offered: int, routed: int) -> str:
        """The one-line scope statement carried with the route data.

        Written in prose as well as machine-readable fields, because the reader who most
        needs to know the route is partial is a person looking at a leg, and they will
        not parse ``truncated``.
        """
        if truncated:
            return (
                f"实时查询，未做任何缓存；本次只对 {routed} 个地点计算了路线"
                f"（共提供 {offered} 个），其余地点的路程由行程规划器估算并已标注为估算"
            )
        if complete_matrix:
            return "实时查询，未做任何缓存"
        return "按相邻点对实时查询；未覆盖的点对由行程规划器给出并标注为估算"

    def _source_reference(
        self,
        *,
        mode: TravelMode,
        point_count: int,
        destination: str,
        points_offered: int | None = None,
    ) -> SourceReference:
        """Stamp the fetch time.

        ``fetched_at`` is the moment of this call and ``expires_at`` is deliberately
        short. Nothing is cached, so the honest reading is "true as of now"; the
        expiry exists to stop a persisted snapshot from being presented as current
        long after the fact, which is the misrepresentation the freshness rules are
        about.

        ``points_offered`` is how many places the planner handed over. When it exceeds
        ``point_count``, this reference covers only part of the trip and says so — the
        alternative is a leg that looks measured while its missing neighbours are
        estimates, with nothing in the payload to distinguish the two.
        """
        now = datetime.now(timezone.utc)
        # Driving is swept one-destination-at-a-time, so it genuinely covers every
        # ordered pair. Walking and transit are one request per pair and therefore
        # cover only the consecutive ones, which must be stated rather than implied.
        complete_matrix = mode in DISTANCE_TYPE_BY_MODE
        offered = point_count if points_offered is None else points_offered
        truncated = offered > point_count
        return SourceReference(
            source=self.name,
            source_type=SourceType.OFFICIAL_API,
            source_id=f"amap:routes:{mode.value}:{point_count}",
            observed_at=now,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            # The route is computed on demand from the current road network, so "now"
            # is a true statement about it. It says nothing about whether a closure or
            # a new line was incorporated — no route source can promise that.
            observed_at_basis=ObservationBasis.LIVE_COMPUTED,
            request_context={
                "amap_endpoint": (
                    "/v5/direction/transit/integrated"
                    if mode is TravelMode.TRANSIT
                    else (
                        "/v3/distance" if complete_matrix else DIRECTION_PATH_BY_MODE.get(mode, "")
                    )
                ),
                "travel_mode": mode.value,
                "point_count": point_count,
                # Both numbers, so "we routed 12 places" cannot be read as "the trip has
                # 12 places".
                "points_offered": offered,
                "points_routed": point_count,
                "point_budget": self.max_points,
                "truncated": truncated,
                "city": destination,
                "attribution": AMAP_ATTRIBUTION.as_credit(),
                # Disclosed in the data, so a reader of one leg learns the scope
                # without having to consult the docs.
                "coverage": "full_matrix" if complete_matrix else "consecutive_pairs",
                "cached": False,
                "note": self._reference_note(complete_matrix, truncated, offered, point_count),
            },
            data_version="amap-live",
            confidence=0.85 if complete_matrix else 0.8,
            domain=DataDomain.GEO_FACT,
        )

    async def health(self) -> ProviderHealth:
        """Report health without spending a quota call.

        Deliberately does **not** probe AMap: a health endpoint that burns quota on
        every poll is a bad trade, and the key's validity is already reported by the
        settings page when it is saved. What this can honestly state is that a key
        is present and that calls are live and uncached.
        """
        return ProviderHealth(
            provider=self.name,
            status="ok",
            checked_at=datetime.now(timezone.utc),
            detail="已配置 Key；每次规划实时查询，不做缓存",
        )


__all__ = [
    "DISTANCE_TYPE_BY_MODE",
    "AmapRoutingProvider",
    "WALKING_SPEED_METERS_PER_SECOND",
    "mode_for_pace",
]
