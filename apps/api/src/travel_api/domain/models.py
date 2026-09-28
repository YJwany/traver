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


class TravelMode(StrEnum):
    WALK = "walk"
    TRANSIT = "transit"
    TAXI = "taxi"
    DRIVE = "drive"


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
    observed_at: datetime
    fetched_at: datetime
    expires_at: datetime | None = None
    request_context: dict[str, Any] = Field(default_factory=dict)
    data_version: str = "1"
    confidence: float = Field(default=1.0, ge=0, le=1)
    domain: DataDomain

    @computed_field
    @property
    def is_stale(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(timezone.utc)


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
    model_config = ConfigDict(extra="forbid")

    price: float
    location: float
    transit: float
    rating: float
    itinerary_fit: float
    user_preference: float


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
