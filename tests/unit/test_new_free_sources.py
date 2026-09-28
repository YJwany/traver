"""Offline tests for the three newly integrated free sources.

Covered here:

* Open-Meteo **air quality** (hourly -> daily reduction) and **elevation**.
* **Public holidays** from the MIT-licensed ``holidays`` library.
* **Wikipedia/Wikidata** attraction descriptions, including the negative cases
  that motivated the design: a name match that resolves to a disambiguation
  page, and a name match to an unrelated article, must both be refused.

The live counterparts are in tests/integration/test_free_providers_live.py.
"""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import unquote

import httpx
import pytest
from travel_api.domain.models import (
    Attraction,
    Coordinates,
    DataDomain,
    HolidayCalendar,
    Location,
    ProviderResult,
    PublicHoliday,
    SourceReference,
    SourceType,
    TripProfile,
    WeatherDay,
    WeatherForecast,
)
from travel_api.providers.free.geo import GeoResolver
from travel_api.providers.free.holidays_provider import HolidaysLibraryProvider
from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.free.open_meteo import (
    MAX_AIR_QUALITY_DAYS,
    OpenMeteoWeatherProvider,
    describe_air_quality,
    is_outdoor_friendly,
)
from travel_api.providers.free.wikipedia import (
    WikipediaContentProvider,
    normalize_wikipedia_tag,
)

NO_RATE_LIMIT = {
    "geocoding-api.open-meteo.com": 0.0,
    "nominatim.openstreetmap.org": 0.0,
    "api.open-meteo.com": 0.0,
    "air-quality-api.open-meteo.com": 0.0,
    "zh.wikipedia.org": 0.0,
    "www.wikidata.org": 0.0,
}
NO_RETRY = {"host_intervals": NO_RATE_LIMIT, "retry_count": 0}


def make_profile(**overrides) -> TripProfile:
    payload = {
        "destination": "成都",
        "start_date": date(2026, 10, 1),
        "end_date": date(2026, 10, 3),
        "travelers": 2,
    }
    payload.update(overrides)
    return TripProfile(**payload)


# -- recorded-shape payloads ------------------------------------------------

GEOCODE_PAYLOAD = {
    "results": [
        {
            "id": 1815286,
            "name": "成都",
            "latitude": 30.66667,
            "longitude": 104.06667,
            "feature_code": "PPLA",
            "country": "中国",
            "country_code": "CN",
            "population": 13568357,
            "timezone": "Asia/Shanghai",
        }
    ]
}

FORECAST_PAYLOAD = {
    "timezone": "Asia/Shanghai",
    # Elevation arrives with the forecast, so no separate request is needed.
    "elevation": 494.0,
    "daily": {
        "time": ["2026-10-01", "2026-10-02", "2026-10-03"],
        "weather_code": [0, 3, 1],
        "temperature_2m_max": [24.0, 22.0, 25.0],
        "temperature_2m_min": [16.0, 15.0, 17.0],
        "precipitation_sum": [0.0, 0.0, 0.0],
        "precipitation_probability_max": [5, 10, 0],
    },
}

#: The air-quality endpoint answers hourly only (every ``daily=`` combination is
#: rejected), and its AQI series is shorter than its time axis, so the reduction
#: must tolerate holes.
AIR_QUALITY_PAYLOAD = {
    "timezone": "Asia/Shanghai",
    "elevation": 494.0,
    "hourly": {
        "time": [
            "2026-10-01T00:00",
            "2026-10-01T12:00",
            "2026-10-01T23:00",
            "2026-10-02T06:00",
            "2026-10-02T18:00",
            "2026-10-03T09:00",
        ],
        "pm2_5": [12.0, 78.2, 40.0, 95.9, 30.0, None],
        "pm10": [20.0, 90.0, 55.0, 110.0, 44.0, None],
        # Day 3 has no AQI value at all, only PM2.5.
        "european_aqi": [30, 74, 52, 90, 61, None],
        "uv_index": [0.0, 3.7, 0.0, 1.0, 7.05, None],
    },
}


