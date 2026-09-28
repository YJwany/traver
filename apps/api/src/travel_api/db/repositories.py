from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from travel_api.db.models import (
    AIRunRecord,
    AIToolCallRecord,
    ApiKeyRecord,
    AttractionRecord,
    ConversationMessageRecord,
    CredentialRecord,
    DecisionScoreRecord,
    HotelOfferRecord,
    HotelOfferSnapshotRecord,
    HotelRecord,
    ProviderRequestRecord,
    ProviderResponseRecord,
    RestaurantRecord,
    RouteSnapshotRecord,
    SourceReferenceRecord,
    TripBudgetItemRecord,
    TripBudgetRecord,
    TripChangeRecord,
    TripConversationRecord,
    TripItineraryDayRecord,
    TripItineraryItemRecord,
    TripProfileRecord,
    TripRecord,
)
from travel_api.domain.models import (
    Attraction,
    BudgetBreakdown,
    ConversationMessage,
    Hotel,
    HotelOffer,
    Itinerary,
    Restaurant,
    RouteLeg,
    SourceReference,
    Trip,
    TripChange,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def source_key(reference: SourceReference) -> str:
    return f"{reference.source}:{reference.source_id}"


def _persisted_model_dump(model: Any) -> dict[str, Any]:
    """Serialize domain state without Pydantic computed presentation fields.

    Computed fields such as ``HotelOffer.total_price`` are derived values, not
    persisted facts. Keeping them out of the durable aggregate state avoids
    replay failures when the domain model validates the state again.
    """

    return model.model_dump(mode="json", exclude_computed_fields=True)


class TripRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, trip: Trip, *, owner_id: str | None = None) -> TripRecord:
        record = TripRecord(
            id=trip.id,
            destination=trip.profile.destination,
            start_date=trip.profile.start_date,
            end_date=trip.profile.end_date,
            travelers=trip.profile.travelers,
            total_budget=trip.profile.total_budget,
            profile_json=_persisted_model_dump(trip.profile),
            state_json=_persisted_model_dump(trip),
            current_version=trip.current_version,
            owner_id=owner_id,
            created_at=trip.created_at,
            updated_at=trip.updated_at,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, trip_id: UUID, *, owner_id: str | None = None) -> Trip | None:
        record = self.session.get(TripRecord, trip_id)
        if record is None:
            return None
        # Scoping is applied here rather than in the route so that every caller
        # — routes, tools and the AI path — is covered by construction. A
        # mismatched owner is reported as "not found" on purpose: revealing that
        # the id exists would let a caller enumerate other users' trips.
        if owner_id is not None and record.owner_id != owner_id:
            return None
        return Trip.model_validate(record.state_json)

    def owner_of(self, trip_id: UUID) -> str | None:
        """Return the stored owner, or ``None`` when the trip does not exist."""
        record = self.session.get(TripRecord, trip_id)
        return None if record is None else record.owner_id

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        owner_id: str | None = None,
    ) -> list[Trip]:
        statement = select(TripRecord).order_by(TripRecord.updated_at.desc())
        if owner_id is not None:
            statement = statement.where(TripRecord.owner_id == owner_id)
        records = self.session.scalars(statement.limit(limit).offset(offset)).all()
        return [Trip.model_validate(record.state_json) for record in records]

    def count(self, *, owner_id: str | None = None) -> int:
        statement = select(func.count()).select_from(TripRecord)
        if owner_id is not None:
            statement = statement.where(TripRecord.owner_id == owner_id)
        return int(self.session.scalar(statement) or 0)

    def set_owner(self, trip_id: UUID, owner_id: str) -> None:
        record = self.session.get(TripRecord, trip_id)
        if record is not None:
            record.owner_id = owner_id
            self.session.flush()

    def update(self, trip: Trip, *, expected_version: int | None = None) -> TripRecord | None:
        """Persist ``trip``.

        When ``expected_version`` is given this is a compare-and-swap: the write
        only lands if the stored row is still at that version, and ``None`` is
        returned when another writer got there first. Without it the update is an
        unconditional overwrite, which is what caused silent lost updates when
        two callers patched the same trip concurrently.
        """
        record = self.session.get(TripRecord, trip.id)
        if record is None:
            return self.create(trip)
        if expected_version is not None and record.current_version != expected_version:
            return None
        record.destination = trip.profile.destination
        record.start_date = trip.profile.start_date
        record.end_date = trip.profile.end_date
        record.travelers = trip.profile.travelers
        record.total_budget = trip.profile.total_budget
        record.profile_json = _persisted_model_dump(trip.profile)
        record.state_json = _persisted_model_dump(trip)
        record.current_version = trip.current_version
        record.updated_at = trip.updated_at
        self.session.flush()
        return record


class TripProfileRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, trip: Trip) -> TripProfileRecord:
        record = self.session.get(TripProfileRecord, trip.id)
        if record is None:
            record = TripProfileRecord(trip_id=trip.id)
            self.session.add(record)
        record.profile_json = _persisted_model_dump(trip.profile)
        record.updated_at = trip.updated_at
        self.session.flush()
        return record


class SourceReferenceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, references: list[SourceReference]) -> None:
        unique_references = {source_key(reference): reference for reference in references}
        for reference in unique_references.values():
            key = source_key(reference)
            record = self.session.get(SourceReferenceRecord, key)
            values = {
                "source": reference.source,
                "source_type": reference.source_type.value,
                "source_id": reference.source_id,
                "observed_at": reference.observed_at,
                "fetched_at": reference.fetched_at,
                "expires_at": reference.expires_at,
                "request_context": reference.request_context,
                "data_version": reference.data_version,
                "confidence": Decimal(str(reference.confidence)),
                "domain": reference.domain.value,
            }
            if record is None:
                self.session.add(SourceReferenceRecord(id=key, **values))
            else:
                for field, value in values.items():
                    setattr(record, field, value)


class HotelRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, hotels: list[Hotel]) -> None:
        for hotel in hotels:
            record = self.session.get(HotelRecord, hotel.id)
            values = {
                "canonical_name": hotel.canonical_name,
                "city": hotel.location.city,
                "address": hotel.location.address,
                "district": hotel.location.district,
                "latitude": Decimal(str(hotel.location.coordinates.latitude)),
                "longitude": Decimal(str(hotel.location.coordinates.longitude)),
                "rating": hotel.rating,
                "review_count": hotel.review_count,
                "brand": hotel.brand,
            }
            if record is None:
                self.session.add(HotelRecord(id=hotel.id, **values))
            else:
                for field, value in values.items():
                    setattr(record, field, value)


class HotelOfferRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, offers: list[HotelOffer]) -> None:
        for offer in offers:
            record = self.session.get(HotelOfferRecord, offer.id)
            values = {
                "hotel_id": offer.hotel_id,
                "room_type_id": offer.room_type_id,
                "rate_plan_id": offer.rate_plan_id,
                "check_in": offer.check_in,
                "check_out": offer.check_out,
                "guests": offer.guests,
                "room_count": offer.room_count,
                "meal_plan": offer.meal_plan,
                "cancellation_policy": offer.cancellation_policy,
                "payment_policy": offer.payment_policy,
                "source_reference_id": source_key(offer.source_reference),
            }
            if record is None:
                self.session.add(HotelOfferRecord(id=offer.id, **values))
            else:
                for field, value in values.items():
                    setattr(record, field, value)


class RestaurantRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, restaurants: list[Restaurant]) -> None:
        for restaurant in restaurants:
            record = self.session.get(RestaurantRecord, restaurant.id)
            values = {
                "canonical_name": restaurant.canonical_name,
                "city": restaurant.location.city,
                "address": restaurant.location.address,
                "district": restaurant.location.district,
                "latitude": Decimal(str(restaurant.location.coordinates.latitude)),
                "longitude": Decimal(str(restaurant.location.coordinates.longitude)),
                "rating": restaurant.rating,
                "review_count": restaurant.review_count,
                "average_cost": restaurant.average_cost.value if restaurant.average_cost else None,
                "currency": restaurant.average_cost.currency if restaurant.average_cost else None,
            }
            if record is None:
                self.session.add(RestaurantRecord(id=restaurant.id, **values))
            else:
                for field, value in values.items():
                    setattr(record, field, value)


class AttractionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, attractions: list[Attraction]) -> None:
        for attraction in attractions:
            record = self.session.get(AttractionRecord, attraction.id)
            values = {
                "canonical_name": attraction.canonical_name,
                "city": attraction.location.city,
                "address": attraction.location.address,
                "district": attraction.location.district,
                "latitude": Decimal(str(attraction.location.coordinates.latitude)),
                "longitude": Decimal(str(attraction.location.coordinates.longitude)),
                "rating": attraction.rating,
                "ticket_price": attraction.ticket_price.value if attraction.ticket_price else None,
                "currency": attraction.ticket_price.currency if attraction.ticket_price else None,
                "opening_hours_json": attraction.model_dump(mode="json")["opening_hours"],
                "recommended_duration_minutes": attraction.recommended_duration_minutes,
            }
            if record is None:
                self.session.add(AttractionRecord(id=attraction.id, **values))
            else:
                for field, value in values.items():
                    setattr(record, field, value)


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append_offer_snapshots(self, offers: list[HotelOffer]) -> None:
        for offer in offers:
            snapshot = HotelOfferSnapshotRecord(
                offer_id=offer.id,
                price=offer.price.value,
                currency=offer.price.currency,
                tax=offer.tax.value if offer.tax else None,
                service_fee=offer.service_fee.value if offer.service_fee else None,
                availability=offer.availability,
                cancellation_policy=offer.cancellation_policy,
                observed_at=offer.source_reference.observed_at,
                fetched_at=offer.source_reference.fetched_at,
                expires_at=offer.source_reference.expires_at,
                source_reference_id=source_key(offer.source_reference),
                raw_reference={"offer_id": str(offer.id)},
            )
            self.session.add(snapshot)

    def list_offer_snapshots(self, offer_ids: list[UUID]) -> list[HotelOfferSnapshotRecord]:
        if not offer_ids:
            return []
        return list(
            self.session.scalars(
                select(HotelOfferSnapshotRecord)
                .where(HotelOfferSnapshotRecord.offer_id.in_(offer_ids))
                .order_by(HotelOfferSnapshotRecord.observed_at.asc())
            )
        )


class BudgetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace(
        self, trip_id: UUID, budget: BudgetBreakdown, calculated_at: datetime | None = None
    ) -> None:
        self.session.execute(delete(TripBudgetRecord).where(TripBudgetRecord.trip_id == trip_id))
        record = TripBudgetRecord(
            id=uuid4(),
            trip_id=trip_id,
            total=budget.total,
            budget_limit=budget.budget_limit,
            remaining=budget.remaining,
            over_budget=budget.over_budget,
            strategy=budget.strategy.value,
            calculated_at=calculated_at or utc_now(),
        )
        self.session.add(record)
        self.session.flush()
        for item in budget.items:
            self.session.add(
                TripBudgetItemRecord(
                    id=uuid4(),
                    budget_id=record.id,
                    category=item.category,
                    amount=item.amount,
                    currency=item.currency,
                    status=item.status.value,
                    is_estimate=item.is_estimate,
                    source_reference_ids=item.source_reference_ids,
                )
            )


class RouteRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append_snapshots(self, routes: list[RouteLeg]) -> None:
        for route in routes:
            self.session.add(
                RouteSnapshotRecord(
                    id=uuid4(),
                    origin_id=route.origin_id,
                    destination_id=route.destination_id,
                    mode=route.mode.value,
                    distance_meters=route.distance_meters,
                    duration_minutes=route.duration_minutes,
                    cost=route.cost.value if route.cost else None,
                    currency=route.cost.currency if route.cost else None,
                    observed_at=route.source_reference.observed_at,
                    fetched_at=route.source_reference.fetched_at,
                    expires_at=route.source_reference.expires_at,
                    source_reference_id=source_key(route.source_reference),
                )
            )


class DecisionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace(
        self, trip_id: UUID, decisions: list[Any], calculated_at: datetime | None = None
    ) -> None:
        self.session.execute(
            delete(DecisionScoreRecord).where(DecisionScoreRecord.trip_id == trip_id)
        )
        for decision in decisions:
            self.session.add(
                DecisionScoreRecord(
                    id=uuid4(),
                    trip_id=trip_id,
                    candidate_id=decision.candidate_id,
                    score=Decimal(str(decision.score)),
                    breakdown_json=decision.score_breakdown.model_dump(mode="json"),
                    matched_constraints_json=decision.matched_constraints,
                    failed_constraints_json=decision.failed_constraints,
                    explanations_json=decision.explanations,
                    calculated_at=calculated_at or utc_now(),
                )
            )


class ItineraryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace(self, trip_id: UUID, itinerary: Itinerary) -> None:
        day_ids = select(TripItineraryDayRecord.id).where(TripItineraryDayRecord.trip_id == trip_id)
        self.session.execute(
            delete(TripItineraryItemRecord).where(TripItineraryItemRecord.day_id.in_(day_ids))
        )
        self.session.execute(
            delete(TripItineraryDayRecord).where(TripItineraryDayRecord.trip_id == trip_id)
        )
        for day in itinerary.days:
            day_record = TripItineraryDayRecord(
                id=day.id,
                trip_id=trip_id,
                day_date=day.day_date,
                day_index=day.day_index,
                warnings_json=day.warnings,
            )
            self.session.add(day_record)
            self.session.flush()
            for item in day.items:
                self.session.add(
                    TripItineraryItemRecord(
                        id=item.id,
                        day_id=day.id,
                        sequence=item.sequence,
                        item_type=item.item_type.value,
                        entity_id=item.entity_id,
                        title=item.title,
                        start_time=item.start_time,
                        end_time=item.end_time,
                        travel_from_previous_minutes=item.travel_from_previous_minutes,
                        visit_duration_minutes=item.visit_duration_minutes,
                        status=item.status,
                        warnings_json=item.warnings,
                    )
                )


class TripChangeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(self, trip_id: UUID, change: TripChange) -> None:
        self.session.add(
            TripChangeRecord(
                id=change.id,
                trip_id=trip_id,
                version=change.version,
                action=change.action.value,
                before_json=change.before,
                after_json=change.after,
                reason=change.reason,
                impact_json=change.impact,
                changed_at=change.changed_at,
            )
        )


class ProviderRequestRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        provider: str,
        operation: str,
        status: str,
        request_context: dict,
        duration_ms: float | None = None,
        error: str | None = None,
    ) -> UUID:
        request_id = uuid4()
        self.session.add(
            ProviderRequestRecord(
                id=request_id,
                provider=provider,
                operation=operation,
                request_context=request_context,
                status=status,
                duration_ms=duration_ms,
                error=error,
                requested_at=utc_now(),
            )
        )
        return request_id


class ProviderResponseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        request_id: UUID,
        provider: str,
        operation: str,
        payload: dict | list,
        status_code: int | None = None,
    ) -> UUID:
        response_id = uuid4()
        self.session.add(
            ProviderResponseRecord(
                id=response_id,
                request_id=request_id,
                provider=provider,
                operation=operation,
                status_code=status_code,
                payload_json=payload,
                received_at=utc_now(),
            )
        )
        return response_id


class ConversationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_trip(self, trip_id: UUID) -> UUID | None:
        record = self.session.scalar(
            select(TripConversationRecord).where(TripConversationRecord.trip_id == trip_id)
        )
        return record.id if record else None

    def get_or_create(self, trip_id: UUID) -> UUID:
        existing = self.session.scalar(
            select(TripConversationRecord).where(TripConversationRecord.trip_id == trip_id)
        )
        if existing:
            return existing.id
        now = utc_now()
        record = TripConversationRecord(trip_id=trip_id, created_at=now, updated_at=now)
        self.session.add(record)
        self.session.flush()
        return record.id

    def append_message(self, conversation_id: UUID, message: ConversationMessage) -> None:
        self.session.add(
            ConversationMessageRecord(
                id=message.id,
                conversation_id=conversation_id,
                role=message.role.value,
                content=message.content,
                tool_name=message.tool_name,
                tool_arguments=message.tool_arguments,
                tool_result_summary=message.tool_result_summary,
                model=message.model,
                input_tokens=message.input_tokens,
                output_tokens=message.output_tokens,
                latency_ms=message.latency_ms,
                created_at=message.created_at,
            )
        )
        record = self.session.get(TripConversationRecord, conversation_id)
        if record:
            record.updated_at = message.created_at

    def list_messages(self, conversation_id: UUID, limit: int = 20) -> list[ConversationMessage]:
        records = self.session.scalars(
            select(ConversationMessageRecord)
            .where(ConversationMessageRecord.conversation_id == conversation_id)
            .order_by(ConversationMessageRecord.created_at.desc())
            .limit(limit)
        ).all()
        return [
            ConversationMessage(
                id=record.id,
                role=record.role,
                content=record.content,
                tool_name=record.tool_name,
                tool_arguments=record.tool_arguments,
                tool_result_summary=record.tool_result_summary,
                model=record.model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                latency_ms=float(record.latency_ms) if record.latency_ms is not None else None,
                created_at=record.created_at,
            )
            for record in reversed(records)
        ]

    def record_tool_call(
        self,
        conversation_id: UUID,
        trip_id: UUID | None,
        tool_call_id: str,
        tool_name: str,
        arguments: dict,
        result_summary: dict,
        success: bool,
        duration_ms: float | None,
    ) -> None:
        self.session.add(
            AIToolCallRecord(
                conversation_id=conversation_id,
                trip_id=trip_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments_json=arguments,
                result_summary_json=result_summary,
                success=success,
                duration_ms=duration_ms,
                created_at=utc_now(),
            )
        )

    def record_run(
        self,
        conversation_id: UUID | None,
        trip_id: UUID | None,
        request_id: str,
        model: str | None,
        duration_ms: float | None,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost: float | None,
        success: bool,
        error: str | None = None,
    ) -> None:
        self.session.add(
            AIRunRecord(
                conversation_id=conversation_id,
                trip_id=trip_id,
                request_id=request_id,
                model=model,
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
                success=success,
                error=error,
                created_at=utc_now(),
            )
        )


