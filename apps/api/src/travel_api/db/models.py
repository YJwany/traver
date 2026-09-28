from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TripRecord(Base):
    __tablename__ = "trips"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    destination: Mapped[str] = mapped_column(String(200), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    travelers: Mapped[int] = mapped_column(Integer, nullable=False)
    total_budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    profile_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Owning principal, used to scope every read and write. Nullable so rows
    #: created before authentication existed remain readable by a migration or
    #: an admin path; the repository filters on it, and it is indexed because
    #: every list query is scoped by it.
    owner_id: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TripProfileRecord(Base):
    __tablename__ = "trip_profiles"

    trip_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    profile_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataSourceRecord(Base):
    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    domain: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class SourceReferenceRecord(Base):
    __tablename__ = "source_references"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    data_version: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    domain: Mapped[str] = mapped_column(String(50), nullable=False)


class HotelRecord(Base):
    __tablename__ = "hotels"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    district: Mapped[str | None] = mapped_column(String(100))
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))
    review_count: Mapped[int | None] = mapped_column(Integer)
    brand: Mapped[str | None] = mapped_column(String(120))


class HotelRoomTypeRecord(Base):
    __tablename__ = "hotel_room_types"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    bed_type: Mapped[str | None] = mapped_column(String(80))
    max_guests: Mapped[int] = mapped_column(Integer, nullable=False)
    area_sqm: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))


class HotelRatePlanRecord(Base):
    __tablename__ = "hotel_rate_plans"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    room_type_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    meal_plan: Mapped[str | None] = mapped_column(String(120))
    cancellation_policy: Mapped[str] = mapped_column(Text, nullable=False)
    payment_policy: Mapped[str] = mapped_column(Text, nullable=False)


class HotelOfferRecord(Base):
    __tablename__ = "hotel_offers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    room_type_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rate_plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    check_out: Mapped[date] = mapped_column(Date, nullable=False)
    guests: Mapped[int] = mapped_column(Integer, nullable=False)
    room_count: Mapped[int] = mapped_column(Integer, nullable=False)
    meal_plan: Mapped[str | None] = mapped_column(String(120))
    cancellation_policy: Mapped[str | None] = mapped_column(Text)
    payment_policy: Mapped[str | None] = mapped_column(Text)
    source_reference_id: Mapped[str] = mapped_column(String(160), nullable=False)


class HotelOfferSnapshotRecord(Base):
    __tablename__ = "hotel_offer_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    offer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    tax: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    service_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    availability: Mapped[int | None] = mapped_column(Integer)
    cancellation_policy: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_reference_id: Mapped[str] = mapped_column(String(160), nullable=False)
    raw_reference: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class RestaurantRecord(Base):
    __tablename__ = "restaurants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    district: Mapped[str | None] = mapped_column(String(100))
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))
    review_count: Mapped[int | None] = mapped_column(Integer)
    average_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))


class AttractionRecord(Base):
    __tablename__ = "attractions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    district: Mapped[str | None] = mapped_column(String(100))
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))
    ticket_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    opening_hours_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recommended_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)


class RouteSnapshotRecord(Base):
    __tablename__ = "route_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    origin_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    destination_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    distance_meters: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_reference_id: Mapped[str] = mapped_column(String(160), nullable=False)


class TripItineraryDayRecord(Base):
    __tablename__ = "trip_itinerary_days"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    trip_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    day_date: Mapped[date] = mapped_column(Date, nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    warnings_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class TripItineraryItemRecord(Base):
    __tablename__ = "trip_itinerary_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    day_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    item_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    travel_from_previous_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    visit_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="planned")
    warnings_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class TripBudgetRecord(Base):
    __tablename__ = "trip_budgets"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    trip_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    budget_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    remaining: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    over_budget: Mapped[bool] = mapped_column(nullable=False)
    strategy: Mapped[str] = mapped_column(String(40), nullable=False, default="balanced")
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TripBudgetItemRecord(Base):
    __tablename__ = "trip_budget_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    budget_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    is_estimate: Mapped[bool] = mapped_column(nullable=False)
    source_reference_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class DecisionScoreRecord(Base):
    __tablename__ = "decision_scores"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    trip_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    breakdown_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    matched_constraints_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    failed_constraints_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    explanations_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TripChangeRecord(Base):
    __tablename__ = "trip_changes"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    trip_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    before_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    after_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(String(120), nullable=False)
    impact_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderRequestRecord(Base):
    __tablename__ = "provider_requests"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    request_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    duration_ms: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderResponseRecord(Base):
    __tablename__ = "provider_responses"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TripConversationRecord(Base):
    __tablename__ = "trip_conversations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    trip_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationMessageRecord(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(120))
    tool_arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tool_result_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model: Mapped[str | None] = mapped_column(String(120))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AIToolCallRecord(Base):
    __tablename__ = "ai_tool_calls"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trip_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    tool_call_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    arguments_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    success: Mapped[bool] = mapped_column(nullable=False)
    duration_ms: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AIRunRecord(Base):
    __tablename__ = "ai_runs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    trip_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    request_id: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    duration_ms: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    success: Mapped[bool] = mapped_column(nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApiKeyRecord(Base):
    """A bearer credential for one API consumer.

    Only the SHA-256 hash of the key is stored, so a database leak does not hand
    out working credentials. ``key_prefix`` keeps the first characters in clear
    text purely so a human can tell two keys apart in a listing; it is far too
    short to be usable as a credential.
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    #: SHA-256 hex digest of the full key. Unique so a key maps to one owner.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    #: Display-only fragment, e.g. "trv_1a2b3c4d".
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Stable owner identity used to scope trips; all keys for one user share it.
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    #: Optional scope list ("trip:read", "trip:write"). Empty means both.
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CredentialRecord(Base):
    """An operator-supplied third-party credential, sealed at rest.

    Deliberately a different table from ``api_keys``: those are Traver's own
    bearer keys and are stored as a one-way hash because they are only ever
    verified. These have to be replayed to the upstream service, so they must be
    recoverable, and are therefore encrypted with AES-256-GCM under a master key
    that is not in this database (see ``services/credentials.py``).

    The owner/provider/name triple is bound into the ciphertext as associated
    data, so a row moved to another owner fails to decrypt rather than leaking
    one tenant's key to another.
    """

    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "provider_id",
            "credential_key",
            name="uq_credentials_owner_provider_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    #: Owner the credential belongs to; every read filters on it.
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    #: Manifest id of the source it configures, e.g. "openai-compatible".
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Credential key within that source, e.g. "ai_api_key".
    credential_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: nonce || AES-256-GCM ciphertext+tag. Never logged, never returned by the API.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Which master-key generation sealed this row, so rotation stays possible.
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContentIngestionRecord(Base):
    """Text a traveller supplied from a page they were already reading.

    Owner-scoped like a trip, because a 小红书 note someone saved is theirs and not the
    deployment's. The raw text is kept so the extracted facts stay auditable: without it
    a reader could see "人均 80 元" but not check it, which is the difference between a
    citation and an assertion.

    ``published_at`` is nullable and that null is meaningful — it records that the page
    did not state a date, which is the common case and must not be rendered as "recent".
    """

    __tablename__ = "content_ingestions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    #: The supplied text, stored verbatim so every extracted fact can be checked.
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: When we received it. Always known.
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The post's own date, when the page stated one. ``None`` means "not stated".
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Extracted facts as JSON. A child table would add a join per note for data that is
    #: always read whole and never queried by fact.
    facts_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


def create_engine_from_url(database_url: str):
    kwargs = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)
