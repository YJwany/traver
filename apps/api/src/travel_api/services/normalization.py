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


#: Suffixes a Chinese place name may carry that another source's name for the same place may
#: not. Two providers describe a district at different granularities — Overpass fills
#: ``district`` from OSM's ``suburb`` (「西湖街道」) while 高德 fills it from ``businessarea``
#: (「西湖」) — so equality between them is a coincidence rather than a comparison. Ordered
#: longest-first so 自治州 is not first truncated to 州.
_ADMINISTRATIVE_SUFFIXES = (
    "哈萨克自治州",
    "自治州",
    "自治县",
    "自治区",
    "特别行政区",
    "地区",
    "街道",
    "市",
    "县",
    "区",
    "盟",
    "省",
    "镇",
    "乡",
)

#: The five autonomous regions, whose full name interposes the ethnicity between the place and
#: 自治区: 新疆维吾尔自治区 is 新疆, not 新疆维吾尔. A table rather than a pattern because
#: there are exactly five of them and a rule that stripped any `X族自治区` would be guessing
#: at a grammar. Written out, it is checkable.
_AUTONOMOUS_REGION_SHORT_NAMES = {
    "新疆维吾尔自治区": "新疆",
    "广西壮族自治区": "广西",
    "宁夏回族自治区": "宁夏",
    "内蒙古自治区": "内蒙古",
    "西藏自治区": "西藏",
}


def normalize_place_name(value: str | None) -> str | None:
    """A place name reduced to what two sources can be expected to agree on.

    The problem this exists for: the traveller types ``丽江``, Nominatim reports
    ``address.city == "丽江市"``, and 高德 is queried with whatever the traveller typed. So the
    *same* city is spelled two ways depending on which provider answered, and any code that
    compares the two by equality silently gets "different" for one provider and "same" for the
    other.

    That is not a cosmetic difference. It means a scoring dimension changes value when a key is
    added — the plan is not the same plan, and nothing says so.

    Returns ``None`` for a value that is absent or carries no name, so callers can distinguish
    "a different place" from "no name to compare". Collapsing those two is the mistake this
    codebase keeps finding elsewhere.
    """
    if value is None:
        return None
    # NFKC folds full-width forms; casefold handles Latin names, of which there are many.
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized:
        return None

    for full, short in _AUTONOMOUS_REGION_SHORT_NAMES.items():
        if normalized == full.casefold():
            return short.casefold()

    for suffix in _ADMINISTRATIVE_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            normalized = normalized[: -len(suffix)]
            break

    # A string that *is* a suffix — "市", "区" — carries no name. Returning it unchanged would
    # make two of them compare equal and read as a match, and would make "市" look like a place
    # called 市 rather than like missing data.
    if not normalized or normalized in _ADMINISTRATIVE_SUFFIXES:
        return None
    return normalized


def same_place_name(left: str | None, right: str | None) -> bool | None:
    """Whether two place names denote the same place, or ``None`` if that cannot be told.

    Three-valued for the same reason ``Location.district`` comparisons are: an absent name is
    not a *different* name. A caller that needs a number must decide what "unknown" means
    rather than being handed a confident False.
    """
    left_key, right_key = normalize_place_name(left), normalize_place_name(right)
    if left_key is None or right_key is None:
        return None
    return left_key == right_key


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
