"""Where a place name becomes a coordinate — and where it is refused.

The feature is "the more you supply, the more detailed the plan", and the tempting way to
deliver it is to take the first geocoding answer and put it on the map. That is wrong in a
specific, silent way: 西湖 is in 杭州, in 惠州 and in 扬州, and a source asked for the bare
name returns whichever it ranks first. A wrong coordinate here is not an error message, it is
a stop in somebody's Tuesday — and every downstream consumer treats it as a measured fact.

So these tests are mostly about refusal. Three properties carry the weight:

* the nearest candidate wins, not the source's first — because the destination is the only
  disambiguator available and the source's ranking is about the *name*;
* a candidate too far away is reported as **在别处**, not dropped and not accepted — dropped
  looks like "this place does not exist", accepted puts a stop 900 km away into a day plan;
* "nobody was asked" is a different outcome from "we asked and found nothing" — a partial
  deployment produces the first constantly, and presenting it as the second is the exact
  fabrication this project keeps finding (the export's absent ``age_unknown_facts``, the
  unserved travel mode, the dropped audit).

The last test in the file is the reason ``place_matching`` exists as its own module at all:
two sources make this decision, and they must make the same one.
"""

from __future__ import annotations

from datetime import date

import pytest
from travel_api.domain.models import (
    Coordinates,
    PlaceResolutionStatus,
    SourceType,
    TripProfile,
)
from travel_api.providers.amap.geocode import AmapPlace
from travel_api.providers.amap.place_resolver import (
    AmapPlaceResolver,
    amap_confidence,
    amap_precision,
)
from travel_api.providers.free.geo import GeoPlace, GeoResolutionError
from travel_api.providers.free.places import (
    FreePlaceResolver,
    nominatim_confidence,
    nominatim_precision,
    open_meteo_precision,
)
from travel_api.providers.place_matching import (
    MAX_DISTANCE_FROM_DESTINATION_KM,
    PRECISION_AREA,
    PRECISION_SPECIFIC,
    PRECISION_VENUE,
    PlaceCandidate,
    any_within,
    choose_within,
    decide,
    nearest,
    not_attempted,
)

#: 杭州's West Lake. The destination most of these tests are about.
HANGZHOU = Coordinates(latitude=30.2741, longitude=120.1551)
#: 惠州's West Lake, ~950 km away — a real namesake of the same Chinese name.
HUIZHOU_WEST_LAKE = Coordinates(latitude=23.1115, longitude=114.4162)


def _profile(destination: str = "杭州") -> TripProfile:
    return TripProfile(
        destination=destination,
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
    )


# -- the shared decision ---------------------------------------------------


def test_the_nearest_candidate_wins_not_the_sources_first():
    """The property the whole feature rests on.

    A source's ordering is about the name's prominence; the question here is which place the
    *note* meant. 惠州's 西湖 is a legitimate answer to the query 西湖 and it is not the one
    this trip is about.
    """
    far = PlaceCandidate(
        source="nominatim",
        source_id="1",
        latitude=HUIZHOU_WEST_LAKE.latitude,
        longitude=HUIZHOU_WEST_LAKE.longitude,
        # Deliberately the source's own first choice, and deliberately the confident one:
        # neither should outweigh being in the wrong province.
        confidence=0.99,
        display_name="西湖",
    )
    near = PlaceCandidate(
        source="nominatim",
        source_id="2",
        latitude=30.2489,
        longitude=120.1400,
        confidence=0.6,
        display_name="西湖风景名胜区",
    )

    resolved = decide("西湖", [far, near], destination=HANGZHOU, source_type=SourceType.OPEN_DATA)

    assert resolved.status is PlaceResolutionStatus.LOCATED
    assert resolved.coordinates is not None
    assert resolved.coordinates.latitude == pytest.approx(30.2489)
    assert resolved.display_name == "西湖风景名胜区"
    assert (resolved.straight_line_from_centre_km or 0) < 10


def test_a_namesake_far_away_is_reported_rather_than_dropped():
    """**在别处**, with the distance, so the reader learns something true about the note.

    Dropping the candidate would be the tempting alternative and it is worse: the place would
    appear unlocatable, which is the one thing it is not. The note really does name a 西湖 —
    just not this trip's.
    """
    resolved = decide(
        "西湖",
        [
            PlaceCandidate(
                source="nominatim",
                source_id="1",
                latitude=HUIZHOU_WEST_LAKE.latitude,
                longitude=HUIZHOU_WEST_LAKE.longitude,
                display_name="西湖",
            )
        ],
        destination=HANGZHOU,
        source_type=SourceType.OPEN_DATA,
    )

    assert resolved.status is PlaceResolutionStatus.ELSEWHERE
    # No coordinate, because the plan must not be able to schedule it by accident.
    assert resolved.coordinates is None
    assert (resolved.straight_line_from_centre_km or 0) > MAX_DISTANCE_FROM_DESTINATION_KM
    assert "公里" in resolved.reason
    assert "很可能不是笔记所指的那个" in resolved.reason


