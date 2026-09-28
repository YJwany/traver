from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Protocol

from travel_api.domain.models import (
    TRAVEL_MODE_LABELS,
    Attraction,
    BudgetStrategyName,
    HolidayCalendar,
    Hotel,
    HotelOffer,
    Itinerary,
    Pace,
    ProviderHealth,
    ProviderResult,
    Restaurant,
    RoutePoint,
    TravelMode,
    TripPreview,
    TripProfile,
    WeatherForecast,
)
from travel_api.providers.base import resolve_travel_mode
from travel_api.providers.registry import ProviderRegistry
from travel_api.services.budget_engine import BudgetEngine
from travel_api.services.decision_engine import score_hotel_offers
from travel_api.services.entity_resolution import (
    deduplicate_attractions,
    deduplicate_hotels,
    deduplicate_restaurants,
)
from travel_api.services.freshness import FreshnessService
from travel_api.services.normalization import (
    normalize_attractions,
    normalize_hotels,
    normalize_restaurants,
)
from travel_api.services.route_optimizer import RouteOptimizer

logger = logging.getLogger(__name__)

#: Upper bound on the routable point set. One OSRM table request returns the
#: full matrix, so this caps both request size and matrix growth (n^2 legs).
MAX_ROUTE_POINTS = 40


class ProgressReporter(Protocol):
    """Where the planner announces the boundaries it crosses.

    A narrow protocol rather than the job registry itself, so planning does not depend on
    the job machinery — the synchronous endpoint passes nothing and behaves exactly as
    before. The two methods are separate because a step is genuinely open for a while: the
    reporter publishes on *open* so a slow step is visible while it is slow, which is the
    only moment progress information is worth anything.
    """

    async def step(self, name: str, detail: str = "") -> None: ...

    async def finish_step(self) -> None: ...


class _NullProgress:
    """The no-op reporter, so every ``report`` call site can be unconditional."""

    async def step(self, name: str, detail: str = "") -> None:
        del name, detail

    async def finish_step(self) -> None:
        return None


def _progress_of(reporter) -> ProgressReporter:
    return reporter if reporter is not None else _NullProgress()


def _audit_entry(operation: str, result: ProviderResult) -> dict:
    payload = []
    for item in result.data:
        if hasattr(item, "model_dump"):
            payload.append(item.model_dump(mode="json", exclude_computed_fields=True))
        else:
            payload.append(item)
    return {
        "provider": result.provider,
        "operation": operation,
        "status": result.status,
        "errors": result.errors,
        "payload": payload,
        "data_count": len(result.data),
    }


#: Note kinds that read as a caution rather than a suggestion. Ordered so a warning is
#: never downgraded to a tip by a later match.
_CAUTION_KINDS = ("warning", "opening_hours")


