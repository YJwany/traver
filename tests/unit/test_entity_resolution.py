from travel_api.providers.mock import build_hotels
from travel_api.services.entity_resolution import deduplicate_hotels
from travel_api.services.normalization import normalize_hotels


def test_same_hotel_from_multiple_providers_is_merged():
    ctrip, _ = build_hotels("ctrip-mock")
    tongcheng, _ = build_hotels("tongcheng-mock")
    merged = deduplicate_hotels(normalize_hotels(ctrip + tongcheng))
    assert len(merged) == 2
    skyline = next(item for item in merged if "天际" in item.canonical_name)
    assert len(skyline.source_references) == 2