def test_elsewhere_still_carries_a_citation():
    """A rejection is a result of a lookup, so the lookup is cited.

    Otherwise 「在别处」 is an assertion the reader cannot check — they cannot tell whether
    something was searched for or guessed at.
    """
    resolved = decide(
        "西湖",
        [
            PlaceCandidate(
                source="nominatim",
                source_id="way/12345",
                latitude=HUIZHOU_WEST_LAKE.latitude,
                longitude=HUIZHOU_WEST_LAKE.longitude,
            )
        ],
        destination=HANGZHOU,
        source_type=SourceType.OPEN_DATA,
    )

    reference = resolved.source_reference
    assert reference is not None
    assert reference.source == "nominatim"
    assert reference.source_id == "way/12345"
    # The lookup is when we asked; a gazetteer publishes no rebuild time.
    assert reference.observed_at_basis.value == "source_silent"
    assert reference.request_context["distance_from_destination_km"] == pytest.approx(
        resolved.straight_line_from_centre_km
    )
    assert reference.request_context["candidates"] == 1


def test_no_candidates_is_not_found_and_says_a_lookup_happened():
    resolved = decide("不存在的名字", [], destination=HANGZHOU, source_type=SourceType.OPEN_DATA)

    assert resolved.status is PlaceResolutionStatus.NOT_FOUND
    assert resolved.coordinates is None
    assert resolved.source_reference is None
    assert "已查询" in resolved.reason


def test_nobody_was_asked_is_a_different_outcome_from_found_nothing():
    """The distinction a partial deployment produces constantly.

    With no source that can resolve places, every mention would otherwise be reported as
    没有找到这个地方 — a claim about the place that nobody made. It is the same defect as
    treating a missing ``age_unknown_facts`` as an empty list: the absence of a lookup
    presented as the result of one.
    """
    from travel_api.providers.place_matching import decide as _decide

    attempted = _decide("西湖", [], destination=HANGZHOU, source_type=SourceType.OPEN_DATA)
    # `decide` is only reached when a source was consulted, so its empty case is NOT_FOUND.
    assert attempted.status is PlaceResolutionStatus.NOT_FOUND
    assert attempted.source_reference is None

    unasked = not_attempted(["西湖"], "当前没有配置可以提供地名定位的来源")
    assert [item.status for item in unasked] == [PlaceResolutionStatus.NOT_ATTEMPTED]
    assert unasked[0].reason == "当前没有配置可以提供地名定位的来源"
    # Explicitly not NOT_FOUND: nothing is claimed about the place.
    assert unasked[0].status is not PlaceResolutionStatus.NOT_FOUND


def test_not_attempted_uses_the_name_exactly_as_given():
    """So a reader comparing the two lists sees the same string, not two spellings of it."""
    resolved = not_attempted(["灵隐寺"], "没有来源")
    assert resolved[0].name == "灵隐寺"


def test_a_source_that_states_no_confidence_does_not_get_one_invented():
    """``None`` is a real state and must survive into the citation.

    Open-Meteo states no match confidence. Filling in 0.5 would produce a figure that looks
    measured; the alternative actually kept is to report the floor and let the reader see it.
    """
    resolved = decide(
        "某地",
        [
            PlaceCandidate(
                source="open-meteo-geocoding",
                source_id="1",
                latitude=30.25,
                longitude=120.15,
                confidence=None,
            )
        ],
        destination=HANGZHOU,
        source_type=SourceType.OPEN_DATA,
    )

    assert resolved.match_confidence is None
    # The citation still needs a float, so it carries the documented floor.
    assert resolved.source_reference is not None
    assert resolved.source_reference.confidence == 0.5


def test_a_stated_confidence_is_carried_through_unchanged():
    resolved = decide(
        "某地",
        [
            PlaceCandidate(
                source="amap",
                source_id="1",
                latitude=30.25,
                longitude=120.15,
                confidence=0.85,
            )
        ],
        destination=HANGZHOU,
        source_type=SourceType.OFFICIAL_API,
    )

    assert resolved.match_confidence == 0.85
    assert resolved.source_reference is not None
    assert resolved.source_reference.confidence == 0.85
    assert resolved.source_reference.source_type is SourceType.OFFICIAL_API