def _air_quality_handler(*, fail: bool = False, status: int = 500):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=GEOCODE_PAYLOAD)
        if host == "api.open-meteo.com":
            return httpx.Response(200, json=FORECAST_PAYLOAD)
        if host == "air-quality-api.open-meteo.com":
            if fail:
                return httpx.Response(status, text="upstream unavailable")
            return httpx.Response(200, json=AIR_QUALITY_PAYLOAD)
        return httpx.Response(404, text=f"unexpected host {host}")

    return handler


def build_weather_stack(*, air_quality_fail: bool = False, status: int = 500, **kwargs):
    http = FreeHttpClient(
        **NO_RETRY,
        transport=httpx.MockTransport(_air_quality_handler(fail=air_quality_fail, status=status)),
    )
    geo = GeoResolver(http)
    weather = OpenMeteoWeatherProvider(http=http, geo_resolver=geo, **kwargs)
    return http, geo, weather


# -- air quality -----------------------------------------------------------


async def test_air_quality_is_reduced_from_hourly_to_daily_peaks():
    """The daily value must be the peak, not the mean.

    An afternoon AQI spike is exactly the thing that should move a plan indoors;
    averaging it away would hide it.
    """
    http, _geo, weather = build_weather_stack()
    result = await weather.search_weather(make_profile())
    forecast: WeatherForecast = result.data[0]
    by_date = {day.day_date: day for day in forecast.days}

    day_one = by_date[date(2026, 10, 1)]
    # Max of [30, 74, 52] and [12.0, 78.2, 40.0]; UV max of [0.0, 3.7, 0.0].
    assert day_one.air_quality_index == 74
    assert day_one.pm2_5_ug_m3 == pytest.approx(78.2)
    assert day_one.uv_index_max == pytest.approx(3.7)

    day_two = by_date[date(2026, 10, 2)]
    assert day_two.air_quality_index == 90
    assert day_two.pm2_5_ug_m3 == pytest.approx(95.9)
    assert day_two.uv_index_max == pytest.approx(7.05)

    await http.aclose()


async def test_air_quality_requests_at_most_the_supported_horizon():
    """16 days is rejected outright by the live endpoint, losing all AQI data.

    Verified live: ``forecast_days=8`` returns HTTP 400
    "Allowed range 0 to 7", so the request must stay within that.
    """
    seen: list[int] = []
    base = _air_quality_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "air-quality-api.open-meteo.com":
            seen.append(int(request.url.params.get("forecast_days", "0")))
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    geo = GeoResolver(http)
    weather = OpenMeteoWeatherProvider(http=http, geo_resolver=geo)
    await weather.search_weather(make_profile())

    assert seen == [MAX_AIR_QUALITY_DAYS]
    assert MAX_AIR_QUALITY_DAYS <= 7
    await http.aclose()


async def test_a_day_without_air_quality_data_keeps_the_fields_empty():
    """Missing AQI must stay None rather than defaulting to a "clean air" value."""
    http, _geo, weather = build_weather_stack()
    result = await weather.search_weather(make_profile())
    forecast: WeatherForecast = result.data[0]
    day_three = next(day for day in forecast.days if day.day_date == date(2026, 10, 3))

    assert day_three.air_quality_index is None
    assert day_three.pm2_5_ug_m3 is None
    assert day_three.uv_index_max is None
    # With no air data the verdict falls back to the weather alone (code 1 = 晴间多云).
    assert day_three.outdoor_friendly is True
    await http.aclose()


async def test_air_quality_failure_does_not_lose_the_forecast():
    """Air quality is optional context; its failure must not fail the forecast."""
    http, _geo, weather = build_weather_stack(air_quality_fail=True)
    result = await weather.search_weather(make_profile())
    forecast: WeatherForecast = result.data[0]

    assert len(forecast.days) == 3
    assert all(day.air_quality_index is None for day in forecast.days)
    assert all(day.outdoor_friendly for day in forecast.days)
    await http.aclose()


