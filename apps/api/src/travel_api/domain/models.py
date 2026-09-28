from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field


class SourceType(StrEnum):
    OFFICIAL_API = "official_api"
    PARTNER_API = "partner_api"
    FIXTURE = "fixture"
    USER_PROVIDED = "user_provided"
    SYSTEM_CALCULATED = "system_calculated"
    #: Public, openly licensed community data (OpenStreetMap / Open-Meteo).
    #: Distinguished from partner APIs because there is no commercial contract,
    #: no SLA, and attribution is mandatory.
    OPEN_DATA = "open_data"


class DataDomain(StrEnum):
    COMMERCIAL_FACT = "commercial_fact"
    GEO_FACT = "geo_fact"
    CONTENT_REFERENCE = "content_reference"
    SYSTEM_CALCULATION = "system_calculation"


class Pace(StrEnum):
    RELAXED = "relaxed"
    BALANCED = "balanced"
    FULL = "full"


#: Chinese wording for each pace. The web client has had its own copy since the form was
#: built; the server needs one too as soon as it writes prose of its own (an exported
#: document), because "节奏：balanced" is not a sentence. ``tests/unit/test_web_api_contract.py``
#: compares the two tables, since nothing else can see both languages at once — the same
#: guard that caught the form offering a pace the API rejects.
PACE_LABELS: dict[str, str] = {
    Pace.RELAXED.value: "轻松节奏",
    Pace.BALANCED.value: "适中节奏",
    Pace.FULL.value: "紧凑节奏",
}


class TravelMode(StrEnum):
    WALK = "walk"
    TRANSIT = "transit"
    TAXI = "taxi"
    DRIVE = "drive"


#: Chinese wording for each mode, for prose the server itself writes (a plan's warnings,
#: for example). Kept beside the enum so adding a member without deciding how to name it
#: is a visible omission rather than an English identifier leaking into a Chinese sentence.
TRAVEL_MODE_LABELS: dict[str, str] = {
    TravelMode.WALK.value: "步行",
    TravelMode.TRANSIT.value: "公交 / 地铁",
    TravelMode.TAXI.value: "打车",
    TravelMode.DRIVE.value: "驾车",
}


class PriceType(StrEnum):
    PER_NIGHT = "per_night"
    PER_STAY = "per_stay"
    PER_PERSON = "per_person"
    PER_MEAL = "per_meal"


class BudgetItemStatus(StrEnum):
    PLANNED = "planned"
    ESTIMATED = "estimated"
    OBSERVED = "observed"
    CONFIRMED = "confirmed"


class BudgetStrategyName(StrEnum):
    STRICT = "strict"
    BALANCED = "balanced"
    COMFORT = "comfort"
    TIME_FIRST = "time_first"
    FOOD_FIRST = "food_first"


class ItineraryItemType(StrEnum):
    HOTEL = "hotel"
    ATTRACTION = "attraction"
    RESTAURANT = "restaurant"
    TRANSFER = "transfer"
    BUFFER = "buffer"


#: Chinese wording for each item kind, so a document the server writes (an export) is readable
#: without the application. Beside the enum for the same reason as the two tables above: a
#: new member with no wording here is an omission you can see, and a raw ``transfer`` in a
#: printed itinerary is not.
ITEM_TYPE_LABELS: dict[str, str] = {
    ItineraryItemType.HOTEL.value: "酒店",
    ItineraryItemType.ATTRACTION.value: "景点",
    ItineraryItemType.RESTAURANT.value: "餐饮",
    ItineraryItemType.TRANSFER.value: "交通 / 换乘",
    ItineraryItemType.BUFFER.value: "缓冲 / 休息",
}