def test_the_choice_is_stable_when_two_candidates_are_equally_close():
    """A plan that changes without its inputs changing is not debuggable.

    Two identical coordinates would otherwise be ordered by whatever the source happened to
    return first, and the winner's ``display_name`` would differ between runs.
    """
    first = PlaceCandidate(
        source="b-source", source_id="9", latitude=30.25, longitude=120.15, display_name="B"
    )
    second = PlaceCandidate(
        source="a-source", source_id="1", latitude=30.25, longitude=120.15, display_name="A"
    )

    chosen = [nearest([first, second], HANGZHOU)[0].display_name for _ in range(5)]  # type: ignore[index]
    assert chosen == ["A"] * 5
    reversed_order = nearest([second, first], HANGZHOU)[0].display_name  # type: ignore[index]
    assert reversed_order == "A"


def test_any_within_answers_whether_a_fallback_source_is_still_needed():
    """The question a source that answered *somewhere else* has not answered."""
    assert any_within([], HANGZHOU, MAX_DISTANCE_FROM_DESTINATION_KM) is False
    assert (
        any_within(
            [
                PlaceCandidate(
                    source="s",
                    source_id="1",
                    latitude=HUIZHOU_WEST_LAKE.latitude,
                    longitude=HUIZHOU_WEST_LAKE.longitude,
                )
            ],
            HANGZHOU,
            MAX_DISTANCE_FROM_DESTINATION_KM,
        )
        is False
    )
    assert (
        any_within(
            [
                PlaceCandidate(source="s", source_id="1", latitude=30.25, longitude=120.15),
                PlaceCandidate(
                    source="s",
                    source_id="2",
                    latitude=HUIZHOU_WEST_LAKE.latitude,
                    longitude=HUIZHOU_WEST_LAKE.longitude,
                ),
            ],
            HANGZHOU,
            MAX_DISTANCE_FROM_DESTINATION_KM,
        )
        is True
    )


# -- what the extractor hands over, and why nothing here re-cuts it --------


def test_the_name_being_located_is_exactly_what_the_extractor_produced():
    """There is no tidying step between extraction and lookup, and this pins that.

    A trimmer here is the tempting addition and it was written once and removed, because its
    premise was false. ``_PLACE_PATTERN`` matches non-greedily up to the first place suffix, so
    「推荐去灵隐寺看看」 already arrives as 灵隐寺 — a downstream trimmer fixes nothing while
    introducing a second, disagreeing rule about where a place name ends.
    """
    from travel_api.domain.ingestion import FactKind, IngestionRequest
    from travel_api.services.ingestion import ContentExtractionEngine

    engine = ContentExtractionEngine()
    extracted = engine.extract(
        IngestionRequest(text="推荐去灵隐寺看看，很值得。"), ingestion_id="x"
    ).facts
    names = [fact.value.get("name") for fact in extracted if fact.kind is FactKind.PLACE_MENTION]
    assert names == ["灵隐寺"], "the extractor already stops at the suffix"


def test_the_known_extraction_limitation_is_a_truncation_not_an_overflow():
    """The real defect, pinned so it is not mistaken for the invented one.

    Suffixes that are themselves part of a longer name cut it short: 桥 inside 断桥残雪, 巷
    inside 宽窄巷子. This is the *opposite* direction from the over-long reading a trimmer would
    target, so a truncating heuristic downstream cannot help — it can only make it worse. The
    fix belongs in the extractor, and both manifests state the limitation until it is made.
    """
    from travel_api.domain.ingestion import FactKind, IngestionRequest
    from travel_api.services.ingestion import ContentExtractionEngine

    engine = ContentExtractionEngine()
    extracted = engine.extract(IngestionRequest(text="断桥残雪怎么去"), ingestion_id="x").facts
    names = [fact.value.get("name") for fact in extracted if fact.kind is FactKind.PLACE_MENTION]
    assert names == ["断桥"], (
        "if this changes, the extractor learned something and the caveat on the manifests "
        "should be revisited"
    )


# -- precision, which is not confidence -----------------------------------


