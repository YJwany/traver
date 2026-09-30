"""Routing provider backed by public OSRM deployments.

The one thing that has to be right here
--------------------------------------
A routing server either honours the profile you ask for or it silently gives you a
different one. The public demo server does the second: ``/table/v1/foot/...`` returns
HTTP 200 and byte-identical **car** durations. It does not fail — it answers confidently
with the wrong travel mode. Believing it would put a ~7-minute "walk" on an 8 km leg, which
is a fabricated fact about the traveller's day.

Verified live (2026-09-28) on 西湖 → 灵隐寺, ~5.5 km apart as the crow flies::

    service   profile    distance   duration   implied speed
    FOSSGIS   foot        8386 m     6708 s      4.5 km/h
    FOSSGIS   bike        8723 m     1866 s     16.8 km/h
    FOSSGIS   driving     9479 m      706 s     48.3 km/h
    demo      driving     9479 m      706 s     48.3 km/h
    demo      foot        9479 m      706 s     48.3 km/h   <- car results, mislabelled

The foot distance differs from the driving distance because pedestrians use a different
network — that difference is the evidence the profile is real, not a relabelling.

So this provider:

* resolves the deployment from its base URL and knows which profiles it honours;
* treats an **unknown** host as car-only, because guessing "it probably has a foot profile"
  is how a car duration ends up in a walking leg;
* when the requested profile is honoured, reports OSRM's **measured** duration;
* when it is not, either declines outright (transit, and any mode this deployment cannot
  route) or derives the duration from a stated speed and sets ``is_estimate=True``, so
  nothing downstream can present a car time as a pedestrian time.

Walking is the case worth having: on FOSSGIS it is a measured fact, and its mode is 步行,
the default for the relaxed city trips this tool plans.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping
from urllib.parse import urlsplit

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
from travel_api.providers.base import resolve_travel_mode
from travel_api.providers.free.attribution import (
    FOSSGIS_ATTRIBUTION,
    OSRM_ATTRIBUTION,
)
from travel_api.providers.free.http import FreeHttpClient

logger = logging.getLogger(__name__)

OSRM_DEMO_HOST = "router.project-osrm.org"
FOSSGIS_ROUTING_HOST = "routing.openstreetmap.de"

#: The deployment used when the operator configures nothing.
#:
#: FOSSGIS rather than the OSRM demo, because it serves strictly more: the demo answers
#: every request with a car route, so on it walking can only ever be a derived estimate,
#: while FOSSGIS has real foot and bike profiles worldwide. Both are community
#: infrastructure under the same kind of policy — valid user agent, at most 1 request per
#: second, no scraping — and ``free/http.py`` already enforces the rate limit for either.
DEFAULT_OSRM_BASE_URL = f"https://{FOSSGIS_ROUTING_HOST}"

#: TripConfig TravelMode -> the OSRM profile that genuinely describes it.
#:
#: TRANSIT is deliberately absent. No public OSRM deployment has a transit profile, because
#: transit routing needs timetable and route data that OSRM does not carry. Mapping it to
#: ``driving`` — which this table used to do — meant a transit request would return a car
#: duration under a bus label. 公交/地铁 is what 高德 adds, and until it is configured the
#: honest answer is to decline.
OSRM_PROFILE_BY_MODE: dict[TravelMode, str] = {
    TravelMode.WALK: "foot",
    TravelMode.DRIVE: "driving",
    TravelMode.TAXI: "driving",
}

#: Modes with no profile of their own that can still be served honestly from another
#: profile's **distance**.
#:
#: The distinction that makes this safe: distance is a property of the roads, duration is a
#: property of the mode. A car graph's road distance is a reasonable stand-in for the
#: pedestrian road distance — the same streets, minus the footpaths only pedestrians may use
#: — whereas its *duration* is about driving and must never be reused. So on a car-only
#: deployment a walking leg keeps a real measured distance, gets a duration derived at a
#: stated walking speed, and is flagged as an estimate. That is strictly better than
#: declining, because the distance really was measured.
#:
#: Transit is deliberately absent: a bus follows neither the driving route nor its distance,
#: so there is no proxy to borrow. That is the whole reason 高德 matters for this product.
DISTANCE_PROXY_PROFILE_BY_MODE: dict[TravelMode, str] = {
    TravelMode.WALK: "driving",
}

#: Average city walking speed, used to derive pedestrian durations when the deployment
#: cannot measure them. 4.5 km/h is a common planning figure.
WALKING_SPEED_METERS_PER_SECOND = 1.25

#: Conservative urban driving speed, used only when a deployment returns a distance with no
#: duration at all.
FALLBACK_DRIVING_SPEED_METERS_PER_SECOND = 25_000 / 3600

#: Rough per-kilometre taxi/transit fare, attached only to vehicle legs and always flagged as
#: an estimate.
ESTIMATED_COST_PER_KM_CNY = 2.5

#: The demo server rejects tables larger than 100 coordinates with HTTP 400 "Too many table
#: coordinates"; we stay well inside that.
OSRM_MAX_TABLE_COORDINATES = 100


@dataclass(frozen=True)
class PublicRoutingServer:
    """A public OSRM deployment: which profiles it honours, and where to ask for them.

    ``prefixes`` maps profile name to the path segment inserted before ``/table`` or
    ``/route``. FOSSGIS runs one instance per mode under ``/routed-*``; the OSRM demo runs a
    single car instance at the root. Modelling it as a prefix rather than assuming one URL
    shape is what lets both be described by the same code without special-casing.
    """

    host: str
    prefixes: Mapping[str, str]
    label: str
    #: How current the routing graph is. Timeliness is a hard requirement for this product,
    #: and a route is only as current as the road network it was computed on — so a reader
    #: has to be able to tell whether "today's route" reflects today's roads.
    freshness_note: str
    attribution: object = OSRM_ATTRIBUTION

    def honours(self, profile: str) -> bool:
        return profile in self.prefixes


#: Every public deployment this provider knows how to talk to.
PUBLIC_ROUTING_SERVERS: tuple[PublicRoutingServer, ...] = (
    PublicRoutingServer(
        host=FOSSGIS_ROUTING_HOST,
        prefixes={"driving": "/routed-car", "bike": "/routed-bike", "foot": "/routed-foot"},
        label="FOSSGIS e.V.",
        # From the operator's own page: "The routing data are updated roughly every two
        # days." Recorded rather than paraphrased so a reader can check it.
        freshness_note="OSM 路网数据约每两天重建一次（FOSSGIS 官方说明），比实时路况旧",
        attribution=FOSSGIS_ATTRIBUTION,
    ),
    PublicRoutingServer(
        host=OSRM_DEMO_HOST,
        prefixes={"driving": ""},
        label="OSRM 公共 demo",
        freshness_note="按请求实时计算，但图数据更新频率未公布，且只有驾车",
        attribution=OSRM_ATTRIBUTION,
    ),
)


def routing_server_for(
    base_url: str, *, honoured_profiles: str | None = None
) -> PublicRoutingServer:
    """Identify the deployment behind ``base_url``.

    An unrecognised host is reported as **car-only**. That is the pessimistic default on
    purpose: we cannot prove a self-hosted server has a foot profile, and the cost of being
    wrong in the other direction is a car duration presented as a walk. An operator who
    knows their deployment supports more can say so with ``honoured_profiles``, which is the
    same "bring your own configuration" contract the rest of the provider catalogue uses.

    ``honoured_profiles`` is a comma-separated list of profile names.
    """
    host = (urlsplit(base_url).hostname or "").lower()

    for server in PUBLIC_ROUTING_SERVERS:
        if host == server.host or host.endswith("." + server.host):
            if not honoured_profiles:
                return server
            # Operator override, but only *adding* profiles: the known server's own limits
            # still apply, because a claim about someone else's deployment cannot be
            # verified from here.
            declared = {part.strip() for part in honoured_profiles.split(",") if part.strip()}
            merged = dict(server.prefixes)
            for profile in declared:
                merged.setdefault(profile, f"/routed-{profile}")
            return PublicRoutingServer(
                host=server.host,
                prefixes=merged,
                label=server.label,
                freshness_note=server.freshness_note,
                attribution=server.attribution,
            )

    declared = {part.strip() for part in (honoured_profiles or "").split(",") if part.strip()} or {
        "driving"
    }
    return PublicRoutingServer(
        host=host,
        prefixes={profile: "" for profile in sorted(declared)},
        label=f"自建 OSRM（{host or '未知主机'}）",
        freshness_note="自建部署，图数据新鲜度未知，请自行确认更新频率",
        attribution=OSRM_ATTRIBUTION,
    )


def mode_for_pace(pace: Pace) -> TravelMode:
    """Pick a travel mode from the pace alone.

    Kept because the pace-to-mode map is a published behaviour worth naming, but
    :func:`resolve_travel_mode` is the only thing that should decide a trip's mode: it
    consults an explicit 公交/地铁 request first. This function cannot express transit at
    all, so a caller that reaches for it directly makes the headline capability
    unreachable again while every test of the transit code still passes.

    Naming that trap is the whole reason this is public rather than inlined — see
    ``test_amap_routes.py::test_the_pace_alone_can_never_select_transit``.
    """
    return TravelMode.WALK if pace in (Pace.RELAXED, Pace.BALANCED) else TravelMode.DRIVE


class OsmRoutingProvider:
    """Serves the routing capability from a public OSRM deployment."""

    #: The matrix aspect this provider decomposes into, used to report which parts it can
    #: serve. Kept in step with ``CAPABILITY_ASPECTS`` by a test rather than by hope.
    ROUTING_CAPABILITY = "calculate_routes"

    def __init__(
        self,
        name: str = "osrm",
        *,
        http: FreeHttpClient,
        base_url: str = DEFAULT_OSRM_BASE_URL,
        timeout_seconds: float = 30.0,
        ttl_seconds: int = 600,
        max_points: int = 25,
        honoured_profiles: str | None = None,
    ) -> None:
        self.name = name
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.ttl_seconds = ttl_seconds
        self.max_points = min(max_points, OSRM_MAX_TABLE_COORDINATES)
        self.server = routing_server_for(self.base_url, honoured_profiles=honoured_profiles)
        #: Set by ``calculate_routes``: the profile actually sent, or None if declined.
        self.last_profile: str | None = None
        #: The mode declined most recently, for health reporting and tests.
        self.last_declined_mode: TravelMode | None = None
        #: Whether the last response's durations were measured by OSRM or derived here.
        self.last_durations_measured: bool = False

    @property
    def UNSERVED_ASPECTS(self) -> dict[str, frozenset[str]]:  # noqa: N802 - declaration
        """Which routing aspects this provider cannot produce a leg for.

        Derived from the mode tables rather than written out, so it cannot drift from what
        ``calculate_routes`` actually does: adding a ``bike`` mode with a bike profile would
        remove ``cycling`` from this set without anyone remembering to edit it here, and
        removing a profile would add to it.

        ``cycling`` is unserved even where FOSSGIS honours a ``bike`` profile, and that is not
        an oversight. The matrix answers "can this tool plan it", not "could the server route
        it": ``TravelMode`` has no cycling member, so no itinerary can ever contain a bike
        leg. Reporting it as covered because the server happens to support it would be the
        same species of claim as crediting an empty stub with coverage.
        """
        from travel_api.providers.manifest import CAPABILITY_ASPECTS

        declared = set(CAPABILITY_ASPECTS.get(self.ROUTING_CAPABILITY, {}))
        served = {
            aspect
            for mode in TravelMode
            for aspect in (_ASPECT_BY_MODE.get(mode),)
            if aspect and self.serves_mode(mode)
        }
        return {self.ROUTING_CAPABILITY: frozenset(declared - served)}

    def serves_mode(self, mode: TravelMode) -> bool:
        """Whether a leg can be produced for ``mode`` at all — measured or derived.

        True when the deployment has the mode's own profile, or a profile whose *distance* is
        a defensible stand-in. Used both to decide whether to decline and to report aspects,
        so the two can never disagree about which modes this deployment covers.
        """
        return self._request_profile_for(mode) is not None

    def _request_profile_for(self, mode: TravelMode) -> str | None:
        """The profile to ask for, or None if this mode cannot be served honestly.

        Returns the mode's own profile where the deployment has one — then the durations come
        back measured for the right mode. Falls back to a distance proxy where one exists,
        which yields a real distance and a derived duration.
        """
        own = OSRM_PROFILE_BY_MODE.get(mode)
        if own is not None and self.server.honours(own):
            return own
        proxy = DISTANCE_PROXY_PROFILE_BY_MODE.get(mode)
        if proxy is not None and self.server.honours(proxy):
            return proxy
        return None

    async def calculate_routes(
        self, profile: TripProfile, points: list[RoutePoint]
    ) -> ProviderResult:
        if len(points) < 2:
            return ProviderResult(provider=self.name, data=[])

        mode = resolve_travel_mode(profile)
        selected = points[: self.max_points]
        coordinates = ";".join(f"{point.longitude:.6f},{point.latitude:.6f}" for point in selected)

        request_profile = self._request_profile_for(mode)
        if request_profile is None:
            # Decline rather than substitute. The alternative here would put a number in the
            # plan that no source measured for this mode — a car duration for a bus. An empty
            # result is the honest answer, and the aggregation layer already handles a source
            # that contributes nothing.
            self.last_declined_mode = mode
            self.last_profile = None
            logger.info(
                "osrm_declined_mode mode=%s server=%s honoured=%s",
                mode.value,
                self.server.host,
                sorted(self.server.prefixes),
            )
            return ProviderResult(provider=self.name, data=[], source_references=[])

        # Whether the durations in this response describe the requested mode. A proxy
        # profile's durations describe *its* mode, so they must not be reported as measured
        # for this one — the whole point of the foot/driving distinction.
        own_profile = OSRM_PROFILE_BY_MODE.get(mode)
        durations_for_this_mode = request_profile == own_profile

        prefix = self.server.prefixes[request_profile]
        url = f"{self.base_url}{prefix}/table/v1/{request_profile}/{coordinates}"
        payload = await self.http.get_json(url, params={"annotations": "distance,duration"})

        if not isinstance(payload, dict) or payload.get("code") != "Ok":
            code = payload.get("code") if isinstance(payload, dict) else None
            raise RuntimeError(f"OSRM returned code {code!r}")

        self.last_profile = request_profile
        self.last_declined_mode = None
        measured = durations_for_this_mode and _matrix_has_durations(payload)
        self.last_durations_measured = measured
        reference = self._source_reference(
            mode=mode,
            profile_name=request_profile,
            point_count=len(selected),
            measured=measured,
        )
        legs = self._build_legs(selected, payload, mode, reference, measured=measured)
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
        *,
        measured: bool,
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
                    mode,
                    distance_meters,
                    durations,
                    origin_index,
                    destination_index,
                    measured=measured,
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
        *,
        measured: bool,
    ) -> tuple[int, bool]:
        """Return (minutes, is_estimate).

        Measured wins when the deployment actually routed this mode. Otherwise the duration
        is derived from a stated speed **for that mode** and flagged, so a derived number is
        never presented as an observation.
        """
        if measured:
            duration = _matrix_value(durations, origin_index, destination_index)
            if duration is not None:
                return max(1, int(round(duration / 60))), False

        if mode is TravelMode.WALK:
            seconds = distance_meters / WALKING_SPEED_METERS_PER_SECOND
            return max(1, int(round(seconds / 60))), True

        # Distance known, duration missing: derive at a conservative urban speed.
        seconds = distance_meters / FALLBACK_DRIVING_SPEED_METERS_PER_SECOND
        return max(1, int(round(seconds / 60))), True

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

    def _source_reference(
        self, *, mode: TravelMode, profile_name: str, point_count: int, measured: bool
    ) -> SourceReference:
        now = datetime.now(timezone.utc)
        return SourceReference(
            source=self.name,
            source_type=SourceType.OPEN_DATA,
            source_id=f"osrm:{self.server.host}:{profile_name}:{point_count}",
            observed_at=now,
            observed_at_basis=ObservationBasis.LIVE_COMPUTED,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            request_context={
                "osrm_profile": profile_name,
                "osrm_server": self.server.host,
                "osrm_server_label": self.server.label,
                "travel_mode": mode.value,
                "point_count": point_count,
                "endpoint": self.base_url,
                "attribution": str(getattr(self.server.attribution, "as_credit", lambda: "")()),
                "data_freshness": self.server.freshness_note,
                # Disclose how the duration was obtained in the data itself, not just in
                # docs: a reader of a single leg must be able to tell an observation from a
                # deriviation without knowing which server was configured.
                "duration_source": (
                    f"osrm_{profile_name}_profile" if measured else f"derived_{mode.value}_speed"
                ),
                "duration_is_measured": measured,
                "walking_speed_mps": WALKING_SPEED_METERS_PER_SECOND
                if (mode is TravelMode.WALK and not measured)
                else None,
                "profiles_honoured": sorted(self.server.prefixes),
            },
            data_version=f"osrm-live-{profile_name}",
            # A measured duration is a computation over a real network; a derived one is an
            # assumption on top of a measured distance.
            confidence=0.75 if measured else 0.55,
            domain=DataDomain.GEO_FACT,
        )

    async def health(self) -> ProviderHealth:
        profiles = sorted(self.server.prefixes)
        measured = "实测" if self.last_durations_measured else "推算"
        return ProviderHealth(
            provider=self.name,
            status="healthy",
            latency_ms=0.0,
            detail=(
                f"{self.server.label}（{self.server.host}），可用 profile={profiles}；"
                f"上次请求的时长来源={measured}"
            ),
        )


#: TravelMode -> the capability-matrix aspect it corresponds to.
#:
#: Used only to report coverage, never to route. Kept next to the mode table so the two
#: cannot drift into disagreeing about which modes are served.
_ASPECT_BY_MODE: dict[TravelMode, str] = {
    TravelMode.WALK: "walking",
    TravelMode.DRIVE: "driving",
    TravelMode.TAXI: "driving",
    TravelMode.TRANSIT: "transit",
}


def _matrix_has_durations(payload: dict) -> bool:
    """Whether the response carries any real duration.

    OSRM returns ``durations: null`` (or rows of nulls) when the profile cannot route a pair
    — the honest failure. Distinguishing that from a populated matrix is what decides whether
    a leg's duration is reported as measured or derived.
    """
    durations = payload.get("durations")
    if not isinstance(durations, list):
        return False
    return any(
        isinstance(row, list) and any(value is not None for value in row) for row in durations
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