class ObservationBasis(StrEnum):
    """What ``SourceReference.observed_at`` actually measures.

    Several very different situations produce the same timestamp, and a reader cannot
    tell them apart from the value alone. Recording which one applies is what stops a
    weaker case from being presented as a stronger one — the whole point of tracking
    freshness separately from the fetch.

    The distinction that matters most for a trip planned in the future: only
    ``PROVIDER_REPORTED`` tells you how old the *data* is. The others tell you only
    that we asked recently.
    """

    #: The source stated when the datum was produced or measured — 高德's
    #: ``reporttime``, OpenStreetMap's ``osm_base`` snapshot time. ``observed_at`` is
    #: that time, so ``data_age_seconds`` is a fact about the data.
    PROVIDER_REPORTED = "provider_reported"
    #: The datum is produced by this request, so it genuinely is as fresh as now — a
    #: route computed on demand against the current road network. Freshness is real,
    #: but it describes the computation, not an underlying dataset.
    LIVE_COMPUTED = "live_computed"
    #: The source returned data and said nothing about its age. This is the case to be
    #: careful with: ``observed_at`` is our fetch time, so the datum may be far older
    #: than it looks, and nothing in the response says by how much.
    SOURCE_SILENT = "source_silent"
    #: Not classified. Deliberately the default and deliberately last: an unclassified
    #: reference must never be treated as provider-reported.
    UNKNOWN = "unknown"

    @property
    def age_is_about_the_data(self) -> bool:
        """Whether ``data_age_seconds`` describes the data rather than our request.

        True only for :attr:`PROVIDER_REPORTED`. Callers that want to warn about
        genuinely old facts should test this, so an unclassified or silent reference
        cannot be counted as evidence of freshness.
        """
        return self is ObservationBasis.PROVIDER_REPORTED


class RevisionAction(StrEnum):
    CHANGE_BUDGET = "change_budget"
    REPLACE_HOTEL = "replace_hotel"
    REPLACE_RESTAURANT = "replace_restaurant"
    REMOVE_ATTRACTION = "remove_attraction"
    ADD_ATTRACTION = "add_attraction"
    CHANGE_PACE = "change_pace"
    REDUCE_WALKING = "reduce_walking"
    REDUCE_TRANSPORT = "reduce_transport"
    INCREASE_FOOD_BUDGET = "increase_food_budget"


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    source_type: SourceType
    source_id: str
    #: When the *datum* is asserted to have been produced or measured. Not the same as
    #: ``fetched_at``: for a cached or slow-moving source it can be much older, and
    #: that gap is the fact a reader needs in order to judge whether it still holds.
    observed_at: datetime
    #: When *we* asked. Always real, and reported separately so a fresh fetch can
    #: never be mistaken for fresh data.
    fetched_at: datetime
    expires_at: datetime | None = None
    #: Which of the two times above ``observed_at`` really is. See
    #: :class:`ObservationBasis`.
    observed_at_basis: ObservationBasis = ObservationBasis.UNKNOWN
    request_context: dict[str, Any] = Field(default_factory=dict)
    data_version: str = "1"
    confidence: float = Field(default=1.0, ge=0, le=1)
    domain: DataDomain

    @computed_field
    @property
    def is_stale(self) -> bool:
        """Whether our copy has passed its validity window (``expires_at``).

        Distinct from the age of the data itself: a just-fetched copy of a
        two-year-old dataset is *not* stale by this measure. Use
        ``data_age_seconds`` and ``observed_at_basis`` to judge the data.
        """
        return self.expires_at is not None and self.expires_at <= datetime.now(timezone.utc)

    @computed_field
    @property
    def data_age_seconds(self) -> int:
        """How old the datum itself claims to be, in seconds.

        Reported as a number rather than a verdict, because whether an age is
        acceptable depends on the kind of fact: a day-old restaurant listing is
        usually fine, a six-hour-old forecast is not. The policy that judges it lives
        in ``FreshnessService``.

        Never negative. A source reporting a time slightly in our future (clock skew,
        or a publication time rounded up) must read as "just now" rather than as a
        negative age, which would make a stale item look fresher than fresh.
        """
        moment = self.observed_at
        if moment.tzinfo is None:
            # Treat a naive timestamp as UTC rather than guessing a zone. Providers
            # are expected to supply an aware value; this is a safety net so one
            # mistake cannot break serialisation of an otherwise valid plan.
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))


class Coordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Decimal = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    price_type: PriceType | None = None
    source_reference_id: str | None = None
    is_estimate: bool = False


class OpeningHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: int = Field(ge=0, le=6)
    opens_at: time
    closes_at: time


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str
    city: str
    coordinates: Coordinates
    district: str | None = None
    source_reference_id: str | None = None


class FieldClaim(BaseModel):
    """One source's assertion about one field of one place.

    Kept per-source rather than collapsed into a single value because the collapse is
    where information is lost. If two sources report different ticket prices, the merge
    has to keep *something* to show, but a reader who sees a number with two sources
    attached will reasonably conclude both agree — so the discarded claim has to be
    recoverable somewhere, and this is that place.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    source_id: str
    #: How the value reads to a person, e.g. ``¥60`` or ``4.5 分``.
    value_text: str
    #: Whether this claim is something we computed rather than something a source stated.
    #: Tracked separately because a derived value is not evidence, so it must never be
    #: preferred over a real report just for being newer.
    is_estimate: bool = False
    observed_at: datetime
    fetched_at: datetime


class FieldDisagreement(BaseModel):
    """Two or more sources asserting different values for the same field.

    Present only when the values actually differ. ``claims`` is every claim seen, not
    just the losing ones, because "three sources, two say ¥60 and one says ¥80" and "two
    sources, ¥60 and ¥80" are different situations and a reader can tell them apart only
    if all of them are listed.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    claims: list[FieldClaim]
    #: The claim whose value the entity kept, so the shown number is attributable.
    chosen_source: str | None = None
    #: How that retained value reads, so a reader can match it to the field on screen
    #: without re-deriving the rendering rule.
    chosen_value_text: str = ""
    #: Why that one, in words. An unexplained tie-break is indistinguishable from a
    #: silent arbitrary pick.
    chosen_reason: str = ""

    @property
    def disputed_values(self) -> list[str]:
        """The distinct values, first-seen order."""
        seen: list[str] = []
        for claim in self.claims:
            if claim.value_text not in seen:
                seen.append(claim.value_text)
        return seen


class Hotel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    location: Location
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    brand: str | None = None
    source_references: list[SourceReference] = Field(default_factory=list)
    #: Fields where the merged sources disagreed. Empty means every source that spoke
    #: agreed — or only one spoke — so an empty list must not be read as "verified".
    disagreements: list[FieldDisagreement] = Field(default_factory=list)


class HotelRoomType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    hotel_id: UUID
    name: str
    bed_type: str | None = None
    max_guests: int = Field(default=2, ge=1)
    area_sqm: float | None = Field(default=None, ge=0)


class HotelRatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    room_type_id: UUID
    name: str
    meal_plan: str | None = None
    cancellation_policy: str
    payment_policy: str


class HotelOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    hotel_id: UUID
    room_type_id: UUID
    rate_plan_id: UUID
    check_in: date
    check_out: date
    guests: int = Field(ge=1)
    room_count: int = Field(default=1, ge=1)
    meal_plan: str | None = None
    cancellation_policy: str | None = None
    payment_policy: str | None = None
    price: Money
    tax: Money | None = None
    service_fee: Money | None = None
    availability: int | None = Field(default=None, ge=0)
    source_reference: SourceReference

    @computed_field
    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @computed_field
    @property
    def total_price(self) -> Decimal:
        return (
            self.price.value
            + (self.tax.value if self.tax else Decimal("0"))
            + (self.service_fee.value if self.service_fee else Decimal("0"))
        )


class Restaurant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    location: Location
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    average_cost: Money | None = None
    cuisine: list[str] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)
    #: Fields where the merged sources disagreed; see :class:`FieldDisagreement`.
    disagreements: list[FieldDisagreement] = Field(default_factory=list)


class Attraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    location: Location
    rating: float | None = Field(default=None, ge=0, le=5)
    ticket_price: Money | None = None
    recommended_duration_minutes: int = Field(default=120, ge=15)
    opening_hours: list[OpeningHours] = Field(default_factory=list)
    #: Encyclopedic text, only ever set from an explicit OSM `wikipedia` /
    #: `wikidata` link (CC BY-SA 4.0). Never guessed from the name: a name match
    #: resolved 火烈鸟 to the taxonomic family 紅鸛科 and 人民公园 to a
    #: disambiguation page, either of which would misattribute content.
    description: str | None = None
    wikipedia_url: str | None = None
    source_references: list[SourceReference] = Field(default_factory=list)
    #: Fields where the merged sources disagreed; see :class:`FieldDisagreement`.
    disagreements: list[FieldDisagreement] = Field(default_factory=list)


