from __future__ import annotations

from dataclasses import dataclass

from travel_api.domain.models import (
    CandidateDecision,
    Hotel,
    HotelOffer,
    ScoreBreakdown,
    TripProfile,
)
from travel_api.services.entity_resolution import geo_distance_meters


@dataclass(frozen=True)
class DecisionWeights:
    price: float = 0.25
    location: float = 0.20
    transit: float = 0.20
    rating: float = 0.10
    itinerary_fit: float = 0.15
    user_preference: float = 0.10


def score_hotel_offers(
    hotels: list[Hotel],
    offers: list[HotelOffer],
    profile: TripProfile,
    weights: DecisionWeights | None = None,
) -> list[CandidateDecision]:
    weights = weights or DecisionWeights()
    hotel_by_id = {hotel.id: hotel for hotel in hotels}
    valid_offers = []
    for offer in offers:
        if offer.hotel_id not in hotel_by_id:
            continue
        if profile.hotel_budget is not None and offer.total_price > profile.hotel_budget:
            continue
        if offer.check_in != profile.start_date or offer.check_out != profile.end_date:
            continue
        if offer.availability is not None and offer.availability <= 0:
            continue
        if offer.source_reference.is_stale:
            continue
        valid_offers.append(offer)
    if not valid_offers:
        return []
    max_price = max(float(offer.total_price) for offer in valid_offers) or 1.0
    decisions: list[CandidateDecision] = []
    for offer in valid_offers:
        hotel = hotel_by_id[offer.hotel_id]
        matched = ["hotel budget", "date range", "availability", "fresh offer"]
        price_score = max(0.0, 1.0 - float(offer.total_price) / max_price)
        rating_score = (hotel.rating or 0) / 5
        transit_distance = geo_distance_meters(
            hotel.location.coordinates.latitude,
            hotel.location.coordinates.longitude,
            30.6574,
            104.0817,
        )
        transit_score = max(0.0, 1.0 - min(transit_distance, 3000) / 3000)
        preference_text = " ".join(profile.hotel_preferences + profile.preferences).lower()
        preference_score = 1.0 if "地铁" in preference_text or "交通" in preference_text else 0.6
        location_score = 0.9 if hotel.location.district in {"市中心", "锦江区"} else 0.6
        itinerary_fit = 0.9 if hotel.location.city == profile.destination else 0.2
        breakdown = ScoreBreakdown(
            price=price_score,
            location=location_score,
            transit=transit_score,
            rating=rating_score,
            itinerary_fit=itinerary_fit,
            user_preference=preference_score,
        )
        score = (
            price_score * weights.price
            + location_score * weights.location
            + transit_score * weights.transit
            + rating_score * weights.rating
            + itinerary_fit * weights.itinerary_fit
            + preference_score * weights.user_preference
        )
        decisions.append(
            CandidateDecision(
                candidate_id=offer.id,
                score=round(score, 6),
                score_breakdown=breakdown,
                matched_constraints=matched,
                failed_constraints=[],
                explanations=[
                    f"总价 ¥{offer.total_price}，{offer.nights} 晚",
                    f"评分 {hotel.rating or '暂无'}，评论数 {hotel.review_count or '暂无'}",
                    f"距中心行程点约 {round(transit_distance)} 米",
                ],
            )
        )
    return sorted(decisions, key=lambda item: item.score, reverse=True)
