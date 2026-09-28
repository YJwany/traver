from __future__ import annotations

from typing import Protocol, runtime_checkable

from travel_api.domain.models import (
    Attraction,
    Pace,
    ProviderHealth,
    ProviderResult,
    RoutePoint,
    TravelMode,
    TripProfile,
)


def resolve_travel_mode(profile: TripProfile) -> TravelMode:
    """Which mode a routing provider should answer for this trip.

    An explicit ``travel_mode`` wins. Otherwise it is derived from the pace, which keeps
    every trip planned before the field existed on exactly the route it had.

    This lives here rather than in each provider because the two providers must agree:
    the registry calls both for one aggregation, and if they disagreed about the mode the
    resulting legs would be measured against different assumptions while being merged into
    one itinerary. Deriving it in two places previously meant each could drift, and only
    one of them could ever produce a 公交/地铁 leg — so a request for transit would have
    silently returned driving distances from the other source.

    A mode no provider can serve is not an error here. Providers decline it themselves
    (see ``OSRM_PROFILE_BY_MODE``, which has no transit entry), the capability matrix
    already reports the gap, and the planner falls back to labelled estimates — which is
    the honest outcome rather than a refusal to plan the trip at all.
    """
    if profile.travel_mode is not None:
        return profile.travel_mode
    return TravelMode.WALK if profile.pace in (Pace.RELAXED, Pace.BALANCED) else TravelMode.DRIVE


# Capability protocols.
#
# A provider implements only the capabilities it can actually serve. The
# registry routes each aggregation call to the providers that declare the
# matching capability, so a geo-only source (OpenStreetMap) never has to fake
# commercial data it does not have, and a weather-only source never has to
# return empty hotel lists.


