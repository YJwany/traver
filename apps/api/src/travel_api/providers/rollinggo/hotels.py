"""RollingGo hotel search and room rates, as a Traver provider.

Two capabilities from one source:

* ``HotelProvider.search_hotels`` — finds hotels in the destination.
* ``RoomOfferProvider.search_room_offers`` — prices hotels the plan already contains.

The second is why this package exists. It is the first source in this deployment that can
produce a room rate at all, so the moment it is built ``search_room_offers`` stops being a
capability nothing can serve and becomes one a credential unlocks.

Three rules this provider keeps
-------------------------------
1. **No caching.** A stored rate read back later and presented as current is exactly the
   failure the product must not produce, and the requirement is a plan for a *future* trip.
   Every call is live and every reference carries the fetch time.
2. **``observed_at`` is our fetch time, and the basis says so.** The source returns a live
   quote and states nothing about when the price was produced, so the reference is
   ``SOURCE_SILENT`` — not ``LIVE_COMPUTED``, which would claim the freshness describes the
   data rather than our request.
3. **No price is invented, and no quote is attached to the wrong hotel.** A hotel this
   source did not issue is skipped rather than matched by name, because a rate shown against
   the wrong hotel looks exactly like a correct one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from travel_api.domain.models import (
    Coordinates,
    DataDomain,
    Hotel,
    HotelOffer,
    Location,
    Money,
    ObservationBasis,
    PriceType,
    ProviderResult,
    SourceReference,
    SourceType,
    TripProfile,
)
from travel_api.providers.mock import stable_id
from travel_api.providers.rollinggo.client import (
    ROLLINGGO_PROVIDER_ID,
    RollingGoMcpClient,
)

#: The registry name. Distinct from the *source* id (``ROLLINGGO_PROVIDER_ID``), because the
#: registry is keyed by name and the source id is what the settings page writes credentials
#: against.
ROLLINGGO_HOTEL_PROVIDER_NAME = "rollinggo-hotels"

#: How many hotels one search asks for. The server documents a default of 10 and a maximum
#: of 20; the lower end keeps a plan's quota spend modest, and the planner does not need
#: more candidates than it will ever rank.
SEARCH_SIZE = 10

#: Keys a hotel array plausibly sits under. Not a guess list to paper over an unknown:
#: whichever matches is recorded on the source reference, so a real run reports the layout
#: instead of the assumption. Nothing matching yields no hotels.
HOTEL_LIST_KEYS: tuple[str, ...] = ("hotels", "list", "items", "results", "data")

#: The same, for a room-rate array from ``getHotelDetail``.
ROOM_LIST_KEYS: tuple[str, ...] = (
    "roomRatePlans",
    "roomRatePlanList",
    "ratePlans",
    "rooms",
    "roomTypes",
    "list",
    "data",
)


def _first(mapping: Any, keys: tuple[str, ...]) -> Any:
    """First present, non-null value among ``keys``. ``None`` when none is."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def locate_list(payload: Any, keys: tuple[str, ...]) -> tuple[list[Any], str]:
    """Find the array of records in a payload whose envelope is not verified.

    Returns the rows and the key path that produced them, or ``([], "")``. The key path is
    reported rather than swallowed so a real run says which layout the server used — the
    same discipline as 高德's ``duration_field``: accepting any of several plausible paths
    can only turn "no rows" into "the rows the source really sent", never fabricate one.
    """
    if isinstance(payload, list):
        return payload, "<root>"
    if not isinstance(payload, dict):
        return [], ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value, key
    # One level down: some tools wrap their payload so the array sits under a nested object.
    for wrapper in ("result", "data", "payload"):
        nested = payload.get(wrapper)
        if isinstance(nested, (dict, list)):
            rows, key = locate_list(nested, keys)
            if rows or key:
                return rows, f"{wrapper}.{key}" if key else wrapper
    return [], ""