async def test_air_quality_can_be_disabled():
    called: list[str] = []
    base = _air_quality_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "air-quality-api.open-meteo.com":
            called.append(request.url.host)
        return base(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    geo = GeoResolver(http)
    weather = OpenMeteoWeatherProvider(http=http, geo_resolver=geo, air_quality_enabled=False)
    result = await weather.search_weather(make_profile())

    assert called == []
    assert all(day.air_quality_index is None for day in result.data[0].days)
    await http.aclose()


async def test_air_quality_adds_its_own_source_reference():
    http, _geo, weather = build_weather_stack()
    forecast: WeatherForecast = (await weather.search_weather(make_profile())).data[0]
    source_ids = [reference.source_id for reference in forecast.source_references]

    assert any("air-quality" in source_id for source_id in source_ids)
    air_reference = next(
        reference
        for reference in forecast.source_references
        if "air-quality" in reference.source_id
    )
    assert air_reference.request_context["aggregation"] == "daily_max_of_hourly"
    assert air_reference.domain is DataDomain.GEO_FACT
    await http.aclose()


# -- elevation -------------------------------------------------------------


async def test_elevation_comes_from_the_forecast_payload():
    http, _geo, weather = build_weather_stack()
    forecast: WeatherForecast = (await weather.search_weather(make_profile())).data[0]
    assert forecast.elevation_m == pytest.approx(494.0)
    await http.aclose()


async def test_missing_elevation_stays_none():
    payload = json.loads(json.dumps(FORECAST_PAYLOAD))
    payload.pop("elevation")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=GEOCODE_PAYLOAD)
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=AIR_QUALITY_PAYLOAD)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    weather = OpenMeteoWeatherProvider(http=http, geo_resolver=GeoResolver(http))
    forecast: WeatherForecast = (await weather.search_weather(make_profile())).data[0]
    assert forecast.elevation_m is None
    await http.aclose()


# -- outdoor suitability ---------------------------------------------------


def test_outdoor_suitability_ignores_unknown_air_quality():
    """Absent data must not be read as a reason to stay indoors, nor as clean air."""
    assert is_outdoor_friendly(0, 0) is True
    assert is_outdoor_friendly(0, 0, air_quality_index=None, uv_index_max=None) is True


def test_outdoor_suitability_reacts_to_poor_air_quality():
    # 61 is just past "moderate" (60), i.e. the "poor" band on the EAQI scale.
    assert is_outdoor_friendly(0, 0, air_quality_index=60) is True
    assert is_outdoor_friendly(0, 0, air_quality_index=61) is False
    assert is_outdoor_friendly(0, 0, air_quality_index=150) is False


def test_outdoor_suitability_reacts_to_high_uv():
    assert is_outdoor_friendly(0, 0, uv_index_max=7.99) is True
    assert is_outdoor_friendly(0, 0, uv_index_max=8.0) is False
    assert is_outdoor_friendly(0, 0, uv_index_max=11.0) is False


def test_weather_still_overrides_air_quality():
    """Severe weather stays disqualifying even when the air is clean."""
    assert is_outdoor_friendly(95, 0, air_quality_index=10, uv_index_max=1.0) is False
    assert is_outdoor_friendly(65, 90, air_quality_index=10, uv_index_max=1.0) is False


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (None, None),
        (0, "优"),
        (20, "优"),
        (21, "良"),
        (40, "良"),
        (41, "中等"),
        (60, "中等"),
        (61, "较差"),
        (80, "较差"),
        (81, "很差"),
        (100, "很差"),
        (101, "极差"),
    ],
)
def test_air_quality_labels_follow_the_european_scale(index, expected):
    """The number consumed is the European AQI, so the bands must be EAQI's."""
    assert describe_air_quality(index) == expected


# -- country code ----------------------------------------------------------


async def test_geocoder_normalises_the_country_code_to_upper_case():
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(_air_quality_handler()))
    place = await GeoResolver(http).resolve("成都")
    assert place.country_code == "CN"
    await http.aclose()


