from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from travel_api.db.uow import UnitOfWork
from travel_api.domain.models import (
    BudgetStrategyName,
    Pace,
    Trip,
    TripChange,
    TripPatchRequest,
    TripRevisionRequest,
    TripRevisionResult,
)
from travel_api.providers.registry import ProviderRegistry
from travel_api.services.aggregation import build_preview
from travel_api.services.budget_engine import BudgetEngine
from travel_api.services.freshness import FreshnessService
from travel_api.services.owner_context import resolve_owner
from travel_api.services.route_optimizer import RouteOptimizer

logger = logging.getLogger(__name__)


class TripNotFoundError(LookupError):
    pass


class TripRevisionError(ValueError):
    pass


class ConcurrentTripUpdateError(RuntimeError):
    """Another writer changed the trip between read and write.

    Distinct from ``TripRevisionError`` because the remedy differs: this one is
    retryable after a reload, whereas a revision error means the request itself
    was invalid.
    """


class VersionConflictError(ValueError):
    """A caller-supplied ``expected_version`` did not match the stored version."""


class TripService:
    def __init__(
        self,
        provider_registry: ProviderRegistry,
        uow_factory,
        freshness_service: FreshnessService | None = None,
        budget_engine: BudgetEngine | None = None,
        content_service=None,
    ) -> None:
        self.provider_registry = provider_registry
        self.uow_factory = uow_factory
        self.freshness = freshness_service or FreshnessService()
        self.budget_engine = budget_engine or BudgetEngine()
        # Optional, so a deployment that never wires ingestion still plans exactly as
        # before; the notes are an enhancement, not a dependency.
        self.content_service = content_service

    def _ingestions(self, owner_id: str | None):
        """The caller's stored notes, or nothing when ingestion is not configured.

        Read fresh on every plan rather than cached, so a note saved a moment ago is
        reflected immediately and one deleted stops appearing. Errors are swallowed to
        an empty list on purpose: a traveller's own optional notes must never be able to
        break trip planning.
        """
        if self.content_service is None:
            return []
        try:
            return self.content_service.list(owner_id=resolve_owner(owner_id), limit=100)
        except Exception as exc:  # noqa: BLE001 - notes are optional, planning is not
            logger.warning("could not read stored notes: %s", exc)
            return []

    async def preview(
        self,
        profile,
        budget_strategy: BudgetStrategyName | str = BudgetStrategyName.BALANCED,
        *,
        owner_id: str | None = None,
    ):
        # Scoped per call, not per process: which sources exist depends on whose
        # credentials are in play, so two owners can get different data from one
        # deployment. ``for_owner`` returns the shared registry unchanged when
        # nothing is configured, so the zero-credential path is unaffected.
        #
        # The owner may arrive as an argument or ambiently; the AI tool path has no
        # argument to pass, which is what ``owner_context`` is for.
        resolved = resolve_owner(owner_id)
        registry = self.provider_registry.for_owner(resolved)
        return await build_preview(
            profile,
            registry,
            budget_strategy,
            self.budget_engine,
            # The policy lives here so every plan is judged by the same rule rather than
            # by whatever a provider stamped on its own data.
            self.freshness,
            ingestions=self._ingestions(resolved),
        )

    async def create(
        self,
        profile,
        budget_strategy: BudgetStrategyName | str = BudgetStrategyName.BALANCED,
        *,
        owner_id: str | None = None,
    ) -> Trip:
        preview = await self.preview(profile, budget_strategy, owner_id=owner_id)
        trip = Trip(
            profile=profile,
            preview=preview,
            selected_hotel_offer_id=self._selected_offer_id(preview),
            selected_restaurant_ids=[restaurant.id for restaurant in preview.restaurants],
            selected_attraction_ids=[attraction.id for attraction in preview.attractions],
            itinerary=preview.itinerary,
            budget=preview.budget,
        )
        with self.uow_factory() as uow:
            self._persist_graph(uow, trip, owner_id=owner_id)
        return trip

    def get(self, trip_id: UUID, *, owner_id: str | None = None) -> Trip:
        with self.uow_factory() as uow:
            trip = uow.trips.get(trip_id, owner_id=owner_id)
        if trip is None:
            # A trip owned by somebody else is reported as missing, never as
            # forbidden: a distinct 403 would confirm the id exists.
            raise TripNotFoundError(str(trip_id))
        return trip

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        owner_id: str | None = None,
    ) -> list[Trip]:
        with self.uow_factory() as uow:
            return uow.trips.list(limit, offset, owner_id=owner_id)

    def count(self, *, owner_id: str | None = None) -> int:
        with self.uow_factory() as uow:
            return uow.trips.count(owner_id=owner_id)

    def owner_of(self, trip_id: UUID) -> str | None:
        with self.uow_factory() as uow:
            return uow.trips.owner_of(trip_id)

    def snapshots(self, trip_id: UUID, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        trip = self.get(trip_id, owner_id=owner_id)
        offer_ids = [offer.id for offer in (trip.preview.hotel_offers if trip.preview else [])]
        with self.uow_factory() as uow:
            records = uow.snapshots.list_offer_snapshots(offer_ids)
        grouped: dict[UUID, list[Any]] = {}
        for record in records:
            grouped.setdefault(record.offer_id, []).append(record)
        result = []
        for offer_id in offer_ids:
            history = grouped.get(offer_id, [])
            history_payload = [self._snapshot_payload(record) for record in history]
            diff = None
            if len(history) >= 2:
                previous, current = history[-2], history[-1]
                old_price = Decimal(str(previous.price))
                new_price = Decimal(str(current.price))
                difference = new_price - old_price
                diff = {
                    "old_price": str(old_price),
                    "new_price": str(new_price),
                    "difference": str(difference),
                    "percentage": float(difference / old_price * 100) if old_price else None,
                }
            result.append(
                {
                    "offer_id": str(offer_id),
                    "current": history_payload[-1] if history_payload else None,
                    "history": history_payload,
                    "diff": diff,
                }
            )
        return result

    async def refresh(self, trip_id: UUID, *, owner_id: str | None = None) -> Trip:
        trip = self.get(trip_id, owner_id=owner_id)
        preview = await self.preview(
            trip.profile,
            trip.budget.strategy if trip.budget else BudgetStrategyName.BALANCED,
            owner_id=owner_id,
        )
        next_trip = trip.model_copy(
            update={
                "preview": preview,
                "selected_hotel_offer_id": self._selected_offer_id(preview),
                "selected_restaurant_ids": [item.id for item in preview.restaurants],
                "selected_attraction_ids": [item.id for item in preview.attractions],
                "itinerary": preview.itinerary,
                "budget": preview.budget,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        with self.uow_factory() as uow:
            persisted = self._persist_graph(uow, next_trip, expected_version=trip.current_version)
        if not persisted:
            raise ConcurrentTripUpdateError(
                f"trip {trip_id} was modified by another request; reload and retry"
            )
        return next_trip

    async def patch(
        self,
        trip_id: UUID,
        patch: TripPatchRequest,
        *,
        owner_id: str | None = None,
        expected_version: int | None = None,
    ) -> Trip:
        trip = self.get(trip_id, owner_id=owner_id)
        self._assert_version(trip, expected_version)
        update = patch.model_dump(exclude_unset=True, exclude_none=True)
        update.pop("budget_strategy", None)
        update.pop("expected_version", None)
        profile = trip.profile.model_copy(update=update)
        preview = await self.preview(
            profile, patch.budget_strategy or BudgetStrategyName.BALANCED, owner_id=owner_id
        )
        next_trip = trip.model_copy(
            update={
                "profile": profile,
                "preview": preview,
                "selected_hotel_offer_id": self._selected_offer_id(preview),
                "selected_restaurant_ids": [item.id for item in preview.restaurants],
                "selected_attraction_ids": [item.id for item in preview.attractions],
                "itinerary": preview.itinerary,
                "budget": preview.budget,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        change = TripChange(
            version=next_trip.current_version,
            action="change_budget" if "total_budget" in update else "change_pace",
            before={"profile": trip.profile.model_dump(mode="json")},
            after={"profile": profile.model_dump(mode="json")},
            impact=self._budget_impact(trip.budget, preview.budget),
        )
        next_trip.change_history.append(change)
        with self.uow_factory() as uow:
            persisted = self._persist_graph(
                uow, next_trip, change, expected_version=trip.current_version
            )
        if not persisted:
            raise ConcurrentTripUpdateError(
                f"trip {trip_id} was modified by another request; reload and retry"
            )
        return next_trip

    async def revise(
        self,
        trip_id: UUID,
        request: TripRevisionRequest,
        *,
        owner_id: str | None = None,
    ) -> TripRevisionResult:
        trip = self.get(trip_id, owner_id=owner_id)
        if trip.preview is None:
            raise TripRevisionError("trip has no preview")
        if request.action.value == "replace_hotel":
            next_trip = self._replace_hotel(trip, request.constraints)
        elif request.action.value == "change_budget":
            next_trip = await self._change_budget(trip, request.constraints, owner_id=owner_id)
        elif request.action.value == "remove_attraction":
            next_trip = self._remove_attraction(trip, request.constraints)
        elif request.action.value == "change_pace":
            next_trip = self._change_pace(trip, request.constraints)
        elif request.action.value == "replace_restaurant":
            next_trip = self._replace_restaurant(trip, request.constraints)
        elif request.action.value == "add_attraction":
            next_trip = self._change_attraction_selection(trip, request.constraints, add=True)
        elif request.action.value == "reduce_transport":
            next_trip = self._change_pace(trip, {"pace": "relaxed"})
        elif request.action.value == "increase_food_budget":
            next_trip = self._increase_food_budget(trip, request.constraints)
        elif request.action.value == "reduce_walking":
            next_trip = self._change_pace(trip, {"pace": "relaxed"})
        else:
            raise TripRevisionError(f"unsupported revision action: {request.action.value}")

        change = TripChange(
            version=next_trip.current_version,
            action=request.action,
            before={
                "selected_hotel_offer_id": str(trip.selected_hotel_offer_id)
                if trip.selected_hotel_offer_id
                else None
            },
            after={
                "selected_hotel_offer_id": str(next_trip.selected_hotel_offer_id)
                if next_trip.selected_hotel_offer_id
                else None
            },
            impact=self._budget_impact(trip.budget, next_trip.budget),
        )
        next_trip.change_history.append(change)
        with self.uow_factory() as uow:
            persisted = self._persist_graph(
                uow, next_trip, change, expected_version=trip.current_version
            )
        if not persisted:
            raise ConcurrentTripUpdateError(
                f"trip {trip_id} was modified by another request; reload and retry"
            )
        return TripRevisionResult(trip=next_trip, change=change)

    def _replace_restaurant(self, trip: Trip, constraints: dict[str, Any]) -> Trip:
        preview = trip.preview
        requested_id = str(constraints.get("restaurant_id", ""))
        candidates = list(preview.restaurants)
        if not candidates:
            raise TripRevisionError("no eligible restaurant alternative")
        max_price = constraints.get("max_price")
        if max_price is not None:
            candidates = [
                item
                for item in candidates
                if item.average_cost is not None
                and item.average_cost.value <= Decimal(str(max_price))
            ]
        if not candidates:
            raise TripRevisionError("no eligible restaurant alternative")
        selected = next(
            (item for item in candidates if str(item.id) == requested_id),
            None,
        )
        if selected is None:
            selected = min(
                candidates,
                key=lambda item: item.average_cost.value if item.average_cost else Decimal("0"),
            )
        return self._recalculate_restaurants(trip, [selected])

    def _recalculate_restaurants(self, trip: Trip, restaurants) -> Trip:
        preview = trip.preview
        selected_offer = self._selected_offer(preview, trip.selected_hotel_offer_id)
        hotel = next(
            (
                item
                for item in preview.hotels
                if item.id == self._selected_hotel_id(preview, trip.selected_hotel_offer_id)
            ),
            None,
        )
        itinerary = RouteOptimizer().optimize(
            trip.profile,
            hotel.id if hotel else None,
            (
                hotel.location.coordinates.latitude,
                hotel.location.coordinates.longitude,
            )
            if hotel
            else (0.0, 0.0),
            preview.attractions,
            restaurants,
            preview.route_legs,
        )
        budget = BudgetEngine().calculate(
            trip.profile,
            selected_offer,
            restaurants,
            preview.attractions,
            preview.route_legs,
            preview.budget.strategy,
        )
        next_preview = preview.model_copy(update={"itinerary": itinerary, "budget": budget})
        return trip.model_copy(
            update={
                "preview": next_preview,
                "selected_restaurant_ids": [item.id for item in restaurants],
                "itinerary": itinerary,
                "budget": budget,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def _change_attraction_selection(
        self, trip: Trip, constraints: dict[str, Any], *, add: bool
    ) -> Trip:
        target = str(constraints.get("attraction_id", ""))
        attraction = next(
            (item for item in trip.preview.attractions if str(item.id) == target), None
        )
        if attraction is None:
            raise TripRevisionError("attraction not found")
        selected = {str(item_id) for item_id in trip.selected_attraction_ids}
        if add:
            selected.add(target)
        else:
            selected.discard(target)
        return self._recalculate_attractions(
            trip, [item for item in trip.preview.attractions if str(item.id) in selected]
        )

    def _recalculate_attractions(self, trip: Trip, attractions) -> Trip:
        preview = trip.preview
        selected_offer = self._selected_offer(preview, trip.selected_hotel_offer_id)
        hotel_id = self._selected_hotel_id(preview, trip.selected_hotel_offer_id)
        itinerary = RouteOptimizer().optimize(
            trip.profile,
            hotel_id,
            (0.0, 0.0),
            attractions,
            preview.restaurants,
            preview.route_legs,
        )
        budget = BudgetEngine().calculate(
            trip.profile,
            selected_offer,
            preview.restaurants,
            attractions,
            preview.route_legs,
            preview.budget.strategy,
        )
        next_preview = preview.model_copy(
            update={"attractions": attractions, "itinerary": itinerary, "budget": budget}
        )
        return trip.model_copy(
            update={
                "preview": next_preview,
                "selected_attraction_ids": [item.id for item in attractions],
                "itinerary": itinerary,
                "budget": budget,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def _increase_food_budget(self, trip: Trip, constraints: dict[str, Any]) -> Trip:
        amount = Decimal(str(constraints.get("food_budget", "0")))
        profile = trip.profile.model_copy(update={"food_budget": amount})
        budget = BudgetEngine().calculate(
            profile,
            self._selected_offer(trip.preview, trip.selected_hotel_offer_id),
            trip.preview.restaurants,
            trip.preview.attractions,
            trip.preview.route_legs,
            trip.budget.strategy,
        )
        preview = trip.preview.model_copy(update={"profile": profile, "budget": budget})
        return trip.model_copy(
            update={
                "profile": profile,
                "preview": preview,
                "budget": budget,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    @staticmethod
    def _snapshot_payload(record) -> dict[str, Any]:
        return {
            "id": str(record.id),
            "offer_id": str(record.offer_id),
            "price": str(record.price),
            "currency": record.currency,
            "tax": str(record.tax) if record.tax is not None else None,
            "service_fee": str(record.service_fee) if record.service_fee is not None else None,
            "availability": record.availability,
            "observed_at": record.observed_at.isoformat(),
            "fetched_at": record.fetched_at.isoformat(),
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "source_reference_id": record.source_reference_id,
        }

    def _replace_hotel(self, trip: Trip, constraints: dict[str, Any]) -> Trip:
        preview = trip.preview
        max_price = Decimal(str(constraints["max_price"])) if "max_price" in constraints else None
        candidates = [
            offer
            for offer in preview.hotel_offers
            if offer.id != trip.selected_hotel_offer_id
            and (max_price is None or offer.total_price <= max_price)
            and not self.freshness.is_stale(offer.source_reference.expires_at)
        ]
        if not candidates:
            raise TripRevisionError("no eligible hotel alternative")
        selected_offer = min(candidates, key=lambda offer: offer.total_price)
        hotel = next(hotel for hotel in preview.hotels if hotel.id == selected_offer.hotel_id)
        itinerary = RouteOptimizer().optimize(
            trip.profile,
            hotel.id,
            (hotel.location.coordinates.latitude, hotel.location.coordinates.longitude),
            preview.attractions,
            preview.restaurants,
            preview.route_legs,
        )
        budget = BudgetEngine().calculate(
            trip.profile,
            selected_offer,
            preview.restaurants,
            preview.attractions,
            preview.route_legs,
            preview.budget.strategy,
        )
        next_preview = preview.model_copy(update={"itinerary": itinerary, "budget": budget})
        return trip.model_copy(
            update={
                "preview": next_preview,
                "selected_hotel_offer_id": selected_offer.id,
                "itinerary": itinerary,
                "budget": budget,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    async def _change_budget(
        self,
        trip: Trip,
        constraints: dict[str, Any],
        *,
        owner_id: str | None = None,
    ) -> Trip:
        total_budget = Decimal(str(constraints["total_budget"]))
        hotel_budget = (
            Decimal(str(constraints["hotel_budget"]))
            if "hotel_budget" in constraints
            else trip.profile.hotel_budget
        )
        profile = trip.profile.model_copy(
            update={"total_budget": total_budget, "hotel_budget": hotel_budget}
        )
        # The owner is forwarded explicitly rather than left to the ambient context.
        # This revision rebuilds the whole preview, so losing the owner here would
        # silently drop a credentialed provider — the re-planned trip would quietly
        # fall back to the free sources and produce worse routes than the first plan.
        preview = await self.preview(
            profile,
            constraints.get("strategy", BudgetStrategyName.STRICT),
            owner_id=owner_id,
        )
        return trip.model_copy(
            update={
                "profile": profile,
                "preview": preview,
                "selected_hotel_offer_id": self._selected_offer_id(preview),
                "selected_restaurant_ids": [item.id for item in preview.restaurants],
                "selected_attraction_ids": [item.id for item in preview.attractions],
                "itinerary": preview.itinerary,
                "budget": preview.budget,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def _remove_attraction(self, trip: Trip, constraints: dict[str, Any]) -> Trip:
        target = str(constraints.get("attraction_id", ""))
        preview = trip.preview
        attractions = [item for item in preview.attractions if str(item.id) != target]
        if len(attractions) == len(preview.attractions):
            raise TripRevisionError("attraction not found")
        itinerary = RouteOptimizer().optimize(
            trip.profile,
            self._selected_hotel_id(preview, trip.selected_hotel_offer_id),
            (0.0, 0.0),
            attractions,
            preview.restaurants,
            preview.route_legs,
        )
        budget = BudgetEngine().calculate(
            trip.profile,
            self._selected_offer(preview, trip.selected_hotel_offer_id),
            preview.restaurants,
            attractions,
            preview.route_legs,
            preview.budget.strategy,
        )
        next_preview = preview.model_copy(
            update={"attractions": attractions, "itinerary": itinerary, "budget": budget}
        )
        return trip.model_copy(
            update={
                "preview": next_preview,
                "selected_attraction_ids": [item.id for item in attractions],
                "itinerary": itinerary,
                "budget": budget,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def _change_pace(self, trip: Trip, constraints: dict[str, Any]) -> Trip:
        pace = Pace(constraints.get("pace", Pace.RELAXED))
        profile = trip.profile.model_copy(update={"pace": pace})
        preview = trip.preview
        hotel = next(
            (
                item
                for item in preview.hotels
                if item.id == self._selected_hotel_id(preview, trip.selected_hotel_offer_id)
            ),
            None,
        )
        itinerary = RouteOptimizer().optimize(
            profile,
            hotel.id if hotel else None,
            (hotel.location.coordinates.latitude, hotel.location.coordinates.longitude)
            if hotel
            else (0.0, 0.0),
            preview.attractions,
            preview.restaurants,
            preview.route_legs,
        )
        next_preview = preview.model_copy(update={"profile": profile, "itinerary": itinerary})
        return trip.model_copy(
            update={
                "profile": profile,
                "preview": next_preview,
                "itinerary": itinerary,
                "current_version": trip.current_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    @staticmethod
    def _assert_version(trip: Trip, expected_version: int | None) -> None:
        """Reject the request early when the caller is working from a stale read.

        Failing before the expensive preview is both faster and clearer than
        doing the work and then losing the compare-and-swap.
        """
        if expected_version is not None and expected_version != trip.current_version:
            raise VersionConflictError(
                f"expected_version {expected_version} does not match the stored "
                f"version {trip.current_version}"
            )

    def _persist_graph(
        self,
        uow: UnitOfWork,
        trip: Trip,
        change: TripChange | None = None,
        *,
        owner_id: str | None = None,
        expected_version: int | None = None,
    ) -> bool:
        """Persist the whole trip graph. Returns ``False`` on a lost update."""
        if trip.preview is None:
            raise ValueError("cannot persist trip without preview")
        record = uow.trips.get(trip.id)
        if record is None:
            uow.trips.create(trip, owner_id=owner_id)
        else:
            updated = uow.trips.update(trip, expected_version=expected_version)
            if updated is None:
                # Another writer advanced the version first. Nothing else in the
                # graph may be written, or the snapshot tables would end up
                # describing a trip state that was never stored.
                return False
            if owner_id is not None and updated.owner_id is None:
                # Claim an unowned row on first authenticated write, so trips
                # created before auth existed become owned rather than staying
                # readable by everyone.
                uow.trips.set_owner(trip.id, owner_id)
        uow.trip_profiles.upsert(trip)
        preview = trip.preview
        refs = list(preview.source_references)
        refs.extend(hotel_ref for hotel in preview.hotels for hotel_ref in hotel.source_references)
        refs.extend(offer.source_reference for offer in preview.hotel_offers)
        refs.extend(ref for item in preview.restaurants for ref in item.source_references)
        refs.extend(ref for item in preview.attractions for ref in item.source_references)
        refs.extend(route.source_reference for route in preview.route_legs)
        unique_refs = {f"{reference.source}:{reference.source_id}": reference for reference in refs}
        uow.source_references.upsert_many(list(unique_refs.values()))
        uow.hotels.upsert_many(preview.hotels)
        uow.hotel_offers.upsert_many(preview.hotel_offers)
        uow.snapshots.append_offer_snapshots(preview.hotel_offers)
        uow.restaurants.upsert_many(preview.restaurants)
        uow.attractions.upsert_many(preview.attractions)
        uow.routes.append_snapshots(preview.route_legs)
        uow.decisions.replace(trip.id, preview.hotel_decisions)
        uow.budgets.replace(trip.id, preview.budget)
        uow.itineraries.replace(trip.id, trip.itinerary)
        for audit in preview.provider_audit:
            request_id = uow.provider_requests.record(
                provider=audit["provider"],
                operation=audit["operation"],
                status=audit["status"],
                request_context={"trip_id": str(trip.id)},
                error="; ".join(audit["errors"]) if audit["errors"] else None,
            )
            uow.provider_responses.record(
                request_id=request_id,
                provider=audit["provider"],
                operation=audit["operation"],
                payload=audit["payload"],
                status_code=200 if audit["status"] == "success" else None,
            )
        if change:
            uow.changes.append(trip.id, change)
        return True

    @staticmethod
    def _selected_offer_id(preview) -> UUID | None:
        return preview.hotel_decisions[0].candidate_id if preview.hotel_decisions else None

    @staticmethod
    def _selected_offer(preview, offer_id: UUID | None):
        return next((offer for offer in preview.hotel_offers if offer.id == offer_id), None)

    @staticmethod
    def _selected_hotel_id(preview, offer_id: UUID | None):
        offer = TripService._selected_offer(preview, offer_id)
        return offer.hotel_id if offer else None

    @staticmethod
    def _budget_impact(before, after) -> dict[str, Any]:
        if before is None or after is None:
            return {}
        return {
            "before_total": str(before.total),
            "after_total": str(after.total),
            "difference": str(after.total - before.total),
            "before_remaining": str(before.remaining) if before.remaining is not None else None,
            "after_remaining": str(after.remaining) if after.remaining is not None else None,
        }