def test_a_precise_landmark_beats_a_nearer_administrative_area():
    """The subtle half of the namesake problem, and the reason ``precision`` exists.

    西湖区's centroid sits nearer 杭州's centre than 西湖风景名胜区 does. A purely
    distance-ordered choice therefore answers「哪个西湖离市中心近」when the question was
    「笔记说的是哪个西湖」— and it answers it confidently, with a coordinate that looks fine.
    """
    district = PlaceCandidate(
        source="amap",
        source_id="d",
        # Closer to the city centre than the scenic area is.
        latitude=30.2700,
        longitude=120.1500,
        precision=PRECISION_AREA,
        confidence=0.85,
        display_name="西湖区",
    )
    scenic = PlaceCandidate(
        source="amap",
        source_id="s",
        latitude=30.2408,
        longitude=120.1010,
        precision=PRECISION_SPECIFIC,
        confidence=0.85,
        display_name="西湖风景名胜区",
    )

    # The premise of the test: the district really is nearer, so ordering by distance alone
    # would pick it.
    assert nearest([district, scenic], HANGZHOU)[0].display_name == "西湖区"  # type: ignore[index]

    chosen = choose_within([district, scenic], HANGZHOU, MAX_DISTANCE_FROM_DESTINATION_KM)
    assert chosen is not None
    assert chosen.display_name == "西湖风景名胜区"


def test_precision_filters_before_it_ranks():
    """A precise match in another province must not displace a usable nearby one.

    Ranking all candidates by precision and *then* checking the distance would do exactly that:
    the far precise match wins the ranking, fails the distance check, and the good nearby answer
    is reported as 在别处 even though it was in hand.
    """
    far_landmark = PlaceCandidate(
        source="nominatim",
        source_id="1",
        latitude=HUIZHOU_WEST_LAKE.latitude,
        longitude=HUIZHOU_WEST_LAKE.longitude,
        precision=PRECISION_SPECIFIC,
        display_name="西湖风景名胜区",
    )
    near_area = PlaceCandidate(
        source="amap",
        source_id="2",
        latitude=30.2500,
        longitude=120.1400,
        precision=PRECISION_AREA,
        display_name="西湖区",
    )

    resolved = decide(
        "西湖",
        [far_landmark, near_area],
        destination=HANGZHOU,
        source_type=SourceType.OPEN_DATA,
    )

    assert resolved.status is PlaceResolutionStatus.LOCATED
    assert resolved.display_name == "西湖区"
    assert (resolved.straight_line_from_centre_km or 0) < MAX_DISTANCE_FROM_DESTINATION_KM


def test_an_unstated_precision_sorts_last_rather_than_first():
    """``None`` is not a claim of precision, so it must not outrank a stated one."""
    unknown = PlaceCandidate(
        source="s", source_id="u", latitude=30.2490, longitude=120.1400, precision=None
    )
    known = PlaceCandidate(
        source="s",
        source_id="k",
        # Further away, so it wins only on precision.
        latitude=30.2400,
        longitude=120.1300,
        precision=PRECISION_VENUE,
    )

    chosen = choose_within([unknown, known], HANGZHOU, MAX_DISTANCE_FROM_DESTINATION_KM)
    assert chosen is not None
    assert chosen.source_id == "k"


def test_nominatims_precision_and_confidence_are_not_the_same_number():
    """A park and a café are equally *found* and not equally *specific*."""
    assert nominatim_precision("attraction") == PRECISION_SPECIFIC
    assert nominatim_precision("restaurant") == PRECISION_VENUE
    assert nominatim_precision("administrative") == PRECISION_AREA
    assert nominatim_precision("city") == PRECISION_AREA
    # An area type is not in the confidence sets, so no match confidence is claimed for it.
    assert nominatim_confidence("administrative") is None
    assert nominatim_precision("something_new") is None


def test_open_meteo_can_only_ever_be_an_area():
    """Its gazetteer holds populated places, not landmarks — so it cannot return the lake."""
    assert open_meteo_precision("PPLA") == PRECISION_AREA
    assert open_meteo_precision("PPLC") == PRECISION_AREA
    assert open_meteo_precision(None) is None
    assert open_meteo_precision("") is None


def test_amap_precision_separates_a_landmark_from_the_area_around_it():
    assert amap_precision("兴趣点") == PRECISION_SPECIFIC
    assert amap_precision("门牌号") == PRECISION_SPECIFIC
    assert amap_precision("风景名胜") == PRECISION_SPECIFIC
    assert amap_precision("区县") == PRECISION_AREA
    assert amap_precision("城市") == PRECISION_AREA
    assert amap_precision("某种新等级") is None
    assert amap_precision(None) is None


def test_amap_is_confident_about_an_area_even_though_it_is_coarse():
    """The divergence that made two fields necessary.

    AMap is not uncertain that 西湖区 is 西湖区. Reporting a low confidence for it would
    misdescribe what happened — the match is certain and it is the wrong kind of thing.
    """
    assert amap_confidence("区县") == 0.85
    assert amap_precision("区县") == PRECISION_AREA
    assert amap_confidence("兴趣点") == 0.85
    assert amap_precision("兴趣点") == PRECISION_SPECIFIC


