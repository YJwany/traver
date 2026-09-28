from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid5

from travel_api.domain.models import (
    Attraction,
    Coordinates,
    DataDomain,
    Hotel,
    HotelOffer,
    Location,
    Money,
    OpeningHours,
    PriceType,
    ProviderHealth,
    ProviderResult,
    Restaurant,
    RouteLeg,
    RoutePoint,
    SourceReference,
    SourceType,
    TravelMode,
    TripProfile,
)

NAMESPACE = UUID("6b6fc4c5-e52b-4fa9-88f2-cc752d63b5cc")


def stable_id(value: str) -> UUID:
    return uuid5(NAMESPACE, value)


def source_reference(
    provider: str, source_id: str, domain: DataDomain, ttl_minutes: int = 30
) -> SourceReference:
    now = datetime.now(timezone.utc)
    return SourceReference(
        source=provider,
        source_type=SourceType.FIXTURE,
        source_id=source_id,
        observed_at=now - timedelta(minutes=2),
        fetched_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
        request_context={"fixture": True},
        data_version="fixture-1",
        confidence=0.95,
        domain=domain,
    )


def location(address: str, lat: float, lng: float, district: str = "市中心") -> Location:
    return Location(
        address=address,
        city="成都",
        district=district,
        coordinates=Coordinates(latitude=lat, longitude=lng),
    )


def build_hotels(provider: str) -> tuple[list[Hotel], list[HotelOffer]]:
    hotel_a_id = stable_id("hotel:chengdu-skyline")
    hotel_b_id = stable_id("hotel:jinjiang-garden")
    hotel_a = Hotel(
        id=hotel_a_id,
        canonical_name="成都天际酒店",
        aliases=["成都天际酒店(春熙路店)" if provider == "tongcheng-mock" else "成都天际酒店"],
        location=location("春熙路东大街 88 号", 30.6574, 104.0817),
        rating=4.8,
        review_count=12480,
        brand="Skyline",
        source_references=[
            source_reference(provider, f"{provider}-hotel-a", DataDomain.COMMERCIAL_FACT)
        ],
    )
    hotel_b = Hotel(
        id=hotel_b_id,
        canonical_name="锦江花园酒店",
        aliases=["成都锦江花园"],
        location=location("红星路三段 18 号", 30.6512, 104.0871),
        rating=4.6,
        review_count=8930,
        brand="Jinjiang",
        source_references=[
            source_reference(provider, f"{provider}-hotel-b", DataDomain.COMMERCIAL_FACT)
        ],
    )
    check_in, check_out = date(2026, 10, 3), date(2026, 10, 7)
    offers: list[HotelOffer] = []
    for hotel, nightly, suffix in ((hotel_a, Decimal("428"), "a"), (hotel_b, Decimal("300"), "b")):
        offers.append(
            HotelOffer(
                id=stable_id(f"offer:{provider}:{suffix}"),
                hotel_id=hotel.id,
                room_type_id=stable_id(f"room:{hotel.id}:superior-king"),
                rate_plan_id=stable_id(f"rate:{hotel.id}:flex-breakfast"),
                check_in=check_in,
                check_out=check_out,
                guests=2,
                room_count=1,
                meal_plan="早餐",
                cancellation_policy="入住前 1 天可免费取消",
                payment_policy="到店支付",
                price=Money(value=nightly * 4, currency="CNY", price_type=PriceType.PER_STAY),
                tax=Money(value=Decimal("0"), currency="CNY", price_type=PriceType.PER_STAY),
                availability=7,
                source_reference=source_reference(
                    provider, f"{provider}-offer-{suffix}", DataDomain.COMMERCIAL_FACT, 5
                ),
            )
        )
    return [hotel_a, hotel_b], offers