def _build_ingested_notes(
    ingestions,
    *,
    hotels: list[Hotel],
    restaurants: list[Restaurant],
    attractions: list[Attraction],
) -> list[dict]:
    """Attach the traveller's own notes to the entities they are about.

    Two decisions here are worth stating.

    **Notes are matched, never merged.** A note's place name is compared against the
    entities already aggregated from structured sources; a match attaches an advisory
    note to that entity. The entity's own fields are left untouched. Ingested content is
    somebody's account of a trip — a remembered price, an impression of how long to
    stay — so letting it overwrite a measured value would launder an opinion into a fact.
    That is the same rule the budget layer follows for estimates.

    **An unmatched mention is still reported.** A place the source recommended and that
    our structured sources do not have is genuinely interesting: it is a gap in the data,
    and hiding it would make the note look fully used. It is listed with
    ``matched`` false so a reader can see the plan did not act on it and why — the place
    has no coordinates, so it cannot be routed.
    """
    if not ingestions:
        return []

    # Names to match against, longest first so 「灵隐寺」 is preferred over a shorter
    # substring of it that another entity happens to share.
    candidates: list[tuple[str, str, str]] = []
    for entity, kind in (
        *((hotel, "hotel") for hotel in hotels),
        *((restaurant, "restaurant") for restaurant in restaurants),
        *((attraction, "attraction") for attraction in attractions),
    ):
        name = getattr(entity, "canonical_name", None)
        if name:
            candidates.append((name, str(entity.id), kind))
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    notes: list[dict] = []
    for ingestion in ingestions:
        for fact in ingestion.facts:
            # One matching rule for every kind of fact, applied in two passes.
            #
            # Checking the *evidence sentence* for an entity name matters more than it
            # looks. Restricting matching to extracted place names meant a note saying
            # 「住成都天际酒店」 never attached to that hotel, because 酒店 is not a place
            # suffix the extractor recognises — the name was right there in the sentence
            # the whole time. So the sentence is checked first, and the extracted place
            # name is the fallback for when the note names somewhere we recorded under a
            # longer or shorter form.
            place = str(fact.value.get("name") or "") if fact.kind.value == "place_mention" else ""
            matched_name = matched_id = matched_kind = None
            for name, entity_id, kind in candidates:
                if name in fact.evidence:
                    matched_name, matched_id, matched_kind = name, entity_id, kind
                    break
            if matched_name is None and place:
                for name, entity_id, kind in candidates:
                    if place in name or name in place:
                        matched_name, matched_id, matched_kind = name, entity_id, kind
                        break

            notes.append(
                {
                    "ingestion_id": ingestion.id,
                    "platform": ingestion.platform_label,
                    "url": ingestion.url,
                    "title": ingestion.title,
                    "author": ingestion.author,
                    "kind": fact.kind.value,
                    "text": fact.text,
                    "evidence": fact.evidence,
                    "confidence": fact.confidence.value,
                    "reason": fact.reason,
                    #: Reported so the UI can say how old the advice is, or that its age
                    #: is unknown. Never omitted: an undated note and a dated one look
                    #: identical otherwise.
                    "published_at": (
                        ingestion.published_at.isoformat() if ingestion.published_at else None
                    ),
                    "ingested_at": ingestion.ingested_at.isoformat(),
                    "data_age_is_known": ingestion.published_at is not None,
                    "matched": matched_name is not None,
                    "matched_entity_id": matched_id,
                    "matched_entity_name": matched_name,
                    "matched_entity_kind": matched_kind,
                }
            )
    # Cautions first, so a closed-day note is not buried under a dozen tips; then by how
    # confident the reading was.
    notes.sort(key=lambda item: (item["kind"] not in _CAUTION_KINDS, item["kind"], item["text"]))
    return notes


async def _safe_call(registry: ProviderRegistry, provider, method: str, *args) -> ProviderResult:
    try:
        return await registry.call(provider, method, *args)
    except Exception as exc:
        logger.warning(
            "provider call failed: %s.%s: %s",
            provider.name,
            method,
            exc,
            # Do not hardcode request_id here: the logging filter fills it in
            # from the current request context. Hardcoding it made the most
            # useful diagnostic line uncorrelatable with the request that hit it.
            extra={"provider": provider.name, "method": method},
        )
        return ProviderResult(status="degraded", provider=provider.name, errors=[str(exc)])


async def _call_capability(registry: ProviderRegistry, method: str, *args) -> list[ProviderResult]:
    """Dispatch one capability to every provider that declares it."""
    providers = registry.providers_for(method)
    if not providers:
        return []
    return list(
        await asyncio.gather(
            *[_safe_call(registry, provider, method, *args) for provider in providers]
        )
    )


def _collect(results: list[ProviderResult], expected_type: type) -> list:
    return [item for result in results for item in result.data if isinstance(item, expected_type)]


#: What would actually close the gap, per mode. A single generic sentence is not
#: actionable: telling someone who asked to walk that 公交/地铁 needs a 高德 key points them
#: at the wrong remedy, and the free routing tier already covers walking and driving.
_TRAVEL_MODE_REMEDIES: dict[str, str] = {
    TravelMode.WALK.value: "免费路线源（OSRM）的步行 profile 可以算，通常是网络或限流问题",
    TravelMode.DRIVE.value: "免费路线源（OSRM）的驾车 profile 可以算，通常是网络或限流问题",
    TravelMode.TAXI.value: "免费路线源（OSRM）的驾车 profile 可以算，通常是网络或限流问题",
    TravelMode.TRANSIT.value: "接入高德 Web 服务 Key（公交/地铁是免费源完全没有的能力）",
}