async def test_geocoder_accepts_a_lower_case_country_code():
    """Nominatim returns ``cn`` while Open-Meteo returns ``CN``."""
    payload = json.loads(json.dumps(GEOCODE_PAYLOAD))
    payload["results"][0]["country_code"] = "cn"

    http = FreeHttpClient(
        **NO_RETRY,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    place = await GeoResolver(http).resolve("成都")
    assert place.country_code == "CN"
    await http.aclose()


async def test_geocoder_drops_a_malformed_country_code():
    """A bad code must not silently address the wrong country's holidays."""
    payload = json.loads(json.dumps(GEOCODE_PAYLOAD))
    payload["results"][0]["country_code"] = "CHINA"

    http = FreeHttpClient(
        **NO_RETRY,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    place = await GeoResolver(http).resolve("成都")
    assert place.country_code is None
    await http.aclose()


# -- holidays --------------------------------------------------------------


def holiday_provider(*, language: str = "zh_CN", country_code: str | None = None):
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(_air_quality_handler()))
    return HolidaysLibraryProvider(
        geo_resolver=GeoResolver(http),
        language=language,
        country_code=country_code,
    )


async def test_holidays_over_national_day_include_the_bridge_days():
    """The 调休 rest days are what make a holiday period a planning constraint.

    A source that lists only 10-01 would understate a week-long closure.
    """
    provider = holiday_provider()
    profile = make_profile(start_date=date(2026, 9, 30), end_date=date(2026, 10, 8))
    result = await provider.search_holidays(profile)

    assert result.errors == []
    calendar: HolidayCalendar = result.data[0]
    assert calendar.country_code == "CN"
    assert calendar.years == [2026]
    dates = calendar.holiday_dates

    # The core National Day dates and the adjusted rest days around them.
    assert date(2026, 10, 1) in dates
    assert date(2026, 10, 2) in dates
    assert date(2026, 10, 3) in dates
    # Outside the period.
    assert date(2026, 10, 8) not in dates

    national_day = calendar.on(date(2026, 10, 1))
    assert national_day is not None
    assert "国庆" in national_day.name
    assert national_day.country_code == "CN"


async def test_holidays_include_qingming_which_the_rejected_api_omitted():
    provider = holiday_provider()
    profile = make_profile(start_date=date(2026, 4, 1), end_date=date(2026, 4, 10))
    calendar: HolidayCalendar = (await provider.search_holidays(profile)).data[0]

    names = " ".join(holiday.name for holiday in calendar.holidays)
    assert "清明" in names


async def test_holidays_are_limited_to_the_trip_window():
    provider = holiday_provider()
    profile = make_profile(start_date=date(2026, 1, 1), end_date=date(2026, 1, 3))
    calendar: HolidayCalendar = (await provider.search_holidays(profile)).data[0]

    assert calendar.holidays, "New Year's Day should fall inside the window"
    assert all(
        profile.start_date <= holiday.holiday_date <= profile.end_date
        for holiday in calendar.holidays
    )
    assert date(2026, 2, 17) not in calendar.holiday_dates


async def test_a_trip_crossing_new_year_collects_both_years():
    """A single ``year`` field would silently drop one side of the trip."""
    provider = holiday_provider()
    profile = make_profile(start_date=date(2026, 12, 30), end_date=date(2027, 1, 4))
    calendar: HolidayCalendar = (await provider.search_holidays(profile)).data[0]

    assert calendar.years == [2026, 2027]
    assert date(2027, 1, 1) in calendar.holiday_dates


async def test_a_window_without_holidays_is_success_not_an_error():
    provider = holiday_provider()
    profile = make_profile(start_date=date(2026, 3, 10), end_date=date(2026, 3, 14))
    result = await provider.search_holidays(profile)

    assert result.errors == []
    calendar: HolidayCalendar = result.data[0]
    assert calendar.holidays == []


async def test_holidays_use_the_configured_country_without_geocoding():
    """An explicit country must skip geocoding entirely."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, json=GEOCODE_PAYLOAD)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    provider = HolidaysLibraryProvider(geo_resolver=GeoResolver(http), country_code="de")
    profile = make_profile(start_date=date(2026, 12, 24), end_date=date(2026, 12, 26))
    result = await provider.search_holidays(profile)

    assert calls == []
    calendar: HolidayCalendar = result.data[0]
    assert calendar.country_code == "DE"
    # Christmas is a nationwide German public holiday.
    assert date(2026, 12, 25) in calendar.holiday_dates
    await http.aclose()


async def test_an_unsupported_country_is_reported_not_raised():
    provider = holiday_provider(country_code="ZZ")
    result = await provider.search_holidays(make_profile())

    assert result.data == []
    assert result.errors
    assert "ZZ" in result.errors[0]


async def test_holidays_report_an_error_when_geocoding_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    provider = HolidaysLibraryProvider(geo_resolver=GeoResolver(http))
    result = await provider.search_holidays(make_profile())

    assert result.data == []
    assert result.errors
    await http.aclose()


async def test_holidays_carry_a_source_reference_and_domain():
    provider = holiday_provider()
    calendar: HolidayCalendar = (await provider.search_holidays(make_profile())).data[0]

    assert calendar.source_references
    reference = calendar.source_references[0]
    assert reference.domain is DataDomain.GEO_FACT
    assert reference.source_type is SourceType.OPEN_DATA
    assert reference.request_context["library"] == "vacanza/holidays"
    assert calendar.holidays[0].source_reference is not None


async def test_holiday_calendar_query_helpers():
    holiday = PublicHoliday(holiday_date=date(2026, 10, 1), name="国庆节")
    calendar = HolidayCalendar(country_code="CN", years=[2026], holidays=[holiday])

    assert calendar.on(date(2026, 10, 1)) is holiday
    assert calendar.on(date(2026, 10, 2)) is None
    assert calendar.between(date(2026, 9, 1), date(2026, 10, 2)) == [holiday]
    assert calendar.between(date(2026, 11, 1), date(2026, 12, 1)) == []


# -- wikipedia tag parsing -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("zh:王建墓", ("zh", "王建墓")),
        ("zh:成都武侯祠", ("zh", "成都武侯祠")),
        ("en:Jinsha (archaeological site)", ("en", "Jinsha (archaeological site)")),
        ("王建墓", (None, "王建墓")),
        ("", (None, None)),
        ("   ", (None, None)),
    ],
)
def test_wikipedia_tag_parsing(raw, expected):
    assert normalize_wikipedia_tag(raw) == expected


# -- wikipedia enrichment --------------------------------------------------


def reference_with_context(**context) -> SourceReference:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return SourceReference(
        source="openstreetmap",
        source_type=SourceType.OPEN_DATA,
        source_id="osm:node/1",
        observed_at=now,
        fetched_at=now,
        request_context=context,
        domain=DataDomain.GEO_FACT,
    )


def attraction(name: str, **context) -> Attraction:
    return Attraction(
        canonical_name=name,
        location=Location(
            address="addr",
            city="成都",
            coordinates=Coordinates(latitude=30.66, longitude=104.06),
        ),
        source_references=[reference_with_context(**context)],
    )


WIKI_PAYLOAD = {
    "query": {
        "pages": {
            "111": {
                "pageid": 111,
                "title": "王建墓",
                "extract": "前蜀永陵，即俗称的王建墓，位于四川省成都市。",
                "fullurl": "https://zh.wikipedia.org/wiki/%E7%8E%8B%E5%BB%BA%E5%A2%93",
            },
            "112": {
                "pageid": 112,
                "title": "杜甫草堂博物馆",
                "extract": "杜甫草堂，又名杜甫草堂博物馆，位于四川省成都市青羊区。",
                "fullurl": "https://zh.wikipedia.org/wiki/%E6%9D%9C%E7%94%AB%E8%8D%89%E5%A0%82",
            },
            "222": {
                "pageid": 222,
                "title": "人民公园",
                "extract": "人民公園是中華人民共和國建国之后带有其特色命名的一类公园設施。",
                "fullurl": "https://zh.wikipedia.org/wiki/%E4%BA%BA%E6%B0%91%E5%85%AC%E5%9B%AD",
                "pageprops": {"disambiguation": ""},
            },
            "333": {
                "pageid": 333,
                "title": "寬窄巷子",
                "extract": "宽窄巷子是中国四川省成都市的一个历史文化区。",
                "fullurl": "https://zh.wikipedia.org/wiki/%E5%AF%AC%E7%AA%84%E5%B7%B7%E5%AD%90",
            },
            "444": {"pageid": 444, "title": "望江楼公园", "missing": ""},
        },
        "redirects": [{"from": "杜甫草堂", "to": "杜甫草堂博物馆"}],
    }
}

WIKIDATA_PAYLOAD = {
    "entities": {
        "Q4390891": {"id": "Q4390891", "sitelinks": {"zhwiki": {"title": "杜甫草堂"}}},
        "Q9999999": {"id": "Q9999999", "sitelinks": {}},
    }
}


def wiki_handler(*, seen_titles: list | None = None, seen_qids: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "zh.wikipedia.org":
            titles = request.url.params.get("titles", "")
            if seen_titles is not None:
                seen_titles.append(titles)
            # Return only the pages that were actually requested.
            payload = json.loads(json.dumps(WIKI_PAYLOAD))
            requested = set(titles.split("|"))
            payload["query"]["pages"] = {
                key: page
                for key, page in payload["query"]["pages"].items()
                if page["title"] in requested
                or any(r["to"] == page["title"] for r in payload["query"]["redirects"])
            }
            return httpx.Response(200, json=payload)
        if host == "www.wikidata.org":
            ids = request.url.params.get("ids", "")
            if seen_qids is not None:
                seen_qids.append(ids)
            payload = json.loads(json.dumps(WIKIDATA_PAYLOAD))
            payload["entities"] = {
                key: value for key, value in payload["entities"].items() if key in ids.split("|")
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(404, text=f"unexpected host {host}")

    return handler


def wiki_provider(**kwargs):
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(wiki_handler(**kwargs)))
    return http, WikipediaContentProvider(http=http)


async def test_wikipedia_enriches_only_explicitly_linked_attractions():
    """An attraction with no OSM link must never get a description.

    This is the core safety property: 火烈鸟 and 人民公园 have no explicit link
    and, when a name match was attempted during research, resolved to 紅鸛科 and
    a disambiguation page respectively.
    """
    http, provider = wiki_provider()
    linked = attraction("成都永陵博物馆", wikipedia="zh:王建墓", wikidata="Q1540709")
    unlinked = attraction("火烈鸟")
    ambiguous = attraction("人民公园")

    result = await provider.enrich_attractions([linked, unlinked, ambiguous], make_profile())
    enriched = {item.canonical_name: item for item in result.data}

    assert enriched["成都永陵博物馆"].description is not None
    # The URL is percent-encoded, so compare the decoded path.
    assert "王建墓" in unquote(enriched["成都永陵博物馆"].wikipedia_url)
    assert enriched["火烈鸟"].description is None
    assert enriched["人民公园"].description is None
    await http.aclose()


async def test_wikipedia_refuses_a_disambiguation_page():
    """A linked title that turns out to be a disambiguation page yields nothing."""
    http, provider = wiki_provider()
    item = attraction("人民公园", wikipedia="zh:人民公园")
    result = await provider.enrich_attractions([item], make_profile())

    assert result.data[0].description is None
    await http.aclose()


async def test_wikipedia_leaves_missing_pages_empty():
    http, provider = wiki_provider()
    item = attraction("望江楼公园", wikipedia="zh:望江楼公园")
    result = await provider.enrich_attractions([item], make_profile())

    assert result.data[0].description is None
    await http.aclose()


async def test_wikidata_only_links_resolve_through_sitelinks():
    """OSM sometimes carries only a QID; the article must still be found."""
    http, provider = wiki_provider()
    item = attraction("杜甫草堂博物馆", wikidata="Q4390891")
    result = await provider.enrich_attractions([item], make_profile())

    assert result.data[0].description is not None
    assert result.data[0].wikipedia_url is not None
    await http.aclose()


async def test_wikidata_without_a_zhwiki_sitelink_yields_nothing():
    http, provider = wiki_provider()
    item = attraction("某处", wikidata="Q9999999")
    result = await provider.enrich_attractions([item], make_profile())

    assert result.data[0].description is None
    await http.aclose()


async def test_wikipedia_descriptions_are_attributed_to_a_content_domain():
    """CC BY-SA requires attribution, so the reference must be traceable."""
    http, provider = wiki_provider()
    item = attraction("成都永陵博物馆", wikipedia="zh:王建墓")
    result = await provider.enrich_attractions([item], make_profile())
    enriched = result.data[0]

    added = [r for r in enriched.source_references if r.source == "wikipedia"]
    assert added, "the description must carry its own source reference"
    assert added[0].domain is DataDomain.CONTENT_REFERENCE
    # request_context is persisted as a JSON column, so the credit must be a
    # plain string. Storing the Attribution object here failed at insert time
    # with "Object of type Attribution is not JSON serializable" — a bug that
    # only the persistence test could catch.
    credit = added[0].request_context["attribution"]
    assert isinstance(credit, str)
    assert "CC BY-SA 4.0" in credit
    assert result.source_references
    await http.aclose()


async def test_wikipedia_records_which_link_was_followed():
    http, provider = wiki_provider()
    item = attraction("成都永陵博物馆", wikidata="Q1540709")
    result = await provider.enrich_attractions([item], make_profile())
    context = result.data[0].source_references[0].request_context

    assert context.get("wikidata") == "Q1540709"
    await http.aclose()


async def test_a_non_zh_link_is_skipped_rather_than_fetched():
    """Only the zh wiki is queried, so an en link must not trigger a request."""
    seen: list[str] = []
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(wiki_handler(seen_titles=seen)))
    provider = WikipediaContentProvider(http=http)
    item = attraction("Jinsha", wikipedia="en:Jinsha (archaeological site)")
    result = await provider.enrich_attractions([item], make_profile())

    assert seen == []
    assert result.data[0].description is None
    await http.aclose()


async def test_wikipedia_batches_titles_into_one_request():
    seen: list[str] = []
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(wiki_handler(seen_titles=seen)))
    provider = WikipediaContentProvider(http=http)
    items = [
        attraction("成都永陵博物馆", wikipedia="zh:王建墓"),
        attraction("宽窄巷子", wikipedia="zh:寬窄巷子"),
        attraction("望江楼公园", wikipedia="zh:望江楼公园"),
    ]
    await provider.enrich_attractions(items, make_profile())

    assert len(seen) == 1, f"expected a single batched request, got {seen}"
    assert "王建墓" in seen[0] and "寬窄巷子" in seen[0]
    await http.aclose()


async def test_wikipedia_does_nothing_when_no_attraction_is_linked():
    """No explicit link means no work at all: no request, no echoed payload.

    Returning an empty set rather than the unchanged attractions also keeps the
    provider audit from duplicating all 50-odd entities for nothing.
    """
    seen: list[str] = []
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(wiki_handler(seen_titles=seen)))
    provider = WikipediaContentProvider(http=http)
    result = await provider.enrich_attractions([attraction("某景点")], make_profile())

    assert seen == []
    assert result.data == []
    assert result.errors == []
    await http.aclose()


async def test_wikipedia_can_be_disabled():
    seen: list[str] = []
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(wiki_handler(seen_titles=seen)))
    provider = WikipediaContentProvider(http=http, enabled=False)
    item = attraction("成都永陵博物馆", wikipedia="zh:王建墓")
    result = await provider.enrich_attractions([item], make_profile())

    assert seen == []
    assert result.data == []
    await http.aclose()


async def test_wikipedia_failure_is_reported_without_losing_attractions():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream down")

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    provider = WikipediaContentProvider(http=http)
    item = attraction("成都永陵博物馆", wikipedia="zh:王建墓")
    result = await provider.enrich_attractions([item], make_profile())

    assert result.errors
    assert result.data[0].canonical_name == "成都永陵博物馆"
    assert result.data[0].description is None
    await http.aclose()


async def test_wikipedia_truncates_very_long_extracts():
    long_text = "成都" * 800

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {"pages": {"1": {"pageid": 1, "title": "王建墓", "extract": long_text}}}
            },
        )

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handler))
    provider = WikipediaContentProvider(http=http)
    item = attraction("成都永陵博物馆", wikipedia="zh:王建墓")
    result = await provider.enrich_attractions([item], make_profile())

    description = result.data[0].description
    assert description is not None
    assert len(description) <= 600
    assert description.endswith("…")
    await http.aclose()


async def test_wikipedia_health_reports_enabled_state():
    http, provider = wiki_provider()
    health = await provider.health()
    assert health.provider == "wikipedia"
    assert health.status == "healthy"

    disabled = WikipediaContentProvider(http=http, enabled=False)
    assert (await disabled.health()).status == "disabled"
    await http.aclose()


async def test_holidays_health_reports_the_local_library():
    health = await holiday_provider().health()
    assert health.provider == "holidays"
    assert health.status == "healthy"


# -- capability routing ----------------------------------------------------


def test_new_capabilities_are_registered_for_routing():
    """Without a registry entry an unknown method is broadcast to every provider."""
    from travel_api.providers.base import (
        CAPABILITY_BY_METHOD,
        ContentProvider,
        HolidayProvider,
    )
    from travel_api.providers.mock import MockTravelProvider

    assert CAPABILITY_BY_METHOD["search_holidays"] is HolidayProvider
    assert CAPABILITY_BY_METHOD["enrich_attractions"] is ContentProvider

    mock = MockTravelProvider("mock")
    # Mocks must not be forced to implement content enrichment.
    assert not isinstance(mock, ContentProvider)
    assert not isinstance(mock, HolidayProvider)


def test_new_providers_declare_their_capabilities():
    from travel_api.providers.base import ContentProvider, HolidayProvider

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(wiki_handler()))
    wikipedia = WikipediaContentProvider(http=http)
    assert isinstance(wikipedia, ContentProvider)
    assert isinstance(holiday_provider(), HolidayProvider)


# -- registry dispatch -----------------------------------------------------


async def test_registry_routes_enrichment_only_to_the_content_provider():
    from travel_api.providers.registry import ProviderRegistry

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(wiki_handler()))
    wikipedia = WikipediaContentProvider(http=http)
    holiday = holiday_provider(country_code="CN")
    registry = ProviderRegistry([holiday, wikipedia])

    assert registry.providers_for("enrich_attractions") == [wikipedia]
    assert registry.providers_for("search_holidays") == [holiday]

    item = attraction("成都永陵博物馆", wikipedia="zh:王建墓")
    result = await registry.call(wikipedia, "enrich_attractions", [item], make_profile())
    assert isinstance(result, ProviderResult)
    assert result.data[0].description is not None
    await http.aclose()


def test_weather_day_air_quality_fields_are_bounded():
    """The model must reject impossible values rather than storing them."""
    with pytest.raises(ValueError):
        WeatherDay(
            day_date=date(2026, 10, 1),
            weather_code=0,
            condition="晴",
            temperature_max_c=20.0,
            temperature_min_c=10.0,
            air_quality_index=-1,
        )
    with pytest.raises(ValueError):
        WeatherDay(
            day_date=date(2026, 10, 1),
            weather_code=0,
            condition="晴",
            temperature_max_c=20.0,
            temperature_min_c=10.0,
            uv_index_max=-0.5,
        )


# -- persistence safety ----------------------------------------------------


async def test_every_free_source_reference_is_json_serializable():
    """request_context is stored in a JSON column, so it must survive json.dumps.

    This is the regression guard for a real bug: the new providers put the
    ``Attribution`` dataclass into ``request_context["attribution"]``, which
    raised "Object of type Attribution is not JSON serializable" only at INSERT
    time. Aggregation-level tests passed, because they never serialise.
    """
    import json

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(_air_quality_handler()))
    geo = GeoResolver(http)
    weather = OpenMeteoWeatherProvider(http=http, geo_resolver=geo)
    holidays = HolidaysLibraryProvider(geo_resolver=geo, country_code="CN")
    wiki_http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(wiki_handler()))
    wikipedia = WikipediaContentProvider(http=wiki_http)

    profile = make_profile()
    references = []
    references.extend((await weather.search_weather(profile)).source_references)
    references.extend((await holidays.search_holidays(profile)).source_references)
    references.extend(
        (
            await wikipedia.enrich_attractions(
                [attraction("成都永陵博物馆", wikipedia="zh:王建墓")], profile
            )
        ).source_references
    )

    assert references, "expected references to check"
    for reference in references:
        # Must not raise.
        payload = json.dumps(reference.request_context, ensure_ascii=False)
        assert payload
        credit = reference.request_context.get("attribution")
        if credit is not None:
            assert isinstance(credit, str), (
                f"{reference.source} stored {type(credit).__name__} as the "
                "attribution credit; request_context is a JSON column"
            )

    await http.aclose()
    await wiki_http.aclose()


def test_attribution_credit_is_a_plain_string():
    from travel_api.providers.free.attribution import OPEN_DATA_ATTRIBUTIONS

    for item in OPEN_DATA_ATTRIBUTIONS:
        credit = item.as_credit()
        assert isinstance(credit, str)
        assert item.text in credit
        assert item.licence in credit

    # The two new sources must be part of the mandatory attribution set.
    providers = {item.provider for item in OPEN_DATA_ATTRIBUTIONS}
    assert {"wikipedia", "holidays"} <= providers
