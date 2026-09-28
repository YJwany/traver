import re
import unicodedata

from travel_api.domain.models import Attraction, Hotel, Restaurant


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"[()（）\[\]【】·,，。\s]", "", value)
    for suffix in ("店", "酒店", "大酒店", "餐厅"):
        if value.endswith(suffix) and len(value) > len(suffix) + 2:
            value = value[: -len(suffix)]
    return value


def _normalize_common(entity):
    return entity.model_copy(
        update={
            "canonical_name": entity.canonical_name.strip(),
            "aliases": sorted({alias.strip() for alias in entity.aliases if alias.strip()}),
        }
    )


def normalize_hotels(hotels: list[Hotel]) -> list[Hotel]:
    return [_normalize_common(hotel) for hotel in hotels]


def normalize_restaurants(restaurants: list[Restaurant]) -> list[Restaurant]:
    return [_normalize_common(restaurant) for restaurant in restaurants]


def normalize_attractions(attractions: list[Attraction]) -> list[Attraction]:
    return [_normalize_common(attraction) for attraction in attractions]