@runtime_checkable
class HotelProvider(Protocol):
    """Finds hotels. Says nothing about whether it can price them.

    Finding a hotel and quoting a room rate are two different abilities from two
    different kinds of source, so they are two protocols. They used to be one, which
    made the second method a condition of having the first: a source that could list
    hotels but had no rates — 高德 returns hotel POIs and no prices — failed the
    ``isinstance`` check and was therefore never routed *at all*, silently
    contributing no hotels while its manifest advertised ``search_hotels``.

    Split deliberately, so a source is judged on the methods it actually implements.
    """

    name: str

    async def search_hotels(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class RoomOfferProvider(Protocol):
    """Quotes room rates for hotels it did not necessarily discover.

    Separate from ``HotelProvider`` for the same reason: rates are a commercial
    capability that most sources lack, and lacking them must not disable hotel
    search. A source that has no rates is not broken — it is simply not asked for
    prices, and the budget layer marks those costs as estimates.
    """

    name: str

    async def search_room_offers(
        self, hotel_ids: list[str], profile: TripProfile
    ) -> ProviderResult: ...


@runtime_checkable
class FoodProvider(Protocol):
    name: str

    async def search_restaurants(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class AttractionProvider(Protocol):
    name: str

    async def search_attractions(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class RouteProvider(Protocol):
    name: str

    async def calculate_routes(
        self, profile: TripProfile, points: list[RoutePoint]
    ) -> ProviderResult: ...


@runtime_checkable
class WeatherProvider(Protocol):
    name: str

    async def search_weather(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class HolidayProvider(Protocol):
    """Supplies public holidays that overlap the trip.

    Kept as its own capability because it is a calendar fact, not a POI or a
    forecast: a source can serve holidays without serving anything else.
    """

    name: str

    async def search_holidays(self, profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class ContentProvider(Protocol):
    """Enriches already-aggregated entities with encyclopedic text.

    Separate from ``AttractionProvider`` because it consumes entities that other
    providers produced rather than discovering new ones, so it runs after
    aggregation and entity resolution.
    """

    name: str

    async def enrich_attractions(
        self, attractions: list[Attraction], profile: TripProfile
    ) -> ProviderResult: ...


@runtime_checkable
class PlaceResolverProvider(Protocol):
    """Locates names the traveller's own material mentioned, and says what it did.

    Its own capability rather than part of ``AttractionProvider``, for the reason the
    capability *is* different: searching for POIs asks a dataset what exists near a point,
    while this asks a geocoder where a specific name is. A source can obviously do one and
    not the other — the free stack's POI search (Overpass) has no geocoder at all, and AMap's
    geocoder was written before any of this existed.

    There is no location parameter, and its absence is deliberate. The disambiguation a bare
    name needs comes from the trip's own destination, and each implementation resolves that
    from ``profile`` with *the same geocoder its POI search uses*. Passing the centre in would
    let a caller point the lookups at one coordinate while the POIs were gathered around
    another, and nothing in the result would show the disagreement.

    The implementation is responsible for using that location — by scoping the query, by
    rejecting a result outside it, or both — and for reporting a resolution it rejected rather
    than dropping it silently.
    """

    name: str

    async def resolve_places(self, names: list[str], profile: TripProfile) -> ProviderResult: ...


@runtime_checkable
class HealthProvider(Protocol):
    name: str

    async def health(self) -> ProviderHealth: ...


# Method name -> the capability a provider must expose to receive that call.
#
# Each entry names the *minimum* a provider must implement to be routed that method.
# Note ``search_hotels`` and ``search_room_offers`` map to different protocols: a
# source that lists hotels without quoting rates must still receive hotel calls, and
# requiring both would disqualify it from either.
CAPABILITY_BY_METHOD: dict[str, type] = {
    "search_hotels": HotelProvider,
    "search_room_offers": RoomOfferProvider,
    "search_restaurants": FoodProvider,
    "search_attractions": AttractionProvider,
    "calculate_routes": RouteProvider,
    "search_weather": WeatherProvider,
    "search_holidays": HolidayProvider,
    "enrich_attractions": ContentProvider,
    "resolve_places": PlaceResolverProvider,
}

#: Capabilities a provider implements structurally but does not actually serve.
#:
#: Implementing a method is not the same as answering it. ``search_room_offers`` exists on
#: the interface because some sources do quote rates; a source without them must still
#: satisfy the interface, and its correct behaviour is to return an empty result rather
#: than invent a price. That empty result is right, but it is not coverage — and nothing in
#: ``isinstance`` can tell the two apart.
#:
#: Left undeclared, the capability matrix counted Overpass as *fully covering* room offers
#: while its own manifest caveat said it had no rates, so the settings page advertised a
#: capability that returns nothing. The registry now consults this set when reporting what a
#: deployment can do, which makes "I cannot serve this" a statement a provider can make
#: instead of one a reader has to infer from an empty list.
#:
#: This is for capabilities the protocol union forces a provider to carry. A source that
#: genuinely serves a capability must never list it here — an unnecessary entry would hide
#: real coverage, which is the same lie told in the other direction.
UNSERVED_CAPABILITIES: frozenset[str] = frozenset()


def unserved_capabilities_of(provider: object) -> frozenset[str]:
    """What ``provider`` declares it cannot serve, tolerating a non-conforming value.

    Read defensively so a test double or a duck-typed provider cannot break the catalogue:
    a missing declaration means "nothing extra is unserved", which is the pre-existing
    behaviour.
    """
    declared = getattr(provider, "UNSERVED_CAPABILITIES", None)
    return declared if isinstance(declared, frozenset) else frozenset()


def unserved_aspects_of(provider: object, capability: str) -> frozenset[str]:
    """Aspects of ``capability`` that ``provider`` cannot serve.

    ``UNSERVED_CAPABILITIES`` answers "can this provider serve this capability at all".
    Some capabilities decompose further, and a provider can serve the whole while being
    unable to serve one part of it: a routing server with a car graph only serves
    ``calculate_routes`` while being unable to produce a pedestrian leg.

    That is exactly the case the capability matrix exists to expose. Without a way for the
    provider to say which parts it cannot do, the catalogue can only repeat the manifest's
    static claim — and the claim has to hold for every possible configuration, so it ends up
    either overstating a car-only server or understating a full one.

    Like ``UNSERVED_CAPABILITIES``, this is read defensively and may only ever *reduce*
    reported coverage. A declaration that added coverage could be used to claim an aspect a
    provider does not serve, so the direction is enforced by the caller rather than trusted.
    """
    declared = getattr(provider, "UNSERVED_ASPECTS", None)
    if not isinstance(declared, dict):
        return frozenset()
    aspects = declared.get(capability)
    return aspects if isinstance(aspects, frozenset) else frozenset()


class TravelProvider(
    HotelProvider,
    RoomOfferProvider,
    FoodProvider,
    AttractionProvider,
    RouteProvider,
    WeatherProvider,
    HolidayProvider,
    HealthProvider,
    Protocol,
):
    """Full-surface provider: serves every category from one dataset.

    Kept for mock/fixture providers that intentionally cover all categories.
    """

    name: str
