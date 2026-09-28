"""Weather provider backed by Open-Meteo (free, keyless).

Open-Meteo is a genuine forecast source: no API key, no account, CC BY 4.0
attribution. It returns daily aggregates for up to 16 days ahead, which is also
its honest limit — a trip further out simply has no forecast yet, and the
provider says so instead of extrapolating.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from travel_api.domain.models import (
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
from travel_api.providers.free.geo import OPEN_METEO_ATTRIBUTION, GeoResolver
from travel_api.providers.free.http import FreeHttpClient

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

#: Open-Meteo publishes at most 16 forecast days.
MAX_FORECAST_DAYS = 16

#: The air-quality endpoint has a *shorter* horizon than the forecast: verified
#: live that ``forecast_days=7`` is accepted and ``8`` is rejected with
#: "Allowed range 0 to 7". Asking for 16 silently lost all air-quality data.
MAX_AIR_QUALITY_DAYS = 7

#: European AQI bands. These are the official EAQI break points, and the labels
#: below are their Chinese renderings — deliberately *not* the Chinese AQI
#: ("中度污染") wording, because the number consumed here is the European index
#: and mislabelling one scale with another's categories would mislead.
EAQI_GOOD = 20
EAQI_FAIR = 40
EAQI_MODERATE = 60
EAQI_POOR = 80

#: At or above this EAQI the air itself is a reason to prefer indoor
#: sightseeing. Set at "poor" rather than "moderate": moderate air is still fine
#: for ordinary sightseeing, and blocking those days would distort most trips.
EAQI_OUTDOOR_LIMIT = EAQI_MODERATE

#: UV index at which unprotected outdoor time becomes a real concern.
UV_HIGH = 8.0

#: WMO 4677 weather interpretation codes used by Open-Meteo.
WMO_CONDITIONS: dict[int, str] = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴大冰雹",
}

#: Codes that make outdoor sightseeing genuinely unpleasant.
SEVERE_CODES = frozenset({55, 57, 65, 66, 67, 75, 82, 86, 95, 96, 99})
WET_CODES = frozenset(
    {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
)


def describe_weather_code(code: int) -> str:
    return WMO_CONDITIONS.get(code, f"未知天气({code})")


def is_outdoor_friendly(
    code: int,
    precipitation_probability: int | None,
    *,
    air_quality_index: int | None = None,
    uv_index_max: float | None = None,
) -> bool:
    """Decide whether outdoor sightseeing is sensible.

    Air quality and UV are considered only when actually known. A missing value
    must not be treated as "clean air": that would let an unmeasurable day look
    like a good one, which is the same class of mistake as inventing a price.
    """
    if code in SEVERE_CODES:
        return False
    if code in WET_CODES and (precipitation_probability or 0) >= 60:
        return False
    if air_quality_index is not None and air_quality_index > EAQI_OUTDOOR_LIMIT:
        return False
    if uv_index_max is not None and uv_index_max >= UV_HIGH:
        return False
    return True


def describe_air_quality(index: int | None) -> str | None:
    """Chinese label for a European AQI value, or None when unknown."""
    if index is None:
        return None
    if index <= EAQI_GOOD:
        return "优"
    if index <= EAQI_FAIR:
        return "良"
    if index <= EAQI_MODERATE:
        return "中等"
    if index <= EAQI_POOR:
        return "较差"
    if index <= 100:
        return "很差"
    return "极差"


class OpenMeteoWeatherProvider:
    """Serves the weather capability from Open-Meteo."""

    def __init__(
        self,
        name: str = "open-meteo",
        *,
        http: FreeHttpClient,
        geo_resolver: GeoResolver,
        timeout_seconds: float = 20.0,
        ttl_seconds: int = 900,
        air_quality_enabled: bool = True,
    ) -> None:
        self.name = name
        self.http = http
        self.geo_resolver = geo_resolver
        self.timeout_seconds = timeout_seconds
        self.ttl_seconds = ttl_seconds
        self.air_quality_enabled = air_quality_enabled

    async def search_weather(self, profile: TripProfile) -> ProviderResult:
        place = await self.geo_resolver.resolve(profile.destination)

        payload = await self.http.get_json(
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": place.latitude,
                "longitude": place.longitude,
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,precipitation_probability_max"
                ),
                "timezone": "auto",
                "forecast_days": MAX_FORECAST_DAYS,
            },
        )

        now = datetime.now(timezone.utc)
        reference = SourceReference(
            source=self.name,
            source_type=SourceType.OPEN_DATA,
            source_id=f"open-meteo:{place.latitude:.4f},{place.longitude:.4f}",
            observed_at=now,
            observed_at_basis=ObservationBasis.SOURCE_SILENT,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            request_context={
                "destination": profile.destination,
                "model": "best_match",
                "timezone": (payload or {}).get("timezone"),
                "attribution": OPEN_METEO_ATTRIBUTION,
            },
            data_version="open-meteo-v1",
            confidence=0.8,
            domain=DataDomain.GEO_FACT,
        )

        days = self._parse_days(payload)
        covered = {day.day_date for day in days}

        references = [reference]
        # Air quality is a separate endpoint on the same provider and the same
        # licence. It is best-effort: this endpoint intermittently drops the
        # first TLS connection, and a missing AQI must not fail the forecast.
        if self.air_quality_enabled:
            air_days, air_reference = await self._fetch_air_quality(place, now)
            if air_reference is not None:
                references.append(air_reference)
            if air_days:
                days = _merge_air_quality(days, air_days)

        forecast = WeatherForecast(
            location_name=place.name,
            coordinates=place.coordinates,
            timezone=(payload or {}).get("timezone") or place.timezone,
            elevation_m=_as_optional_float((payload or {}).get("elevation")),
            days=days,
            source_references=references if days else [],
        )

        errors: list[str] = []
        if profile.start_date not in covered and profile.start_date > now.date() + timedelta(
            days=MAX_FORECAST_DAYS - 1
        ):
            errors.append(
                f"行程日期 {profile.start_date} 超出天气预报范围（未来 {MAX_FORECAST_DAYS} 天），"
                "暂无可用预报。"
            )

        return ProviderResult(
            provider=self.name,
            data=[forecast] if days else [],
            source_references=references if days else [],
            errors=errors,
        )

    async def _fetch_air_quality(
        self, place, now: datetime
    ) -> tuple[dict[date, dict], SourceReference | None]:
        """Fetch hourly air quality and reduce it to one value per day.

        The air-quality endpoint rejects every ``daily=`` parameter combination,
        so the reduction happens here. Peak values are used rather than averages:
        a day whose afternoon AQI spikes is a day to plan indoor activities,
        and an average would hide exactly that.

        Its horizon (7 days) is shorter than the forecast's (16) and the AQI
        series is shorter still, so days with no air-quality hours are simply
        left untouched — the forecast keeps its full range and the extra fields
        stay ``None`` rather than pretending the air is clean.
        """
        try:
            payload = await self.http.get_json(
                OPEN_METEO_AIR_QUALITY_URL,
                params={
                    "latitude": place.latitude,
                    "longitude": place.longitude,
                    "hourly": "pm2_5,pm10,european_aqi,uv_index",
                    "timezone": "auto",
                    "forecast_days": MAX_AIR_QUALITY_DAYS,
                },
            )
        except Exception as exc:  # noqa: BLE001 - air quality is optional context
            logger.warning("air quality unavailable for %r: %r", place.name, exc)
            return {}, None

        hourly = (payload or {}).get("hourly") or {}
        times: list = hourly.get("time") or []
        pm25: list = hourly.get("pm2_5") or []
        aqi: list = hourly.get("european_aqi") or []
        uv: list = hourly.get("uv_index") or []

        by_day: dict[date, dict] = {}
        for index, raw_time in enumerate(times):
            try:
                day = date.fromisoformat(str(raw_time)[:10])
            except ValueError:
                continue
            bucket = by_day.setdefault(day, {"pm2_5": [], "aqi": [], "uv": []})
            for key, series in (("pm2_5", pm25), ("aqi", aqi), ("uv", uv)):
                value = _at(series, index)
                if isinstance(value, (int, float)):
                    bucket[key].append(float(value))

        reduced = {
            day: {
                "pm2_5": max(values["pm2_5"]) if values["pm2_5"] else None,
                "aqi": max(values["aqi"]) if values["aqi"] else None,
                "uv": max(values["uv"]) if values["uv"] else None,
            }
            for day, values in by_day.items()
        }

        reference = SourceReference(
            source=self.name,
            source_type=SourceType.OPEN_DATA,
            source_id=f"open-meteo-air-quality:{place.latitude:.4f},{place.longitude:.4f}",
            observed_at=now,
            observed_at_basis=ObservationBasis.SOURCE_SILENT,
            fetched_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            request_context={
                "destination": place.name,
                "variables": "pm2_5,pm10,european_aqi,uv_index",
                "aggregation": "daily_max_of_hourly",
                "attribution": OPEN_METEO_ATTRIBUTION,
            },
            data_version="open-meteo-air-quality-v1",
            confidence=0.75,
            domain=DataDomain.GEO_FACT,
        )
        return reduced, reference

    @staticmethod
    def _parse_days(payload: dict) -> list[WeatherDay]:
        daily = (payload or {}).get("daily") or {}
        dates: list = daily.get("time") or []
        codes: list = daily.get("weather_code") or []
        maxima: list = daily.get("temperature_2m_max") or []
        minima: list = daily.get("temperature_2m_min") or []
        precipitation: list = daily.get("precipitation_sum") or []
        probabilities: list = daily.get("precipitation_probability_max") or []

        days: list[WeatherDay] = []
        for index, raw_date in enumerate(dates):
            code = _at(codes, index)
            if code is None:
                continue
            try:
                day_date = date.fromisoformat(str(raw_date))
                weather_code = int(code)
                temp_max = float(_at(maxima, index))
                temp_min = float(_at(minima, index))
            except (TypeError, ValueError):
                continue
            precipitation_mm = _as_float(_at(precipitation, index), default=0.0)
            probability_raw = _at(probabilities, index)
            probability = (
                int(probability_raw) if isinstance(probability_raw, (int, float)) else None
            )
            days.append(
                WeatherDay(
                    day_date=day_date,
                    weather_code=weather_code,
                    condition=describe_weather_code(weather_code),
                    temperature_max_c=temp_max,
                    temperature_min_c=temp_min,
                    precipitation_mm=max(0.0, precipitation_mm),
                    precipitation_probability=probability,
                    outdoor_friendly=is_outdoor_friendly(weather_code, probability),
                )
            )
        return days

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name, status="healthy", latency_ms=0.0, detail="Open-Meteo forecast"
        )


def _merge_air_quality(days: list[WeatherDay], air_by_day: dict[date, dict]) -> list[WeatherDay]:
    """Attach air-quality peaks to each day and re-evaluate outdoor suitability."""
    merged: list[WeatherDay] = []
    for day in days:
        values = air_by_day.get(day.day_date)
        if not values:
            merged.append(day)
            continue
        aqi_raw = values.get("aqi")
        uv_raw = values.get("uv")
        pm25_raw = values.get("pm2_5")
        aqi = int(round(aqi_raw)) if isinstance(aqi_raw, (int, float)) else None
        uv = float(uv_raw) if isinstance(uv_raw, (int, float)) else None
        pm25 = float(pm25_raw) if isinstance(pm25_raw, (int, float)) else None
        merged.append(
            day.model_copy(
                update={
                    "air_quality_index": aqi,
                    "pm2_5_ug_m3": pm25,
                    "uv_index_max": uv,
                    "outdoor_friendly": is_outdoor_friendly(
                        day.weather_code,
                        day.precipitation_probability,
                        air_quality_index=aqi,
                        uv_index_max=uv,
                    ),
                }
            )
        )
    return merged


def _at(values: list, index: int):
    return values[index] if index < len(values) else None


def _as_float(value, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