def _disclose_unserved_travel_mode(
    itinerary: Itinerary, profile: TripProfile, routes: list
) -> None:
    """Say when the mode the traveller asked for is not what the plan was built on.

    Choosing 公交 / 地铁 without a 高德 key is a supported, non-failing path: the free routers
    decline transit rather than substituting a car time, and the itinerary's travel times
    become distance-based estimates. Those estimates are disclosed — but only as a count
    ("N leg(s) use distance-based travel estimates"), which does not connect them to the
    request that caused them. A traveller who explicitly picked 公交 / 地铁 sees estimated
    minutes and no explanation, and the most likely reading is that the numbers are
    measured.

    So the reason is attached to the consequence, in the same shape the capability matrix
    uses for a gap: what was asked, what happened, and what would change it. The remedy is
    per-mode because a wrong remedy is worse than none — it sends someone to configure a
    credential that would not have helped.

    Only fires for an *explicit* request. A pace-derived mode is the planner's own default,
    not something that went unserved, and the generic estimate disclosure already covers
    it; warning there would put a caveat on the default zero-configuration plan, which is
    the one path that should read as complete.
    """
    if profile.travel_mode is None:
        return
    if routes:
        return

    mode = resolve_travel_mode(profile)
    label = TRAVEL_MODE_LABELS.get(mode.value, mode.value)
    remedy = _TRAVEL_MODE_REMEDIES.get(mode.value, "接入一个支持该方式的来源")
    itinerary.conflicts.append(
        f"你要求按「{label}」规划，但没有可用的数据源能算这种路线，"
        f"因此本行程的路程时间是按距离估算的，不是实测结果。"
        f"{remedy}之后，这些时间会变成实测值。"
    )


def _build_route_points(
    selected_hotel: Hotel | None,
    attractions: list[Attraction],
    restaurants,
    profile: TripProfile,
    *,
    limit: int = MAX_ROUTE_POINTS,
) -> list[RoutePoint]:
    """Assemble the point set handed to the routing provider.

    The optimizer looks legs up by (origin_id, destination_id), so every place it
    may actually schedule must be in the matrix. Two properties matter:

    * Selection is *not* plain list order. The optimizer consumes attractions and
      restaurants from the front of each list, so a single shared allowance filled
      by one category leaves the others unroutable (previously the first 17
      attractions consumed everything, and every restaurant leg silently fell back
      to a hardcoded estimate).
    * The per-category budget is derived from how many entries an itinerary of
      this length can consume, with margin for candidates skipped because they do
      not fit, rather than a fixed count.
    """
    days = max(profile.nights, 1)
    attractions_per_day = 1 if profile.pace is Pace.RELAXED else 2
    # Pessimistic: the optimizer may walk past several candidates per slot before
    # finding one that fits, so allow generous margin over the scheduled count.
    attraction_budget = max(4, attractions_per_day * days * 3)
    restaurant_budget = max(4, days * 4)

    points: list[RoutePoint] = []
    seen: set = set()

    def add(entity) -> bool:
        if entity is None or entity.id in seen:
            return False
        coordinates = entity.location.coordinates
        seen.add(entity.id)
        points.append(
            RoutePoint(
                id=entity.id,
                latitude=coordinates.latitude,
                longitude=coordinates.longitude,
            )
        )
        return True

    add(selected_hotel)

    # Interleave so no single category can starve the others.
    attraction_slice = attractions[:attraction_budget]
    restaurant_slice = restaurants[:restaurant_budget]
    for index in range(max(len(attraction_slice), len(restaurant_slice))):
        if index < len(attraction_slice):
            add(attraction_slice[index])
        if index < len(restaurant_slice):
            add(restaurant_slice[index])

    return points[:limit]