class ApiKeyRepository:
    """Storage for bearer credentials.

    Lookup is by hash, never by the plaintext key: the caller hashes what was
    presented and asks for the digest, so the secret itself never reaches the
    database layer.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        key_hash: str,
        key_prefix: str,
        owner_id: str,
        label: str = "",
        scopes: list[str] | None = None,
        created_at: datetime | None = None,
    ) -> ApiKeyRecord:
        record = ApiKeyRecord(
            key_hash=key_hash,
            key_prefix=key_prefix,
            owner_id=owner_id,
            label=label,
            scopes=list(scopes or []),
            active=True,
            created_at=created_at or utc_now(),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        return self.session.scalars(
            select(ApiKeyRecord).where(ApiKeyRecord.key_hash == key_hash)
        ).first()

    def get(self, key_id: UUID) -> ApiKeyRecord | None:
        return self.session.get(ApiKeyRecord, key_id)

    def list(self, owner_id: str | None = None) -> list[ApiKeyRecord]:
        statement = select(ApiKeyRecord).order_by(ApiKeyRecord.created_at.desc())
        if owner_id is not None:
            statement = statement.where(ApiKeyRecord.owner_id == owner_id)
        return list(self.session.scalars(statement).all())

    def count_active(self) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ApiKeyRecord)
                .where(ApiKeyRecord.active.is_(True), ApiKeyRecord.revoked_at.is_(None))
            )
            or 0
        )

    def touch_last_used(self, key_id: UUID, when: datetime) -> None:
        record = self.session.get(ApiKeyRecord, key_id)
        if record is not None:
            record.last_used_at = when
            self.session.flush()

    def revoke(self, key_id: UUID, when: datetime | None = None) -> bool:
        record = self.session.get(ApiKeyRecord, key_id)
        if record is None or record.revoked_at is not None:
            return False
        record.active = False
        record.revoked_at = when or utc_now()
        self.session.flush()
        return True


class CredentialRepository:
    """Storage for sealed third-party credentials.

    Every method that returns a record takes ``owner_id`` and filters on it, so
    there is no way to read across tenants by accident. The ciphertext is stored
    and returned opaquely here — encryption and decryption live in
    ``services/credentials.py`` so this layer never handles plaintext or a key.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        *,
        owner_id: str,
        provider_id: str,
        credential_key: str,
        ciphertext: bytes,
        key_version: int = 1,
        when: datetime | None = None,
    ) -> CredentialRecord:
        """Insert or replace one credential.

        Upsert rather than insert-only because re-saving a key is the normal
        correction path when a user pastes a wrong one; the unique constraint
        turns that into an update instead of a duplicate row.
        """
        now = when or utc_now()
        record = self.get(owner_id, provider_id, credential_key)
        if record is None:
            record = CredentialRecord(
                owner_id=owner_id,
                provider_id=provider_id,
                credential_key=credential_key,
                ciphertext=ciphertext,
                key_version=key_version,
                created_at=now,
                updated_at=now,
            )
            self.session.add(record)
        else:
            record.ciphertext = ciphertext
            record.key_version = key_version
            record.updated_at = now
        self.session.flush()
        return record

    def get(self, owner_id: str, provider_id: str, credential_key: str) -> CredentialRecord | None:
        return self.session.scalars(
            select(CredentialRecord).where(
                CredentialRecord.owner_id == owner_id,
                CredentialRecord.provider_id == provider_id,
                CredentialRecord.credential_key == credential_key,
            )
        ).first()

    def list_for_owner(self, owner_id: str) -> list[CredentialRecord]:
        return list(
            self.session.scalars(
                select(CredentialRecord)
                .where(CredentialRecord.owner_id == owner_id)
                .order_by(CredentialRecord.provider_id, CredentialRecord.credential_key)
            ).all()
        )

    def delete(self, owner_id: str, provider_id: str, credential_key: str) -> bool:
        record = self.get(owner_id, provider_id, credential_key)
        if record is None:
            return False
        self.session.delete(record)
        self.session.flush()
        return True

    def delete_for_provider(self, owner_id: str, provider_id: str) -> int:
        result = self.session.execute(
            delete(CredentialRecord).where(
                CredentialRecord.owner_id == owner_id,
                CredentialRecord.provider_id == provider_id,
            )
        )
        self.session.flush()
        return int(result.rowcount or 0)