class MockTravelProvider:
    def __init__(self, name: str, price_adjustment: Decimal = Decimal("0")) -> None:
        self.name = name
        self.price_adjustment = price_adjustment

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name, status="healthy", latency_ms=1.0, detail="fixture provider"
        )

    async def search_hotels(self, profile: TripProfile) -> ProviderResult:
        await asyncio.sleep(0)
        hotels, _ = build_hotels(self.name)
        return ProviderResult(
            provider=self.name,
            data=hotels,
            source_references=[ref for hotel in hotels for ref in hotel.source_references],
        )

    async def search_room_offers(
        self, hotel_ids: list[str], profile: TripProfile
    ) -> ProviderResult:
        await asyncio.sleep(0)
        _, offers = build_hotels(self.name)
        requested = {UUID(value) for value in hotel_ids}
        selected = []
        for offer in offers:
            if offer.hotel_id in requested:
                adjusted = offer.model_copy(
                    update={
                        "check_in": profile.start_date,
                        "check_out": profile.end_date,
                        "guests": profile.travelers,
                        "price": offer.price.model_copy(
                            update={"value": offer.price.value + self.price_adjustment}
                        ),
                    }
                )
                selected.append(adjusted)
        return ProviderResult(
            provider=self.name,
            data=selected,
            source_references=[offer.source_reference for offer in selected],
        )

    async def search_restaurants(self, profile: TripProfile) -> ProviderResult:
        await asyncio.sleep(0)
        refs = [
            source_reference(self.name, f"{self.name}-restaurant-{idx}", DataDomain.COMMERCIAL_FACT)
            for idx in range(2)
        ]
        restaurants = [
            Restaurant(
                id=stable_id("restaurant:shu-jiu-xiang"),
                canonical_name="蜀九香火锅(春熙路店)",
                aliases=["蜀九香火锅"],
                location=location("春熙路 66 号", 30.6561, 104.0832),
                rating=4.7,
                review_count=5200,
                average_cost=Money(
                    value=Decimal("108"),
                    currency="CNY",
                    price_type=PriceType.PER_PERSON,
                    source_reference_id=refs[0].source_id,
                ),
                cuisine=["火锅", "川菜"],
                source_references=[refs[0]],
            ),
            Restaurant(
                id=stable_id("restaurant:kuixinglou-snack"),
                canonical_name="魁星楼小吃集合",
                aliases=["魁星楼街小吃"],
                location=location("魁星楼街 12 号", 30.6698, 104.0604, "青羊区"),
                rating=4.6,
                review_count=3100,
                average_cost=Money(
                    value=Decimal("55"),
                    currency="CNY",
                    price_type=PriceType.PER_PERSON,
                    source_reference_id=refs[1].source_id,
                ),
                cuisine=["小吃", "川味"],
                source_references=[refs[1]],
            ),
        ]
        return ProviderResult(provider=self.name, data=restaurants, source_references=refs)

    async def search_attractions(self, profile: TripProfile) -> ProviderResult:
        await asyncio.sleep(0)
        ref = source_reference(self.name, f"{self.name}-attractions", DataDomain.GEO_FACT, 120)
        attractions = [
            Attraction(
                id=stable_id("attraction:taikooli"),
                canonical_name="太古里与春熙路",
                aliases=["春熙路太古里"],
                location=location("中纱帽街 8 号", 30.6558, 104.0805),
                rating=4.8,
                ticket_price=Money(
                    value=Decimal("0"),
                    currency="CNY",
                    price_type=PriceType.PER_PERSON,
                    source_reference_id=ref.source_id,
                ),
                recommended_duration_minutes=150,
                opening_hours=[
                    OpeningHours(weekday=day, opens_at=time(10), closes_at=time(22))
                    for day in range(7)
                ],
                source_references=[ref],
            ),
            Attraction(
                id=stable_id("attraction:wuhou"),
                canonical_name="武侯祠",
                aliases=["成都武侯祠博物馆"],
                location=location("武侯祠大街 231 号", 30.6495, 104.0432, "武侯区"),
                rating=4.7,
                ticket_price=Money(
                    value=Decimal("50"),
                    currency="CNY",
                    price_type=PriceType.PER_PERSON,
                    source_reference_id=ref.source_id,
                ),
                recommended_duration_minutes=120,
                opening_hours=[
                    OpeningHours(weekday=day, opens_at=time(9), closes_at=time(18))
                    for day in range(7)
                ],
                source_references=[ref],
            ),
        ]
        return ProviderResult(provider=self.name, data=attractions, source_references=[ref])

    async def calculate_routes(
        self, profile: TripProfile, points: list[RoutePoint]
    ) -> ProviderResult:
        await asyncio.sleep(0)
        ref = source_reference(self.name, f"{self.name}-routes", DataDomain.GEO_FACT, 30)
        route = RouteLeg(
            origin_id=stable_id("hotel:chengdu-skyline"),
            destination_id=stable_id("attraction:taikooli"),
            mode=TravelMode.WALK,
            distance_meters=320,
            duration_minutes=5,
            cost=Money(
                value=Decimal("0"),
                currency="CNY",
                price_type=PriceType.PER_PERSON,
                source_reference_id=ref.source_id,
            ),
            source_reference=ref,
        )
        return ProviderResult(provider=self.name, data=[route], source_references=[ref])
