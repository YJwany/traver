from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from travel_api.db.repositories import (
    ApiKeyRepository,
    AttractionRepository,
    BudgetRepository,
    ContentIngestionRepository,
    ConversationRepository,
    CredentialRepository,
    DecisionRepository,
    HotelOfferRepository,
    HotelRepository,
    ItineraryRepository,
    ProviderRequestRepository,
    ProviderResponseRepository,
    RestaurantRepository,
    RouteRepository,
    SnapshotRepository,
    SourceReferenceRepository,
    TripChangeRepository,
    TripProfileRepository,
    TripRepository,
)


class UnitOfWork(AbstractContextManager["UnitOfWork"]):
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> "UnitOfWork":
        self.session = self._session_factory()
        self.trips = TripRepository(self.session)
        self.trip_profiles = TripProfileRepository(self.session)
        self.hotels = HotelRepository(self.session)
        self.hotel_offers = HotelOfferRepository(self.session)
        self.restaurants = RestaurantRepository(self.session)
        self.attractions = AttractionRepository(self.session)
        self.source_references = SourceReferenceRepository(self.session)
        self.snapshots = SnapshotRepository(self.session)
        self.budgets = BudgetRepository(self.session)
        self.routes = RouteRepository(self.session)
        self.decisions = DecisionRepository(self.session)
        self.itineraries = ItineraryRepository(self.session)
        self.changes = TripChangeRepository(self.session)
        self.provider_requests = ProviderRequestRepository(self.session)
        self.provider_responses = ProviderResponseRepository(self.session)
        self.conversations = ConversationRepository(self.session)
        self.api_keys = ApiKeyRepository(self.session)
        self.credentials = CredentialRepository(self.session)
        self.ingestions = ContentIngestionRepository(self.session)
        return self

    def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("unit of work is not active")
        self.session.commit()

    def rollback(self) -> None:
        if self.session is not None:
            self.session.rollback()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is not None:
                self.rollback()
            elif self.session is not None:
                self.commit()
        finally:
            if self.session is not None:
                self.session.close()
                self.session = None
        return False