# -- the free resolver, over a fake transport ------------------------------


class _FakeHttp:
    """Records the requests made and replays canned JSON, keyed by URL and query."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()

    async def get_json(self, url: str, params: dict | None = None):
        self.calls.append((url, dict(params or {})))
        if url in self.fail:
            raise RuntimeError(f"transport refused {url}")
        value = self.responses.get(url)
        if callable(value):
            return value(params or {})
        return value


OPEN_METEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


class _FakeGeoResolver:
    """Stands in for the destination geocoder the free stack shares."""

    def __init__(self, place: GeoPlace | None) -> None:
        self.place = place
        self.queries: list[str] = []

    async def resolve(self, destination: str) -> GeoPlace:
        self.queries.append(destination)
        if self.place is None:
            raise GeoResolutionError(f"无法解析目的地坐标: {destination}")
        return self.place


def _geo_place(destination: str = "杭州") -> GeoPlace:
    from datetime import datetime, timezone

    from travel_api.domain.models import (
        DataDomain,
        ObservationBasis,
        SourceReference,
        SourceType,
    )

    now = datetime.now(timezone.utc)
    return GeoPlace(
        name=destination,
        city=destination,
        country="中国",
        country_code="CN",
        latitude=HANGZHOU.latitude,
        longitude=HANGZHOU.longitude,
        timezone="Asia/Shanghai",
        bbox=(29.0, 119.0, 31.0, 121.0),
        source_reference=SourceReference(
            source="open-meteo-geocoding",
            source_type=SourceType.OPEN_DATA,
            source_id="1",
            observed_at=now,
            observed_at_basis=ObservationBasis.SOURCE_SILENT,
            fetched_at=now,
            domain=DataDomain.GEO_FACT,
        ),
    )


def _open_meteo_result(name: str, latitude: float, longitude: float, ident: int) -> dict:
    return {
        "results": [
            {
                "id": ident,
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
                "feature_code": "PPLA",
                "admin1": "某省",
                "country_code": "CN",
            }
        ]
    }


@pytest.mark.asyncio
async def test_the_free_resolver_accepts_a_nearby_answer_and_cites_it():
    http = _FakeHttp({OPEN_METEO_URL: _open_meteo_result("灵隐寺", 30.2408, 120.1010, 7)})
    resolver = FreePlaceResolver(http, geo_resolver=_FakeGeoResolver(_geo_place()))  # type: ignore[arg-type]

    result = await resolver.resolve_places(["灵隐寺"], _profile())

    assert result.status == "success"
    assert len(result.data) == 1
    resolved = result.data[0]
    assert resolved.status is PlaceResolutionStatus.LOCATED
    assert resolved.display_name == "灵隐寺"
    assert result.source_references == [resolved.source_reference]
    # The destination was geocoded through the shared resolver, not independently.
    assert resolver.geo_resolver.queries == ["杭州"]  # type: ignore[attr-defined]
    # Nominatim was never needed, because the first source answered inside the destination.
    assert resolver.last_sources == ["open-meteo-geocoding"]


@pytest.mark.asyncio
async def test_a_far_answer_from_the_first_source_triggers_the_scoped_second_source():
    """The case Open-Meteo cannot avoid and Nominatim can.

    Open-Meteo has no scoping parameter, so 人民公园 comes back as a park in some other city
    and *looks like success* — a non-empty answer that a naive implementation would accept.
    The second source is therefore asked not only when the first is empty but also when
    everything it returned is out of area.
    """
    http = _FakeHttp(
        {
            OPEN_METEO_URL: _open_meteo_result(
                "人民公园", HUIZHOU_WEST_LAKE.latitude, HUIZHOU_WEST_LAKE.longitude, 3
            ),
            NOMINATIM_URL: [
                {
                    "osm_id": 88,
                    "lat": "30.2460",
                    "lon": "120.1620",
                    "name": "人民公园",
                    "type": "park",
                }
            ],
        }
    )
    resolver = FreePlaceResolver(http, geo_resolver=_FakeGeoResolver(_geo_place()))  # type: ignore[arg-type]

    result = await resolver.resolve_places(["人民公园"], _profile())
    resolved = result.data[0]

    assert resolved.status is PlaceResolutionStatus.LOCATED
    assert resolved.source_reference is not None
    assert resolved.source_reference.source == "nominatim"
    assert resolver.last_sources == ["open-meteo-geocoding", "nominatim"]

    # And the scoping request is the part that matters: `bounded=1` makes Nominatim return
    # nothing rather than a prominent namesake, and the viewbox order is
    # west,north,east,south — getting it wrong silently finds nothing.
    nominatim_params = next(params for url, params in http.calls if url == NOMINATIM_URL)
    assert nominatim_params["bounded"] == 1
    left, top, right, bottom = (float(v) for v in nominatim_params["viewbox"].split(","))
    assert bottom < top
    assert left < right
    assert bottom < HANGZHOU.latitude < top
    assert left < HANGZHOU.longitude < right


@pytest.mark.asyncio
async def test_the_second_source_is_not_asked_when_the_first_answered_in_area():
    """One request, not two, whenever one is enough — these are rate-limited public services."""
    http = _FakeHttp({OPEN_METEO_URL: _open_meteo_result("西湖", 30.2489, 120.1400, 5)})
    resolver = FreePlaceResolver(http, geo_resolver=_FakeGeoResolver(_geo_place()))  # type: ignore[arg-type]

    await resolver.resolve_places(["西湖"], _profile())

    assert [url for url, _ in http.calls] == [OPEN_METEO_URL]


@pytest.mark.asyncio
async def test_an_ungovernable_trip_destination_yields_not_attempted_per_name():
    """No centre means no disambiguation, so no lookup is meaningful.

    Reported as 未尝试 rather than 未找到: the failure is the trip's centre, and blaming the
    place for it is a claim nobody made.
    """
    http = _FakeHttp({})
    resolver = FreePlaceResolver(http, geo_resolver=_FakeGeoResolver(None))  # type: ignore[arg-type]

    result = await resolver.resolve_places(["西湖", "灵隐寺"], _profile())

    assert result.status == "degraded"
    assert [item.status for item in result.data] == [
        PlaceResolutionStatus.NOT_ATTEMPTED,
        PlaceResolutionStatus.NOT_ATTEMPTED,
    ]
    assert all("无法确定目的地的位置" in item.reason for item in result.data)
    assert http.calls == [], "nothing should have been queried without a destination"
    assert result.errors and "杭州" in result.errors[0]


@pytest.mark.asyncio
async def test_every_name_gets_an_entry_even_when_one_lookup_fails():
    """The contract that makes the result usable: one entry per mention, in order.

    A caller matching results back to mentions cannot do that if the list is short — and the
    omissions would be exactly the failures, which are the ones worth reporting.
    """
    http = _FakeHttp(
        {
            OPEN_METEO_URL: lambda params: (
                _open_meteo_result("西湖", 30.25, 120.15, 1)
                if params.get("name") == "西湖"
                else None
            )
        }
    )
    http.fail.add(NOMINATIM_URL)
    resolver = FreePlaceResolver(http, geo_resolver=_FakeGeoResolver(_geo_place()))  # type: ignore[arg-type]

    result = await resolver.resolve_places(["西湖", "查不到的地方"], _profile())

    assert [item.name for item in result.data] == ["西湖", "查不到的地方"]
    assert [item.status for item in result.data] == [
        PlaceResolutionStatus.LOCATED,
        PlaceResolutionStatus.NOT_FOUND,
    ]


@pytest.mark.asyncio
async def test_an_empty_name_is_not_sent_to_a_geocoder():
    """「人均约 80 元」 is not a place, and asking a gazetteer where it is wastes a request."""
    http = _FakeHttp({})
    resolver = FreePlaceResolver(http, geo_resolver=_FakeGeoResolver(_geo_place()))  # type: ignore[arg-type]

    result = await resolver.resolve_places(["   "], _profile())

    assert result.data[0].status is PlaceResolutionStatus.NOT_ATTEMPTED
    assert http.calls == []


@pytest.mark.asyncio
async def test_an_empty_request_contacts_nothing():
    http = _FakeHttp({})
    resolver = FreePlaceResolver(http, geo_resolver=_FakeGeoResolver(_geo_place()))  # type: ignore[arg-type]

    result = await resolver.resolve_places([], _profile())

    assert result.data == []
    assert http.calls == []


def test_nominatim_confidence_bands_are_about_specificity():
    """A landmark is a real answer for a named place; a boundary is a different thing."""
    assert nominatim_confidence("attraction") == 0.75
    assert nominatim_confidence("restaurant") == 0.6
    # An unrecognised type is not evidence of a bad match, so nothing is claimed.
    assert nominatim_confidence("something_new") is None
    assert nominatim_confidence(None) is None
    assert nominatim_confidence(17) is None


# -- the 高德 resolver ------------------------------------------------------


class _FakeAmapClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path: str, params: dict | None = None):
        self.calls.append((path, dict(params or {})))
        value = self.responses.get(path)
        return value(params or {}) if callable(value) else value


def _amap_geocode(*entries: tuple[str, str, str, str]) -> dict:
    """A ``/v3/geocode/geo`` payload from ``(address, city, adcode, location)`` tuples."""
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": str(len(entries)),
        "geocodes": [
            {
                "formatted_address": address,
                "province": city,
                "city": city,
                "district": "",
                "adcode": adcode,
                "location": location,
                "level": "兴趣点",
            }
            for address, city, adcode, location in entries
        ],
    }


def _amap_resolver(client: _FakeAmapClient) -> AmapPlaceResolver:
    from travel_api.providers.amap.geocode import AmapGeocoder

    return AmapPlaceResolver(client, geocoder=AmapGeocoder(client))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_amap_scopes_the_lookup_to_the_destination_city():
    """高德's own scoping parameter, which the free sources do not have.

    人民公园 without a city is a nationwide search. The stub records the parameter so this
    cannot pass merely because the answer happened to be right.
    """

    def geocode(params: dict):
        if params["address"] == "杭州":
            return _amap_geocode(("浙江省杭州市", "杭州市", "330100", "120.155100,30.274100"))
        assert params.get("city") == "杭州市", params
        return _amap_geocode(("人民公园", "杭州市", "330106", "120.162000,30.246000"))

    client = _FakeAmapClient({"/v3/geocode/geo": geocode})
    resolver = _amap_resolver(client)

    result = await resolver.resolve_places(["人民公园"], _profile())

    assert result.data[0].status is PlaceResolutionStatus.LOCATED
    assert resolver.last_city == "杭州市"
    # The destination lookup carried no `city`; the name lookup did.
    name_lookup = next(p for _, p in client.calls if p.get("city"))
    assert name_lookup["city"] == "杭州市"


@pytest.mark.asyncio
async def test_amap_rejects_a_namesake_outside_the_destination():
    """A scoping parameter is a request, not a guarantee, so the distance check still runs.

    高德 can answer a scoped query with a match from elsewhere — the parameter orders results
    rather than filtering them in every case — and a trusted scoping parameter is how a stop
    ends up 950 km away.
    """

    def geocode(params: dict):
        if params["address"] == "杭州":
            return _amap_geocode(("浙江省杭州市", "杭州市", "330100", "120.155100,30.274100"))
        return _amap_geocode(("惠州西湖", "惠州市", "441300", "114.416200,23.111500"))

    client = _FakeAmapClient({"/v3/geocode/geo": geocode})
    resolver = _amap_resolver(client)

    result = await resolver.resolve_places(["西湖"], _profile())

    assert result.data[0].status is PlaceResolutionStatus.ELSEWHERE
    assert result.data[0].coordinates is None


@pytest.mark.asyncio
async def test_amap_without_a_destination_reports_not_attempted():
    client = _FakeAmapClient({"/v3/geocode/geo": {"status": "1", "geocodes": []}})
    resolver = _amap_resolver(client)

    result = await resolver.resolve_places(["西湖"], _profile())

    assert result.status == "degraded"
    assert result.data[0].status is PlaceResolutionStatus.NOT_ATTEMPTED
    assert "高德" in result.data[0].reason


@pytest.mark.asyncio
async def test_amap_reports_a_query_failure_for_that_name_only():
    from travel_api.providers.amap.client import AmapError

    def geocode(params: dict):
        if params["address"] == "杭州":
            return _amap_geocode(("浙江省杭州市", "杭州市", "330100", "120.155100,30.274100"))
        if params["address"] == "坏名字":
            raise AmapError("INVALID_USER_KEY")
        return _amap_geocode(("灵隐寺", "杭州市", "330106", "120.101000,30.240800"))

    client = _FakeAmapClient({"/v3/geocode/geo": geocode})
    resolver = _amap_resolver(client)

    result = await resolver.resolve_places(["坏名字", "灵隐寺"], _profile())

    assert [item.status for item in result.data] == [
        PlaceResolutionStatus.NOT_FOUND,
        PlaceResolutionStatus.LOCATED,
    ]
    assert "INVALID_USER_KEY" in result.data[0].reason
    assert result.status == "degraded"
    # The failure is reported, not swallowed by the fallback.
    assert result.errors and "INVALID_USER_KEY" in result.errors[0]


@pytest.mark.asyncio
async def test_amap_does_not_promote_a_district_match_over_the_landmark():
    """The ordering that is right for a destination is backwards for a named place.

    ``AmapGeocoder._rank`` prefers an administrative match, which is what stops 北京 resolving
    to a 北京烤鸭 shop. For a place named in a note it would do the opposite: 灵隐寺's own match
    is a 风景名胜, and preferring a same-named 区县 would replace the temple with the district
    around it.
    """

    def geocode(params: dict):
        if params["address"] == "杭州":
            return _amap_geocode(("浙江省杭州市", "杭州市", "330100", "120.155100,30.274100"))
        # 高德 orders these itself with the district first, as it does for destination queries.
        return {
            "status": "1",
            "info": "OK",
            "infocode": "10000",
            "count": "2",
            "geocodes": [
                {
                    "formatted_address": "灵隐街道",
                    "province": "浙江省",
                    "city": "杭州市",
                    "district": "西湖区",
                    "adcode": "330106",
                    "location": "120.120000,30.250000",
                    "level": "区县",
                },
                {
                    "formatted_address": "灵隐寺",
                    "province": "浙江省",
                    "city": "杭州市",
                    "district": "西湖区",
                    "adcode": "330106",
                    "location": "120.101000,30.240800",
                    "level": "兴趣点",
                },
            ],
        }

    client = _FakeAmapClient({"/v3/geocode/geo": geocode})
    resolver = _amap_resolver(client)

    result = await resolver.resolve_places(["灵隐寺"], _profile())

    assert result.data[0].status is PlaceResolutionStatus.LOCATED
    assert result.data[0].display_name == "灵隐寺"


def test_an_unknown_level_is_not_downgraded():
    """高德 adds levels, and one being new is not evidence the match is bad."""
    assert amap_confidence("兴趣点") == 0.85
    assert amap_confidence("门牌号") == 0.85
    assert amap_confidence("某种新等级") is None
    assert amap_confidence(None) is None
    # An unrecognised level gets no precision either, so it cannot outrank a stated one.
    assert amap_precision("某种新等级") is None


# -- the property that made this a shared module ---------------------------


@pytest.mark.asyncio
async def test_both_resolvers_make_the_same_decision_about_the_same_place():
    """Why ``place_matching`` is a module rather than a helper in each provider.

    Two sources can locate a place, and a deployment with a key uses a different one from a
    deployment without. If each decided for itself what counts as a match, the two would
    drift — and the drift would be invisible, because each would look correct in isolation
    while the same note produced a located place for one owner and a rejection for another.
    Nothing in either result would show the disagreement.

    So the same real namesake, far from the destination, is put through both, and both must
    reject it in the same way. The test is deliberately about the *outcome*, not the
    implementation: it would still pass if the shared module were replaced by two identical
    copies, and it would fail the moment one copy changed.
    """
    from travel_api.providers.amap.geocode import AmapGeocoder

    free_http = _FakeHttp(
        {
            OPEN_METEO_URL: _open_meteo_result(
                "西湖", HUIZHOU_WEST_LAKE.latitude, HUIZHOU_WEST_LAKE.longitude, 1
            ),
            NOMINATIM_URL: [],
        }
    )
    free = FreePlaceResolver(free_http, geo_resolver=_FakeGeoResolver(_geo_place()))  # type: ignore[arg-type]

    def geocode(params: dict):
        if params["address"] == "杭州":
            return _amap_geocode(("浙江省杭州市", "杭州市", "330100", "120.155100,30.274100"))
        return _amap_geocode(("惠州西湖", "惠州市", "441300", "114.416200,23.111500"))

    amap_client = _FakeAmapClient({"/v3/geocode/geo": geocode})
    amap = AmapPlaceResolver(amap_client, geocoder=AmapGeocoder(amap_client))  # type: ignore[arg-type]

    free_result = await free.resolve_places(["西湖"], _profile())
    amap_result = await amap.resolve_places(["西湖"], _profile())

    assert free_result.data[0].status is PlaceResolutionStatus.ELSEWHERE
    assert amap_result.data[0].status is PlaceResolutionStatus.ELSEWHERE
    for resolved in (free_result.data[0], amap_result.data[0]):
        assert resolved.coordinates is None
        assert (resolved.straight_line_from_centre_km or 0) > 900


def test_the_amap_place_type_is_importable_and_has_its_own_adapter():
    """A sentinel against the adapter being deleted while the geocoder's shape changes.

    ``AmapPlaceResolver`` reads ``AmapPlace`` fields directly; if the geocoder's result shape
    changed, this is the import that breaks first and says so.
    """
    place = AmapPlace(
        formatted_address="灵隐寺",
        province="浙江省",
        city="杭州市",
        district="西湖区",
        adcode="330106",
        latitude=30.2408,
        longitude=120.1010,
        level="兴趣点",
    )
    assert place.city_name == "杭州市"
    assert place.is_admin is False
