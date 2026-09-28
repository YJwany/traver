from __future__ import annotations

from math import atan2, cos, radians, sin, sqrt

from travel_api.domain.models import Attraction, Hotel, Restaurant
from travel_api.services.normalization import normalize_name


def geo_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    d_lat, d_lon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * radius * atan2(sqrt(a), sqrt(1 - a))


def _same_entity(
    name_a: str, name_b: str, lat_a: float, lon_a: float, lat_b: float, lon_b: float
) -> bool:
    normalized_a, normalized_b = normalize_name(name_a), normalize_name(name_b)
    name_match = (
        normalized_a == normalized_b or normalized_a in normalized_b or normalized_b in normalized_a
    )
    return name_match and geo_distance_meters(lat_a, lon_a, lat_b, lon_b) <= 500


def deduplicate_hotels(hotels: list[Hotel]) -> list[Hotel]:
    canonical: list[Hotel] = []
    for item in hotels:
        existing = next(
            (
                entity
                for entity in canonical
                if _same_entity(
                    item.canonical_name,
                    entity.canonical_name,
                    item.location.coordinates.latitude,
                    item.location.coordinates.longitude,
                    entity.location.coordinates.latitude,
                    entity.location.coordinates.longitude,
                )
            ),
            None,
        )
        if existing is None:
            canonical.append(item)
            continue
        existing.aliases = sorted(set(existing.aliases + [item.canonical_name] + item.aliases))
        existing.source_references.extend(item.source_references)
        if (item.review_count or 0) > (existing.review_count or 0):
            existing.review_count = item.review_count
    return canonical


def deduplicate_restaurants(restaurants: list[Restaurant]) -> list[Restaurant]:
    canonical: list[Restaurant] = []
    for item in restaurants:
        existing = next(
            (
                entity
                for entity in canonical
                if _same_entity(
                    item.canonical_name,
                    entity.canonical_name,
                    item.location.coordinates.latitude,
                    item.location.coordinates.longitude,
                    entity.location.coordinates.latitude,
                    entity.location.coordinates.longitude,
                )
            ),
            None,
        )
        if existing is None:
            canonical.append(item)
            continue
        existing.aliases = sorted(set(existing.aliases + [item.canonical_name] + item.aliases))
        existing.source_references.extend(item.source_references)
    return canonical


def deduplicate_attractions(attractions: list[Attraction]) -> list[Attraction]:
    """Merge attractions that describe the same place.

    Without this, de-duplicating by id alone keeps only the last provider's copy
    and silently drops every other source's reference, which destroys
    cross-source corroboration.
    """
    canonical: list[Attraction] = []
    for item in attractions:
        existing = next(
            (
                entity
                for entity in canonical
                if _same_entity(
                    item.canonical_name,
                    entity.canonical_name,
                    item.location.coordinates.latitude,
                    item.location.coordinates.longitude,
                    entity.location.coordinates.latitude,
                    entity.location.coordinates.longitude,
                )
            ),
            None,
        )
        if existing is None:
            canonical.append(item)
            continue
        existing.aliases = sorted(set(existing.aliases + [item.canonical_name] + item.aliases))
        existing.source_references.extend(item.source_references)
        # Prefer the richer record when one source knows more than another.
        if existing.ticket_price is None and item.ticket_price is not None:
            existing.ticket_price = item.ticket_price
        if not existing.opening_hours and item.opening_hours:
            existing.opening_hours = item.opening_hours
        if existing.rating is None and item.rating is not None:
            existing.rating = item.rating
    return canonical