def _to_decimal(value: Any) -> Decimal | None:
    """Parse a number, or ``None``. Never guesses: an unparseable amount is absent."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    number = _to_decimal(value)
    return None if number is None else float(number)


def _to_int(value: Any) -> int | None:
    number = _to_decimal(value)
    return None if number is None else int(round(number))


def _coordinates(row: Any) -> Coordinates | None:
    """Latitude-first coordinates from whichever shape the source used, or ``None``.

    A hotel with no usable coordinate is dropped rather than placed at a guess: an
    itinerary point at the wrong place is worse than a missing one.
    """
    latitude = longitude = None
    geo = _first(row, ("geo", "location", "coordinates", "coordinate"))
    if isinstance(geo, dict):
        latitude = _to_float(_first(geo, ("latitude", "lat")))
        longitude = _to_float(_first(geo, ("longitude", "lon", "lng")))
    if latitude is None:
        latitude = _to_float(_first(row, ("latitude", "lat")))
    if longitude is None:
        longitude = _to_float(_first(row, ("longitude", "lon", "lng")))
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return Coordinates(latitude=latitude, longitude=longitude)


class RollingGoHotelProvider:
    """Hotels and live room rates from RollingGo, for one owner's key."""

    def __init__(
        self,
        client: RollingGoMcpClient,
        *,
        name: str = ROLLINGGO_HOTEL_PROVIDER_NAME,
        ttl_seconds: int = 1800,
    ) -> None:
        self.client = client
        self.name = name
        self.ttl_seconds = ttl_seconds

    # -- capability: HotelProvider ----------------------------------------
    async def search_hotels(self, profile: TripProfile) -> ProviderResult:
        nights = max(1, (profile.end_date - profile.start_date).days)
        payload = await self.client.call_tool(
            "searchHotels",
            {
                "originQuery": f"{profile.destination}酒店",
                "place": profile.destination,
                "placeType": "城市",
                "checkInParam": {
                    "checkInDate": profile.start_date.isoformat(),
                    "stayNights": nights,
                },
                "size": SEARCH_SIZE,
            },
        )
        rows, matched_key = locate_list(payload, HOTEL_LIST_KEYS)
        hotels = [hotel for row in rows if (hotel := self._hotel(row, matched_key)) is not None]
        return ProviderResult(
            provider=self.name,
            data=hotels,
            source_references=[ref for hotel in hotels for ref in hotel.source_references],
        )

    # -- capability: RoomOfferProvider ------------------------------------
    async def search_room_offers(self, hotels: list[Hotel], profile: TripProfile) -> ProviderResult:
        """Price the hotels this source issued.

        The external id is read from the hotel's own source references — the reason
        ``search_room_offers`` takes entities rather than ids. A hotel another source found
        is skipped: this source has no id for it, and matching by name and coordinate is not
        done here because a rate attached to the wrong hotel is indistinguishable from a
        correct one.
        """
        offers: list[HotelOffer] = []
        for hotel in hotels:
            source_id = self._source_id_of(hotel)
            if source_id is None:
                continue
            payload = await self.client.call_tool(
                "getHotelDetail",
                {
                    "hotelId": source_id,
                    "checkInDate": profile.start_date.isoformat(),
                    "checkOutDate": profile.end_date.isoformat(),
                    "roomCount": 1,
                    "adultCount": profile.travelers,
                },
            )
            rows, matched_key = locate_list(payload, ROOM_LIST_KEYS)
            for row in rows:
                offer = self._offer(hotel, row, source_id, matched_key, profile)
                if offer is not None:
                    offers.append(offer)
        return ProviderResult(
            provider=self.name,
            data=offers,
            source_references=[offer.source_reference for offer in offers],
        )

    # -- mapping -----------------------------------------------------------
    def _hotel(self, row: Any, matched_key: str) -> Hotel | None:
        source_id = _first(row, ("hotelId", "hotel_id", "id", "code"))
        name = _first(row, ("name", "nameCn", "hotelName", "nameEn"))
        coordinates = _coordinates(row)
        if source_id is None or not name or coordinates is None:
            # A row without an id, a name or a coordinate cannot be a usable hotel, and
            # dropping it is the honest outcome — inventing any of the three would place a
            # real hotel somewhere it is not.
            return None
        reference = self._reference(
            str(source_id),
            request_context={"tool": "searchHotels", "payload_key": matched_key},
        )
        return Hotel(
            id=stable_id(f"rollinggo:hotel:{source_id}"),
            canonical_name=str(name),
            location=Location(
                address=str(_first(row, ("address", "addressCn", "fullAddress")) or ""),
                city=str(_first(row, ("city", "cityName", "cityCn")) or ""),
                coordinates=coordinates,
            ),
            source_references=[reference],
        )

    def _offer(
        self,
        hotel: Hotel,
        row: Any,
        hotel_source_id: str,
        matched_key: str,
        profile: TripProfile,
    ) -> HotelOffer | None:
        room_key = _first(row, ("roomTypeId", "roomTypeID", "roomId"))
        room_name = _first(row, ("roomName", "roomNameCn", "name", "roomTypeName"))
        rate_key = _first(row, ("ratePlanId", "ratePlanID", "rateId"))
        rate_name = _first(row, ("ratePlanName", "rateName", "name"))
        if room_key is None and not room_name:
            return None

        # ``totalPrice`` is the stay total and ``averagePrice`` the nightly rate; the two are
        # different quantities, so the price type follows whichever was read rather than
        # being assumed.
        total = _to_decimal(_first(row, ("totalPrice", "amount", "price")))
        average = _to_decimal(_first(row, ("averagePrice", "pricePerNight", "nightlyPrice")))
        price = total if total is not None else average
        if price is None:
            # No amount means no offer. An unpriced "offer" would enter the budget as a
            # commercial fact with a zero in it.
            return None
        price_type = PriceType.PER_STAY if total is not None else PriceType.PER_NIGHT

        currency = str(_first(row, ("currency", "currencyCode")) or "CNY").upper()[:3]
        reference = self._reference(
            f"{hotel_source_id}:{room_key or room_name}",
            request_context={"tool": "getHotelDetail", "payload_key": matched_key},
        )
        # 「需申请」不等于「有房」: a room that must be requested has no confirmed inventory,
        # so it must not be reported as availability > 0.
        on_request = bool(_first(row, ("isOnRequest", "onRequest", "is_on_request")) or False)
        availability = (
            None
            if on_request
            else _to_int(_first(row, ("availability", "availableCount", "inventory")))
        )
        return HotelOffer(
            id=stable_id(f"rollinggo:offer:{hotel_source_id}:{rate_key or rate_name or room_key}"),
            hotel_id=hotel.id,
            room_type_id=stable_id(f"rollinggo:room:{hotel_source_id}:{room_key or room_name}"),
            rate_plan_id=stable_id(
                f"rollinggo:rate:{hotel_source_id}:{rate_key or rate_name or room_key}"
            ),
            # The dates come from the request, not the response: they are what was asked
            # for, and the source is not being trusted to echo them back correctly.
            check_in=profile.start_date,
            check_out=profile.end_date,
            guests=profile.travelers,
            room_count=1,
            meal_plan=_first(row, ("mealPlan", "mealPlanName", "boardType", "board")),
            cancellation_policy=_cancellation_text(row),
            payment_policy=_first(row, ("paymentPolicy", "payType", "paymentType")),
            price=Money(
                value=price,
                currency=currency,
                price_type=price_type,
                source_reference_id=reference.source_id,
            ),
            tax=_money(_first(row, ("tax", "taxAmount", "taxes")), currency),
            service_fee=_money(_first(row, ("serviceFee", "fee", "serviceCharge")), currency),
            availability=availability,
            source_reference=reference,
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _source_id_of(hotel: Hotel) -> str | None:
        for reference in hotel.source_references:
            if reference.source == ROLLINGGO_PROVIDER_ID:
                return reference.source_id
        return None

    def _reference(
        self, source_id: str, *, request_context: dict[str, Any] | None = None
    ) -> SourceReference:
        now = datetime.now(timezone.utc)
        return SourceReference(
            source=ROLLINGGO_PROVIDER_ID,
            source_type=SourceType.PARTNER_API,
            source_id=source_id,
            observed_at=now,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            # The source answers a live query and says nothing about when the price was
            # produced. ``observed_at`` is therefore our fetch time, and the basis says so:
            # ``LIVE_COMPUTED`` would claim the age describes the data, when it describes
            # our request — whether the upstream is itself cached is not something we know.
            observed_at_basis=ObservationBasis.SOURCE_SILENT,
            domain=DataDomain.COMMERCIAL_FACT,
            request_context=request_context or {},
        )


def _money(value: Any, currency: str) -> Money | None:
    """A tax or fee amount, or ``None``. Never ``0`` for an absent field.

    ``0`` is a claim ("this costs nothing"); ``None`` is the absence of a claim, and the
    budget layer is what decides how to present an unknown.
    """
    amount = _to_decimal(value)
    if amount is None:
        return None
    return Money(value=amount, currency=currency)


def _cancellation_text(row: Any) -> str | None:
    """The source's own cancellation terms as text, without interpreting them.

    Values are rendered, keys are not: a policy that is free before a date and penalised
    after it reads as the source stated it, and nothing here infers a rule the source did
    not give.
    """
    policies = _first(row, ("cancellationPolicies", "cancellationPolicyList", "cancellationPolicy"))
    if isinstance(policies, str):
        return policies
    if isinstance(policies, list):
        parts: list[str] = []
        for item in policies:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                from_date = _first(item, ("fromDate", "from", "date"))
                amount = _first(item, ("amount", "fee", "penalty"))
                if from_date is None and amount is None:
                    continue
                parts.append(f"{from_date if from_date is not None else '未注明时间'} 起 {amount}")
        return "；".join(parts) or None
    return None