class RoutePoint(BaseModel):
    """A routable place: an entity id plus the coordinates to route between.

    Providers receive the coordinates directly because a routing service has no
    way to look up an internal entity id.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @property
    def coordinates(self) -> Coordinates:
        return Coordinates(latitude=self.latitude, longitude=self.longitude)


class PlaceResolutionStatus(StrEnum):
    """What a source did with a name it was asked to locate.

    Four outcomes, and the reason they are four rather than a boolean is that the reader's
    next move differs for each. Collapsing them is how "we did not look" becomes
    indistinguishable from "we looked and found nothing", which reads as "this place does
    not exist" — a claim nobody made.

    This is the same distinction ``_disclose_unserved_travel_mode`` draws between a mode no
    source can serve and a source that answered and refused.
    """

    #: The source returned a coordinate, and it is plausibly the place the text meant.
    LOCATED = "located"
    #: The source returned a coordinate that is not near the destination the text is about.
    #: 西湖 exists in several cities; the note is about one of them.
    ELSEWHERE = "elsewhere"
    #: The source was asked and had nothing. A real answer, and a different one from
    #: :attr:`NOT_ATTEMPTED`.
    NOT_FOUND = "not_found"
    #: No configured source serves place resolution at all, so nothing was asked. Reported
    #: apart from ``NOT_FOUND`` because telling someone「没有找到这个地方」when in fact
    #: nobody looked is a fabrication, and it is the fabrication a partial deployment
    #: produces most often.
    NOT_ATTEMPTED = "not_attempted"


#: Chinese wording for each outcome, because the export prints it into a document that has to
#: explain itself without the application around it. Beside the enum for the same reason as
#: ``ITEM_TYPE_LABELS``: a fifth status with no wording here is a visible omission, whereas a
#: bare ``not_attempted`` in a printed itinerary is an English identifier in Chinese prose.
#:
#: ``not_found`` and ``not_attempted`` are worded so they cannot be confused. The first says a
#: source was asked; the second says the lookup did not happen and why. Collapsing them is the
#: fabrication this status exists to prevent — 「没有找到这个地方」 is a claim about the world,
#: and a deployment with no geocoder has no basis for it.
PLACE_STATUS_LABELS: dict[str, str] = {
    PlaceResolutionStatus.LOCATED.value: "已定位（在目的地范围内）",
    PlaceResolutionStatus.ELSEWHERE.value: "在别处（同名地点不在目的地附近）",
    PlaceResolutionStatus.NOT_FOUND.value: "已查询地名来源，没有该名称的结果",
    PlaceResolutionStatus.NOT_ATTEMPTED.value: "没有查询（本次规划没有可用的地名定位来源）",
}


class ResolvedPlace(BaseModel):
    """A name from the traveller's own material, put on the map — or not, and why.

    This is deliberately **not** an :class:`Attraction`. An attraction is a POI discovered by
    a data source, carrying that source's rating, ticket price and opening hours. This is a
    place *a note mentioned*, and the differences are the ones that matter:

    * it has no rating, no price and no opening hours, because nothing measured them;
    * its coordinate comes from a geocoder reading a name out of prose, so the coordinate is
      a reading, not a survey — which is why the geocoder's own confidence is carried;
    * it exists because somebody wrote about it, so its provenance is the note, not a dataset.

    Keeping it a separate type is the structural part of 「必须与实测 POI 明确区分开来」: no
    code that consumes ``Attraction`` can accidentally receive one of these, because it is
    not one. A boolean flag on ``Attraction`` would have been a comment, not a constraint.
    """

    model_config = ConfigDict(extra="forbid")

    #: The name as queried, which is the extractor's reading of the prose.
    name: str
    status: PlaceResolutionStatus
    #: Set only when :attr:`status` is ``LOCATED``. Absent otherwise, never defaulted to a
    #: plausible-looking point: a wrong coordinate here becomes a stop in somebody's day.
    coordinates: Coordinates | None = None
    #: What the source calls the place, when it answered. Shown so a reader can see the
    #: match was 「春熙路」 → 「春熙路步行街」 rather than taking it on trust.
    display_name: str | None = None
    #: The source's own confidence in the match, when it states one. ``None`` means the
    #: source did not say, which is not the same as being sure.
    match_confidence: float | None = Field(default=None, ge=0, le=1)
    #: Straight-line distance from the destination centre, in kilometres. Named for what it
    #: is: this is great-circle distance, not a road distance, and no travel time is derived
    #: from it here.
    straight_line_from_centre_km: float | None = Field(default=None, ge=0)
    #: Why this status, in the source's or the resolver's words. Never empty for a
    #: non-``LOCATED`` outcome, because "elsewhere" with no explanation is a puzzle.
    reason: str = ""
    #: The geocoder's citation. Present whenever a source was actually asked, including for
    #: ``ELSEWHERE`` and ``NOT_FOUND`` — the lookup is a source like any other, and its
    #: result is only checkable if it is cited. Absent for ``NOT_ATTEMPTED``, where there is
    #: nothing to cite.
    source_reference: SourceReference | None = None


class RouteLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin_id: UUID
    destination_id: UUID
    mode: TravelMode
    distance_meters: int = Field(ge=0)
    duration_minutes: int = Field(ge=0)
    cost: Money | None = None
    #: True when distance/duration were derived rather than observed. The public
    #: OSRM demo only routes cars, so a walking leg's travel time is a stated
    #: estimate and must never be presented as a measured fact.
    is_estimate: bool = False
    source_reference: SourceReference


class TripProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(min_length=1)
    start_date: date
    end_date: date
    travelers: int = Field(default=1, ge=1)
    total_budget: Decimal | None = Field(default=None, ge=0)
    hotel_budget: Decimal | None = Field(default=None, ge=0)
    food_budget: Decimal | None = Field(default=None, ge=0)
    transport_budget: Decimal | None = Field(default=None, ge=0)
    attraction_budget: Decimal | None = Field(default=None, ge=0)
    preferences: list[str] = Field(default_factory=list)
    pace: Pace = Pace.BALANCED
    #: How the traveller wants to move between places once they are there.
    #:
    #: ``None`` means "derive it from the pace", which is what every trip did before this
    #: field existed — so leaving it unset changes nothing. It is a separate field rather
    #: than a fourth ``Pace`` because the two answer different questions: pace says how
    #: much to pack into a day, this says how to get between the things packed in.
    #:
    #: Without it, 公交/地铁 was unimplementable-in-practice: ``TravelMode.TRANSIT`` was a
    #: real enum member with a complete provider implementation behind it and no way for
    #: any request to select it, so the capability the Amap integration exists to add
    #: could never appear in an itinerary.
    travel_mode: TravelMode | None = None
    hotel_preferences: list[str] = Field(default_factory=list)
    food_preferences: list[str] = Field(default_factory=list)
    attraction_preferences: list[str] = Field(default_factory=list)
    mobility_constraints: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)
    special_requirements: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def nights(self) -> int:
        return (self.end_date - self.start_date).days

    def model_post_init(self, __context: Any) -> None:
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")


class ScoreBreakdown(BaseModel):
    """One decision's dimensions, each either measured or explicitly absent.

    Every dimension that can be missing is optional, and ``None`` means **not measured**. This
    is deliberately not spelled ``0.0``: a zero reads as "worst possible", which is a
    measurement, and neither a reader nor a later scorer can tell the two apart from the value.

    The rule that keeps this honest: **a dimension the scorer dropped from its weighted sum
    must be ``None`` here, and a dimension it included must be present.** The scorer normalises
    its total over exactly the dimensions it could compute, so a breakdown claiming a value for
    a dimension that was not in the sum would describe a decision that was not made. There is a
    test for that correspondence rather than a comment, because the two lists live in different
    places.

    All six are optional because all six can in principle be unmeasurable: the price needs an
    offer, the rating needs the source to have published one, and the other four need the plan
    to have located something.
    """

    model_config = ConfigDict(extra="forbid")

    price: float | None = None
    #: Whether the hotel is in the district the plan mostly sits in. ``None`` when either side
    #: has no district, which is not the same as "a different district".
    location: float | None = None
    #: Distance to the plan's own centre. ``None`` when the plan located no points.
    transit: float | None = None
    #: The source's own rating, normalised. ``None`` when no source published one — OSM's
    #: ``stars`` is a classification, not a guest rating, and is deliberately not used here.
    rating: float | None = None
    #: Whether the hotel is in the city the trip is about. ``None`` when the hotel's city is
    #: absent, which is missing data rather than evidence of another city.
    itinerary_fit: float | None = None
    user_preference: float | None = None


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    score: float
    score_breakdown: ScoreBreakdown
    matched_constraints: list[str] = Field(default_factory=list)
    failed_constraints: list[str] = Field(default_factory=list)
    explanations: list[str] = Field(default_factory=list)


class BudgetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    amount: Decimal
    currency: str = "CNY"
    status: BudgetItemStatus = BudgetItemStatus.ESTIMATED
    source_reference_ids: list[str] = Field(default_factory=list)
    is_estimate: bool = False


class BudgetBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BudgetItem]
    total: Decimal
    budget_limit: Decimal | None
    remaining: Decimal | None
    over_budget: bool
    strategy: BudgetStrategyName = BudgetStrategyName.BALANCED


class ItineraryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=1)
    item_type: ItineraryItemType
    entity_id: UUID | None = None
    title: str
    start_time: datetime
    end_time: datetime
    travel_from_previous_minutes: int = Field(default=0, ge=0)
    visit_duration_minutes: int = Field(default=0, ge=0)
    status: str = "planned"
    warnings: list[str] = Field(default_factory=list)


class ItineraryDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    day_date: date
    day_index: int = Field(ge=1)
    items: list[ItineraryItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Itinerary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: list[ItineraryDay] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class TripChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    version: int = Field(ge=1)
    action: RevisionAction
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    reason: str = "user_requested"
    impact: dict[str, Any] = Field(default_factory=dict)
    changed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TripRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: RevisionAction
    constraints: dict[str, Any] = Field(default_factory=dict)


class TripPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    travelers: int | None = Field(default=None, ge=1)
    total_budget: Decimal | None = Field(default=None, ge=0)
    hotel_budget: Decimal | None = Field(default=None, ge=0)
    food_budget: Decimal | None = Field(default=None, ge=0)
    transport_budget: Decimal | None = Field(default=None, ge=0)
    attraction_budget: Decimal | None = Field(default=None, ge=0)
    preferences: list[str] | None = None
    pace: Pace | None = None
    #: ``None`` means "leave unchanged", so this cannot be used to clear an explicit mode
    #: back to pace-derived — the same limitation ``pace`` already has. Clearing would
    #: need a sentinel distinct from both ``None`` and a member, and inventing one for
    #: this field alone would make the patch semantics inconsistent.
    travel_mode: TravelMode | None = None
    hotel_preferences: list[str] | None = None
    food_preferences: list[str] | None = None
    attraction_preferences: list[str] | None = None
    mobility_constraints: list[str] | None = None
    must_visit: list[str] | None = None
    must_avoid: list[str] | None = None
    special_requirements: list[str] | None = None
    budget_strategy: BudgetStrategyName | None = None
    #: Optional optimistic-concurrency guard. When supplied, the patch is
    #: applied only if the stored trip is still at this version; otherwise the
    #: request is rejected with 409 instead of silently overwriting a concurrent
    #: change. Omit it to keep the previous last-write-wins behaviour.
    expected_version: int | None = Field(default=None, ge=1)


class ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    status: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latency_ms: float | None = None
    detail: str | None = None
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None


class ProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "success"
    provider: str
    source_references: list[SourceReference] = Field(default_factory=list)
    data: list[Any] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class WeatherDay(BaseModel):
    """One forecast day. Always carries the SourceReference it came from.

    Air-quality fields are separate from the forecast fields because they come
    from a different Open-Meteo endpoint and are aggregated from hourly values
    (the air-quality API has no daily aggregate). They stay ``None`` when that
    endpoint is unavailable, rather than defaulting to a "good" value that would
    silently claim clean air.
    """

    model_config = ConfigDict(extra="forbid")

    day_date: date
    weather_code: int
    condition: str
    temperature_max_c: float
    temperature_min_c: float
    precipitation_mm: float = Field(default=0.0, ge=0)
    precipitation_probability: int | None = Field(default=None, ge=0, le=100)
    #: European AQI (0-20 good ... 100+ very poor). ``None`` when unknown.
    air_quality_index: int | None = Field(default=None, ge=0)
    pm2_5_ug_m3: float | None = Field(default=None, ge=0)
    #: Peak UV index for the day.
    uv_index_max: float | None = Field(default=None, ge=0)
    outdoor_friendly: bool = True


class WeatherForecast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_name: str
    coordinates: Coordinates
    timezone: str | None = None
    #: Terrain elevation reported by the forecast endpoint itself. Present for
    #: free, so no separate elevation request is needed.
    elevation_m: float | None = None
    days: list[WeatherDay] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)


class PublicHoliday(BaseModel):
    """A public holiday that can change whether a plan is workable.

    Kept as plain dated facts (name plus date) rather than a derived "crowd
    score": the source states when a holiday falls, not how busy a given museum
    will be, and inventing the latter would be a fabricated fact.
    """

    model_config = ConfigDict(extra="forbid")

    holiday_date: date
    name: str
    local_name: str | None = None
    country_code: str | None = None
    #: True when the holiday applies nationwide rather than to some regions.
    nationwide: bool = True
    #: Holiday category as reported by the source (``public``, ``bank``, ...).
    #: Kept so a consumer can tell a public holiday from an observance.
    category: str = "public"
    source_reference: SourceReference | None = None


class HolidayCalendar(BaseModel):
    """The holidays relevant to one trip.

    Holds every year the trip touches, because a trip that crosses New Year
    (e.g. 12-30 to 01-02) needs both years' calendars; a single ``year`` field
    would silently drop one of them.
    """

    model_config = ConfigDict(extra="forbid")

    country_code: str
    years: list[int] = Field(default_factory=list)
    #: Only the holidays inside the trip window are included: those are the ones
    #: that can change a plan.
    holidays: list[PublicHoliday] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)

    def on(self, day: date) -> PublicHoliday | None:
        """Return the holiday falling on ``day``, if any."""
        for holiday in self.holidays:
            if holiday.holiday_date == day:
                return holiday
        return None

    def between(self, start: date, end: date) -> list[PublicHoliday]:
        """Holidays in the inclusive range [start, end]."""
        return [h for h in self.holidays if start <= h.holiday_date <= end]

    @property
    def holiday_dates(self) -> set[date]:
        return {h.holiday_date for h in self.holidays}


class TripPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: TripProfile
    hotels: list[Hotel]
    hotel_offers: list[HotelOffer]
    hotel_decisions: list[CandidateDecision]
    restaurants: list[Restaurant]
    attractions: list[Attraction]
    route_legs: list[RouteLeg]
    budget: BudgetBreakdown
    itinerary: Itinerary = Field(default_factory=Itinerary)
    weather: WeatherForecast | None = None
    #: Public holidays overlapping the trip. A hard input, not decoration: a
    #: holiday can close a museum or fill a site, so the plan must know about it.
    holidays: HolidayCalendar | None = None
    provider_health: list[ProviderHealth]
    provider_audit: list[dict[str, Any]] = Field(default_factory=list)
    source_references: list[SourceReference]
    #: Facts whose *own* age exceeds what their kind allows, so they must not be
    #: presented as current. A trip is planned for the future, so a stale fact is
    #: worse than a missing one: it looks like knowledge.
    #:
    #: Empty does **not** mean every fact was verified recent — it means none was
    #: provably too old. Those are different claims, and conflating them was a real
    #: defect: the free stack publishes an observation time for its POI data but not
    #: for weather, Wikipedia or the holiday calendar, so a typical plan has many
    #: unverifiable facts and an empty ``stale_facts``. Read the two lists together.
    stale_facts: list[dict[str, Any]] = Field(default_factory=list)
    #: Facts whose age cannot be established at all, because the source does not
    #: publish when its data was produced.
    #:
    #: Reported separately from ``stale_facts`` rather than merged into it, because
    #: the remedies differ: a stale fact is re-fetched or discarded, an unverifiable
    #: one is disclosed. Emitting these as staleness would cry wolf on every plan, and
    #: omitting them entirely would leave an empty banner implying a verification that
    #: never happened. The count is the honest middle: we know when we asked, we do
    #: not know how old the answer was.
    age_unknown_facts: list[dict[str, Any]] = Field(default_factory=list)
    #: Advice the traveller supplied themselves — a pasted 小红书 note, a 大众点评 review —
    #: matched against the entities in this plan.
    #:
    #: Advisory only, and the shape says so: each entry records which entity it matched,
    #: the sentence it came from, and where it was published. Ingested text is somebody's
    #: account of a trip, not a measurement, so it is never merged into an entity's own
    #: fields — a remembered price must not stand in for a quoted rate, and a blogger's
    #: "2 hours is enough" must not overwrite a measured visit duration.
    ingested_notes: list[dict[str, Any]] = Field(default_factory=list)
    #: Places the traveller's own material named, located on the map when a source could
    #: resolve the name, with the outcome stated for the rest.
    #:
    #: Reported **apart from** ``attractions`` rather than merged into them, and the reason is
    #: the product's honesty rule rather than tidiness. An attraction on this plan was found
    #: by a data source and carries that source's measured fields; one of these was named in
    #: somebody's post and then geocoded from a name, which is a reading. Merging them would
    #: make 「笔记里提到的店」 indistinguishable from 「查到的景点」 in every consumer at
    #: once — including the itinerary, which would start scheduling recommendations the
    #: traveller never chose.
    #:
    #: These are located, not scheduled. A name in a note is evidence that somebody liked a
    #: place; it is not a decision by this traveller to go there, and the plan does not make
    #: that decision on their behalf. The list exists so the gap between "mentioned" and "in
    #: your plan" is visible and actionable rather than silently closed.
    ingested_places: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Trip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    profile: TripProfile
    preview: TripPreview | None = None
    selected_hotel_offer_id: UUID | None = None
    selected_restaurant_ids: list[UUID] = Field(default_factory=list)
    selected_attraction_ids: list[UUID] = Field(default_factory=list)
    itinerary: Itinerary = Field(default_factory=Itinerary)
    budget: BudgetBreakdown | None = None
    current_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    change_history: list[TripChange] = Field(default_factory=list)


class TripRevisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip: Trip
    change: TripChange


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class ConversationMessage(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    role: ConversationRole
    content: str
    tool_name: str | None = None
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    tool_result_summary: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AIChatRequest(BaseModel):
    trip_id: UUID | None = None
    message: str = Field(min_length=1, max_length=4000)


class AIChatResult(BaseModel):
    message: str
    intent: str
    trip_id: UUID | None = None
    trip_updated: bool = False
    version: int | None = None
    selected_hotel_id: UUID | None = None
    selected_restaurants: list[UUID] = Field(default_factory=list)
    selected_attractions: list[UUID] = Field(default_factory=list)
    budget_summary: dict[str, Any] = Field(default_factory=dict)
    itinerary_summary: list[dict[str, Any]] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    change: TripChange | None = None
    unresolved_questions: list[str] = Field(default_factory=list)


class CredentialWriteRequest(BaseModel):
    """A credential value to store.

    Write-only on purpose: there is no response model carrying a value back, so a
    stored secret cannot be read out of the API even by its owner. Showing it again
    would add an exfiltration path for no benefit — the value can be replaced, and
    a "test connection" answers whether it works.
    """

    value: str = Field(min_length=1, max_length=4096)


class ModelTestResult(BaseModel):
    """Outcome of really calling the configured model once."""

    ok: bool
    model: str = ""
    latency_ms: float | None = None
    reply: str | None = None
    error: str | None = None
