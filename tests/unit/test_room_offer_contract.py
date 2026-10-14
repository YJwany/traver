"""The room-offer call hands over hotels, not ids.

``Hotel.id`` is a locally generated UUID (``uuid4``), so a supply source given only that
string has nothing to look up with — no name, no coordinate, no id of its own. Its correct
behaviour (return no offers) is then indistinguishable from a source that has no rates at
all, which is the silent gap ``RoomOfferProvider`` was split out to expose. The contract
therefore carries the entity, and the id a supplier can actually resolve travels on
``hotel.source_references[*].source_id``.

Both halves are pinned, because either one alone is satisfiable by a broken design: the
argument could be entities whose source references are empty, or a signature that still
took ids and merely happened to be called with objects.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import get_type_hints

from travel_api.domain.models import (
    Coordinates,
    DataDomain,
    Hotel,
    Location,
    ProviderResult,
    SourceReference,
    SourceType,
    TripProfile,
)
from travel_api.providers.base import RoomOfferProvider
from travel_api.providers.registry import ProviderRegistry


class RoomOfferRecorder:
    """A room-offer source that remembers what it was handed."""

    name = "room-offer-recorder"

    def __init__(self) -> None:
        self.seen: list[Hotel] | None = None

    async def search_room_offers(self, hotels: list[Hotel], profile: TripProfile) -> ProviderResult:
        self.seen = hotels
        return ProviderResult(provider=self.name)


def _profile() -> TripProfile:
    return TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 7),
        travelers=2,
        total_budget=Decimal("5000"),
    )


def _hotel(source_id: str) -> Hotel:
    now = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)
    return Hotel(
        canonical_name="某酒店",
        location=Location(
            address="某路 1 号",
            city="成都",
            coordinates=Coordinates(latitude=30.65, longitude=104.08),
        ),
        source_references=[
            SourceReference(
                source="rollinggo",
                source_type=SourceType.PARTNER_API,
                source_id=source_id,
                observed_at=now,
                fetched_at=now,
                domain=DataDomain.COMMERCIAL_FACT,
            )
        ],
    )


async def test_a_room_offer_source_receives_hotel_entities():
    recorder = RoomOfferRecorder()
    registry = ProviderRegistry([recorder])
    hotel = _hotel("supplier-42")

    await registry.call(recorder, "search_room_offers", [hotel], _profile())

    assert recorder.seen == [hotel]


async def test_the_id_a_supplier_can_resolve_travels_on_the_entity():
    """The half that makes the entity argument worth anything.

    ``Hotel.id`` is a ``uuid4`` minted here; no external source can resolve it. The id the
    supplier issued is on the source reference, and it has to survive the trip — an entity
    whose references were empty would satisfy the signature while leaving a supplier exactly
    as stuck as it was with a bare UUID.
    """
    recorder = RoomOfferRecorder()
    registry = ProviderRegistry([recorder])

    await registry.call(recorder, "search_room_offers", [_hotel("supplier-42")], _profile())

    assert recorder.seen is not None
    handed = recorder.seen[0]
    assert str(handed.id) != "supplier-42"
    assert [ref.source_id for ref in handed.source_references] == ["supplier-42"]


def test_the_contract_takes_hotels_not_ids():
    """A structural guard, so reverting the signature fails here and not in the field.

    ``runtime_checkable`` only checks that a method exists, never its signature, so nothing
    else in the suite would notice a revert to ``hotel_ids: list[str]`` — and the failure
    that produces is a supplier quietly returning nothing, which reads as "this source has
    no rates". Checking the annotation is the only way to make the vocabulary itself
    asserted rather than assumed.
    """
    hints = get_type_hints(RoomOfferProvider.search_room_offers)

    assert hints["hotels"] == list[Hotel]
    assert "hotel_ids" not in hints
