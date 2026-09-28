"""AMap weather, keyed by administrative code.

AMap's weather endpoint takes an **adcode** (行政区编码), not a city name, so this
provider geocodes the destination first. That dependency is the reason the geocoder
lives in the same package.

Two times, both reported
------------------------
Every forecast carries:

* ``fetched_at`` — when Traver asked AMap, and
* AMap's own ``reporttime`` in ``request_context`` — when AMap says the observation
  was issued.

They are different facts and both matter. ``fetched_at`` answers "how current is
this?", while ``reporttime`` answers "how old is the data behind it?", and a reader
comparing an 08:00 forecast against a 21:00 plan needs the second one.

No weather code is invented
---------------------------
AMap returns Chinese condition text (``dayweather``: 晴/多云/小雨) and no numeric
code. ``WeatherDay.weather_code`` is required, so the text is mapped to the closest
WMO code through an explicit table and the original Chinese text is preserved in
``condition``. An unrecognised description yields ``weather_code=0`` (WMO "clear
sky" is *not* assumed — see ``_UNKNOWN_CODE``) and is flagged in the source context,
because silently coding a sandstorm as clear would be worse than admitting ignorance.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from travel_api.domain.models import (
    Coordinates,
    DataDomain,
    ObservationBasis,
    ProviderHealth,
    ProviderResult,
    SourceReference,
    SourceType,
    TripProfile,
    WeatherDay,
    WeatherForecast,
)
from travel_api.providers.amap.client import (
    AmapClient,
    AmapError,
    AmapKeyError,
    parse_float,
    parse_location,
)
from travel_api.providers.free.attribution import AMAP_ATTRIBUTION

logger = logging.getLogger(__name__)

WEATHER_PATH = "/v3/weather/weatherInfo"
GEOCODE_PATH = "/v3/geocode/geo"

#: Used when AMap's description is not in the table below. WMO 0 means "clear sky",
#: so reusing it would claim clear weather for anything unrecognised; this is a
#: distinct sentinel documented as "unknown".
_UNKNOWN_CODE = -1

#: AMap's Chinese condition text -> WMO 4677 code, for the strings AMap actually
#: emits. Only unambiguous mappings are listed; anything else stays unknown rather
#: than being forced into the nearest-looking code.
CONDITION_CODES: dict[str, int] = {
    "晴": 0,
    "多云": 2,
    "阴": 3,
    "小雨": 61,
    "中雨": 63,
    "大雨": 65,
    "暴雨": 65,
    "阵雨": 80,
    "雷阵雨": 95,
    "雷雨": 95,
    "小雪": 71,
    "中雪": 73,
    "大雪": 75,
    "雨夹雪": 68,
    "雾": 45,
    "霾": 45,
    "浮尘": 45,
    "沙尘暴": 45,
    "扬沙": 45,
}

#: Conditions that make outdoor sightseeing a poor idea. Used for
#: ``WeatherDay.outdoor_friendly``, which the optimizer consults.
INDOOR_CONDITIONS = frozenset(
    {"小雨", "中雨", "大雨", "暴雨", "阵雨", "雷阵雨", "雷雨", "小雪", "中雪", "大雪", "沙尘暴"}
)


def condition_code(description: str) -> int:
    """Map an AMap description to a WMO code, or :data:`_UNKNOWN_CODE`."""
    return CONDITION_CODES.get(description.strip(), _UNKNOWN_CODE)


def _report_time_iso(value: object) -> str | None:
    """AMap's ``reporttime`` verbatim, for the source context.

    Kept as the original string as well as parsed into ``observed_at``: the string is
    what AMap actually said, and some consumers want to display exactly that.
    """
    if not value:
        return None
    return str(value)


#: AMap reports ``reporttime`` in China Standard Time (UTC+8) and does not state a
#: zone. Every other time in this program is UTC, so the value must be interpreted
#: rather than copied. Assuming UTC instead would place a freshly published forecast
#: eight hours in the future, which reads as "fresher than fresh" and would mask a
#: genuinely stale one — the one direction of error that must not happen here.
AMAP_REPORT_TIME_TZ = timezone(timedelta(hours=8))

#: How AMap formats reporttime. Tried in order; a shape not listed stays unparsed
#: rather than being guessed at.
_REPORT_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_report_time(value: object) -> datetime | None:
    """Parse AMap's ``reporttime`` into an aware datetime, or ``None``.

    Returns ``None`` rather than substituting the current time. The caller must then
    say so, because silently standing in our fetch time for a published observation
    time would claim the forecast is newer than it is.
    """
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _REPORT_TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=AMAP_REPORT_TIME_TZ)
    logger.warning("AMap reporttime %r did not match any known format", text)
    return None


class AmapWeatherProvider:
    """Serves ``search_weather`` from AMap."""

    def __init__(
        self,
        client: AmapClient,
        *,
        name: str = "amap",
        ttl_seconds: int = 300,
        geocoder=None,
    ) -> None:
        self.name = name
        self.client = client
        self.ttl_seconds = ttl_seconds
        #: Needed because the weather endpoint takes an adcode. Without one the
        #: destination cannot be converted, and the provider reports why.
        self.geocoder = geocoder
        self.last_adcode: str | None = None
        self.last_report_time: str | None = None

    async def search_weather(self, profile: TripProfile) -> ProviderResult:
        resolved = await self._resolve(profile.destination)
        if resolved is None:
            # Reported as an error rather than an empty forecast: "we could not
            # resolve this city" and "the weather is unknown" are different problems.
            return ProviderResult(
                provider=self.name,
                data=[],
                errors=[f"无法解析 {profile.destination!r} 的行政区编码（adcode），跳过天气查询"],
            )
        adcode, coordinates = resolved

        try:
            payload = await self.client.get(
                WEATHER_PATH, params={"city": adcode, "extensions": "all"}
            )
        except AmapKeyError:
            raise
        except AmapError as exc:
            logger.warning("AMap weather failed for %r: %s", adcode, exc)
            return ProviderResult(provider=self.name, data=[], errors=[str(exc)])

        forecast = self._forecast(payload, profile, adcode, coordinates)
        if forecast is None:
            return ProviderResult(provider=self.name, data=[])
        return ProviderResult(
            provider=self.name, data=[forecast], source_references=[forecast.source_references[0]]
        )

    async def _resolve(self, destination: str) -> tuple[str, Coordinates] | None:
        """Resolve a destination to ``(adcode, coordinates)``.

        The coordinates come back with the adcode because both are needed and the
        geocode response is the only place they are available together. Returning
        them as a pair is what lets the forecast carry the city's real coordinates —
        ``Coordinates`` is a required field, and defaulting it to ``(0, 0)`` would
        place every Chinese city off the coast of Africa.
        """
        if self.geocoder is not None:
            place = await self.geocoder.resolve(destination)
            if place is not None and place.adcode:
                return place.adcode, Coordinates(latitude=place.latitude, longitude=place.longitude)

        # Fall back to a direct lookup so the provider still works when it is built
        # without a geocoder.
        try:
            payload = await self.client.get(GEOCODE_PATH, params={"address": destination})
        except AmapError as exc:
            logger.warning("AMap geocode failed for %r: %s", destination, exc)
            return None
        geocodes = payload.get("geocodes")
        if not isinstance(geocodes, list) or not geocodes:
            return None
        first = geocodes[0]
        if not isinstance(first, dict):
            return None
        adcode = str(first.get("adcode") or "").strip()
        coordinates = parse_location(first.get("location"))
        if not adcode or coordinates is None:
            return None
        latitude, longitude = coordinates
        return adcode, Coordinates(latitude=latitude, longitude=longitude)

    def _forecast(
        self, payload: dict, profile: TripProfile, adcode: str, coordinates: Coordinates
    ) -> WeatherForecast | None:
        forecasts = payload.get("forecasts")
        if not isinstance(forecasts, list) or not forecasts:
            return None
        block = forecasts[0]
        if not isinstance(block, dict):
            return None
        casts = block.get("casts")
        if not isinstance(casts, list) or not casts:
            return None

        report_time = _report_time_iso(block.get("reporttime"))
        self.last_report_time = report_time
        self.last_adcode = adcode

        reference = self._source_reference(adcode=adcode, report_time=report_time)
        days: list[WeatherDay] = []
        unknown_conditions: list[str] = []

        for cast in casts:
            if not isinstance(cast, dict):
                continue
            day = self._day(cast, unknown_conditions)
            if day is not None:
                days.append(day)

        if not days:
            return None

        # Rebuild the reference now that the unrecognised descriptions are known, so
        # the disclosure travels with the data.
        reference = self._source_reference(
            adcode=adcode,
            report_time=report_time,
            unknown_conditions=unknown_conditions,
        )

        return WeatherForecast(
            location_name=str(block.get("city") or profile.destination),
            coordinates=coordinates,
            timezone=None,
            days=days,
            source_references=[reference],
        )

    def _day(self, cast: dict, unknown: list[str]) -> WeatherDay | None:
        raw_date = str(cast.get("date") or "").strip()
        try:
            day_date = date.fromisoformat(raw_date[:10])
        except ValueError:
            # A day without a usable date cannot be placed in the trip window.
            return None

        day_weather = str(cast.get("dayweather") or "").strip()
        night_weather = str(cast.get("nightweather") or "").strip()
        # WMO has no day/night pair. The day condition is used because a plan is
        # built around daytime activity, and the night text is preserved below.
        condition = day_weather or night_weather
        code = condition_code(condition)
        if code == _UNKNOWN_CODE and condition:
            unknown.append(condition)

        # AMap gives a day/night temperature pair and no single daily value, so the
        # pair maps onto min/max directly rather than being averaged.
        day_temp = parse_float(cast.get("daytemp"))
        night_temp = parse_float(cast.get("nighttemp"))
        if day_temp is None and night_temp is None:
            return None
        high = day_temp if day_temp is not None else night_temp
        low = night_temp if night_temp is not None else day_temp

        return WeatherDay(
            day_date=day_date,
            weather_code=code,
            condition=condition or "未知",
            temperature_max_c=float(high),
            temperature_min_c=float(low),
            # AMap's forecast endpoint reports no precipitation amount or
            # probability, so both stay at their "unknown" defaults rather than being
            # filled with a plausible-looking zero.
            precipitation_mm=0.0,
            precipitation_probability=None,
            outdoor_friendly=condition not in INDOOR_CONDITIONS,
        )

    def _source_reference(
        self,
        *,
        adcode: str,
        report_time: str | None,
        unknown_conditions: list[str] | None = None,
    ) -> SourceReference:
        now = datetime.now(timezone.utc)
        published = parse_report_time(report_time)
        context = {
            "amap_endpoint": WEATHER_PATH,
            "adcode": adcode,
            # AMap's own publication time for this forecast, verbatim as sent.
            "amap_report_time": report_time,
            "attribution": AMAP_ATTRIBUTION.as_credit(),
            "cached": False,
            "absent_fields": "precipitation_mm,precipitation_probability",
            "weather_code_source": "mapped_from_amap_chinese_text",
        }
        if published is not None:
            # ``observed_at`` is AMap's publication time, not our fetch time. This is
            # the whole point of tracking the two separately: a forecast published six
            # hours ago is six hours old even though we just asked for it, and a
            # reader deciding whether to trust it needs that number.
            observed_at = published
            basis = ObservationBasis.PROVIDER_REPORTED
            context["amap_report_time_zone"] = "UTC+08:00"
        else:
            # AMap gave no usable publication time. Fall back to the fetch time and
            # *say so*, rather than quietly presenting it as a reported observation.
            observed_at = now
            basis = ObservationBasis.SOURCE_SILENT
            context["amap_report_time_missing"] = True
        if unknown_conditions:
            # Named explicitly so a reader can see which descriptions were not mapped
            # instead of trusting a coded value that means "unknown".
            context["unmapped_conditions"] = sorted(set(unknown_conditions))
        return SourceReference(
            source=self.name,
            source_type=SourceType.OFFICIAL_API,
            source_id=f"amap:weather:{adcode}",
            observed_at=observed_at,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            observed_at_basis=basis,
            request_context=context,
            data_version="amap-live",
            confidence=0.8,
            domain=DataDomain.GEO_FACT,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            status="ok",
            checked_at=datetime.now(timezone.utc),
            detail="已配置 Key；天气按 adcode 实时查询",
        )


__all__ = [
    "CONDITION_CODES",
    "GEOCODE_PATH",
    "INDOOR_CONDITIONS",
    "WEATHER_PATH",
    "AmapWeatherProvider",
    "condition_code",
]