async def build_preview(
    profile: TripProfile,
    registry: ProviderRegistry,
    budget_strategy: BudgetStrategyName | str = BudgetStrategyName.BALANCED,
    budget_engine: BudgetEngine | None = None,
    freshness_service: FreshnessService | None = None,
    ingestions=None,
    progress=None,
) -> TripPreview:
    reporter = _progress_of(progress)

    # Each step below is announced at a boundary the code genuinely crosses, and carries
    # the names of the sources being asked. A progress indicator that advances on a timer
    # is worse than none: it moves while nothing happens and then jumps, which teaches the
    # reader to ignore it. These move only when work actually moves.
    await reporter.step("collect_places", "酒店、餐厅、景点、天气、节假日")
    (
        hotel_results,
        restaurant_results,
        attraction_results,
        weather_results,
        holiday_results,
    ) = await asyncio.gather(
        _call_capability(registry, "search_hotels", profile),
        _call_capability(registry, "search_restaurants", profile),
        _call_capability(registry, "search_attractions", profile),
        _call_capability(registry, "search_weather", profile),
        _call_capability(registry, "search_holidays", profile),
    )
    await reporter.finish_step()

    hotels = deduplicate_hotels(normalize_hotels(_collect(hotel_results, Hotel)))
    hotel_ids = [str(hotel.id) for hotel in hotels]
    # Announced even when there is nothing to ask, so the step list is the same shape on
    # every run and a missing step cannot be misread as a silent failure.
    await reporter.step(
        "room_offers",
        f"{len(hotel_ids)} 家酒店" if hotel_ids else "无候选酒店，跳过",
    )
    offer_results = (
        await _call_capability(registry, "search_room_offers", hotel_ids, profile)
        if hotel_ids
        else []
    )
    offers = _collect(offer_results, HotelOffer)
    await reporter.finish_step()

    restaurants = deduplicate_restaurants(
        normalize_restaurants(_collect(restaurant_results, Restaurant))
    )
    attractions = deduplicate_attractions(
        normalize_attractions(_collect(attraction_results, Attraction))
    )

    # Content enrichment runs *after* dedup so the same article is fetched once
    # per surviving attraction, and only for entities that carry an explicit
    # OSM wikipedia/wikidata link (the provider never guesses a match).
    await reporter.step(
        "enrich_attractions",
        f"{len(attractions)} 个景点" if attractions else "无候选景点，跳过",
    )
    enrichment_results = (
        await _call_capability(registry, "enrich_attractions", attractions, profile)
        if attractions
        else []
    )
    if enrichment_results:
        enriched = _collect(enrichment_results, Attraction)
        # Only accept the enriched copies for entities that actually exist, so a
        # misbehaving provider cannot introduce new attractions.
        known = {attraction.id for attraction in attractions}
        replacements = {item.id: item for item in enriched if item.id in known}
        attractions = [replacements.get(item.id, item) for item in attractions]
    await reporter.finish_step()

    decisions = score_hotel_offers(hotels, offers, profile)
    selected_offer = None
    if decisions and not decisions[0].failed_constraints:
        selected_offer = next(
            (offer for offer in offers if offer.id == decisions[0].candidate_id), None
        )
    selected_hotel = next(
        (hotel for hotel in hotels if selected_offer and hotel.id == selected_offer.hotel_id),
        None,
    )

    # Routing happens *after* the candidate set exists: a routing service needs
    # real coordinates, which are only known once POIs have been aggregated.
    route_points = _build_route_points(selected_hotel, attractions, restaurants, profile)
    # The slowest step for most deployments, and the reason this whole job mechanism
    # exists — a single upstream query was measured at 36.5 s, so the count is reported
    # to make a long wait legible rather than mysterious.
    await reporter.step(
        "calculate_routes",
        f"{len(route_points)} 个途经点" if len(route_points) >= 2 else "途经点不足，跳过",
    )
    route_results = (
        await _call_capability(registry, "calculate_routes", profile, route_points)
        if len(route_points) >= 2
        else []
    )
    routes = [
        item for result in route_results for item in result.data if hasattr(item, "origin_id")
    ]
    await reporter.finish_step()

    await reporter.step("build_itinerary", "预算与每日路线")
    budget = (budget_engine or BudgetEngine()).calculate(
        profile,
        selected_offer,
        restaurants,
        attractions,
        routes,
        strategy=budget_strategy,
    )
    itinerary = RouteOptimizer().optimize(
        profile,
        selected_hotel.id if selected_hotel else None,
        (
            selected_hotel.location.coordinates.latitude,
            selected_hotel.location.coordinates.longitude,
        )
        if selected_hotel
        else (0.0, 0.0),
        attractions,
        restaurants,
        routes,
    )
    _disclose_unserved_travel_mode(itinerary, profile, routes)
    weather = next(iter(_collect(weather_results, WeatherForecast)), None)
    holidays = next(iter(_collect(holiday_results, HolidayCalendar)), None)
    await reporter.finish_step()

    await reporter.step("collect_health", "各数据源状态")
    health: list[ProviderHealth] = await registry.health()
    await reporter.finish_step()
    refs = [
        ref
        for result in [
            *hotel_results,
            *offer_results,
            *restaurant_results,
            *attraction_results,
            *route_results,
            *weather_results,
            *holiday_results,
            *enrichment_results,
        ]
        for ref in result.source_references
    ]
    # Each reference is paired with the kind of fact it carries, because whether an age
    # is acceptable is a property of the fact: an hour-old forecast is stale, an
    # hour-old restaurant listing is not. A reference on its own cannot say which it is.
    typed_refs: list[tuple[str, object]] = [
        ("hotel", ref) for result in hotel_results for ref in result.source_references
    ]
    typed_refs += [
        ("hotel_offer", ref) for result in offer_results for ref in result.source_references
    ]
    typed_refs += [
        ("restaurant", ref)
        for result in [*restaurant_results, *enrichment_results]
        for ref in result.source_references
    ]
    typed_refs += [
        ("attraction", ref) for result in attraction_results for ref in result.source_references
    ]
    typed_refs += [("route", ref) for result in route_results for ref in result.source_references]
    typed_refs += [
        ("weather", ref) for result in weather_results for ref in result.source_references
    ]
    # Holidays are paired even though ``MAX_AGE_SECONDS`` gives them no horizon, and the
    # pairing is the point: ``FreshnessService`` distinguishes "no horizon, so nothing is
    # claimed about this kind of fact" from "a horizon exists and the caller did not apply
    # it", and only the first is acceptable. Leaving them out produced identical output
    # today and would have silently swallowed the report the moment someone decided a
    # holiday calendar does expire — which is why the test supplies such a policy.
    typed_refs += [
        ("holiday", ref) for result in holiday_results for ref in result.source_references
    ]
    # Recomputed here rather than trusted from the provider, so the verdict is applied
    # by the current policy in one place instead of by whichever provider happened to
    # stamp the data.
    freshness = freshness_service or FreshnessService()

    # The traveller's own notes are matched against the entities just aggregated, so a
    # pasted 小红书 note lands next to the place it is about instead of in a separate
    # list the reader has to reconcile by hand.
    ingested = list(ingestions or [])
    ingested_notes = _build_ingested_notes(
        ingested, hotels=hotels, restaurants=restaurants, attractions=attractions
    )
    # Their references join the plan's citations. An ingested note is a source like any
    # other, and one that is not cited is one whose origin cannot be checked.
    content_refs = [item.source_reference() for item in ingested]
    refs = [*refs, *content_refs]
    typed_refs += [("user_content", ref) for ref in content_refs]
    # Judged once, after every reference that belongs to this plan has been added —
    # including the traveller's notes. An undated note is reported as age-unknown and a
    # dated-but-old one as too old, without either being treated as current merely because
    # the traveller supplied it. Judging before the notes were added would have needed a
    # second pass, and a second pass is how the earlier verdict gets silently dropped.
    stale_facts = freshness.stale_facts(typed_refs)

    provider_audit = (
        [_audit_entry("search_hotels", result) for result in hotel_results]
        + [_audit_entry("search_room_offers", result) for result in offer_results]
        + [_audit_entry("search_restaurants", result) for result in restaurant_results]
        + [_audit_entry("search_attractions", result) for result in attraction_results]
        + [_audit_entry("calculate_routes", result) for result in route_results]
        + [_audit_entry("search_weather", result) for result in weather_results]
        + [_audit_entry("search_holidays", result) for result in holiday_results]
        + [_audit_entry("enrich_attractions", result) for result in enrichment_results]
    )
    preview = TripPreview(
        profile=profile,
        hotels=hotels,
        hotel_offers=offers,
        hotel_decisions=decisions,
        restaurants=restaurants,
        attractions=attractions,
        route_legs=routes,
        budget=budget,
        itinerary=itinerary,
        weather=weather,
        holidays=holidays,
        provider_health=health,
        provider_audit=provider_audit,
        source_references=refs,
        stale_facts=stale_facts,
        ingested_notes=ingested_notes,
        generated_at=datetime.now(timezone.utc),
    )
    # Closed after the result exists, so a step never reports finished while its own
    # output is still being assembled — a step list that disagrees with reality is the
    # failure this whole mechanism is meant to avoid.
    await reporter.finish_step()
    return preview
