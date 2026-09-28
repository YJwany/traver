"""Public-holiday provider backed by the MIT-licensed `holidays` library.

Why a local library rather than a holiday web API
-------------------------------------------------
The obvious free endpoint (``date.nager.at``) is *not* unconditionally free:
its Terms of Service state "The Web API can be used for private or non-profit
projects. For commercial purposes we require active sponsorship." A paid or
sponsorship-gated source is exactly what this project excludes, so it cannot be
the default.

The ``holidays`` package (vacanza/holidays) is MIT licensed, needs no API key,
no network and no rate limit, and — verified against the live API on 2026 CN
data — is also *more complete*. For 2026 it reports 元旦, 春节, 清明节, 劳动节,
端午节, 中秋节 and 国庆节 together with the surrounding 调休 rest days
(e.g. 2026-10-02..10-07, 春节 2026-02-16..02-23). Nager.Date reported only the
six single festival dates: it omitted 清明节 entirely and every bridge day, so
it would have understated closures and crowding.

Honest limits
-------------
* Holiday *periods* are known, but how busy a specific site becomes is not, so
  no crowd estimate is derived here.
* For countries whose holidays are subdivision-specific (DE, US, ...) only the
  nationwide calendar is used, because the trip profile carries no subdivision.
  That under-reports rather than inventing a regional holiday.
* A holiday means "many businesses and museums may close"; it does not prove a
  given attraction is shut, so this never overrides an attraction's own facts.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from travel_api.domain.models import (
    DataDomain,
    HolidayCalendar,
    ProviderHealth,
    ProviderResult,
    PublicHoliday,
    SourceReference,
    SourceType,
    TripProfile,
)
from travel_api.providers.free.attribution import HOLIDAYS_ATTRIBUTION
from travel_api.providers.free.geo import GeoResolver

logger = logging.getLogger(__name__)

#: Two-letter code this data is published under.
HOLIDAYS_SOURCE = "holidays"

#: Language used for holiday names. zh_CN keeps the wording a traveller in
#: China actually sees (元旦, 春节, 国庆节) instead of a translation.
DEFAULT_HOLIDAY_LANGUAGE = "zh_CN"


def _library():
    """Import lazily so a missing dependency degrades instead of crashing.

    Holiday data is valuable but not essential to planning a trip; an import
    failure must not take down the whole preview.
    """
    try:
        import holidays as holidays_library
    except Exception:  # noqa: BLE001 - any import problem is "unavailable"
        return None
    return holidays_library


class HolidaysLibraryProvider:
    """Serves the holiday capability from the bundled `holidays` database."""

    def __init__(
        self,
        name: str = HOLIDAYS_SOURCE,
        *,
        geo_resolver: GeoResolver,
        language: str = DEFAULT_HOLIDAY_LANGUAGE,
        ttl_seconds: int = 86400,
        country_code: str | None = None,
    ) -> None:
        self.name = name
        self.geo_resolver = geo_resolver
        self.language = language
        self.ttl_seconds = ttl_seconds
        #: Optional override, mainly for tests and for callers that already know
        #: the country. When None the destination is geocoded.
        self.country_code = (country_code or "").upper() or None

    async def search_holidays(self, profile: TripProfile) -> ProviderResult:
        library = _library()
        if library is None:
            return ProviderResult(
                provider=self.name,
                data=[],
                errors=["holidays 依赖不可用，已跳过公共节假日。"],
            )

        country_code = self.country_code
        if country_code is None:
            # Geocoding is the only way to learn the country from a free-text
            # destination such as "成都". A failure here means no holiday data,
            # not a failed trip.
            try:
                place = await self.geo_resolver.resolve(profile.destination)
            except Exception as exc:  # noqa: BLE001
                logger.warning("holiday lookup skipped, geocoding failed: %r", exc)
                return ProviderResult(
                    provider=self.name,
                    data=[],
                    errors=[f"无法解析目的地国家，已跳过公共节假日：{type(exc).__name__}"],
                )
            country_code = place.country_code

        if not country_code:
            return ProviderResult(
                provider=self.name,
                data=[],
                errors=["目的地国家未知，已跳过公共节假日。"],
            )

        years = list(range(profile.start_date.year, profile.end_date.year + 1))
        try:
            calendar = library.country_holidays(country_code, years=years, language=self.language)
        except NotImplementedError:
            # The library knows 250 codes; anything else is simply not covered.
            return ProviderResult(
                provider=self.name,
                data=[],
                errors=[f"holidays 暂不支持国家代码 {country_code}，已跳过公共节假日。"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("holiday lookup failed for %s: %r", country_code, exc)
            return ProviderResult(
                provider=self.name,
                data=[],
                errors=[f"节假日查询失败：{type(exc).__name__}"],
            )

        now = datetime.now(timezone.utc)
        reference = SourceReference(
            source=self.name,
            source_type=SourceType.OPEN_DATA,
            source_id=f"holidays:{country_code}:{years[0]}-{years[-1]}",
            observed_at=now,
            fetched_at=now,
            # Holiday calendars are announced well in advance and change rarely.
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            request_context={
                "destination": profile.destination,
                "country_code": country_code,
                "years": years,
                "language": self.language,
                "library": "vacanza/holidays",
                "attribution": HOLIDAYS_ATTRIBUTION.as_credit(),
            },
            data_version="holidays-library",
            confidence=0.9,
            domain=DataDomain.GEO_FACT,
        )

        holidays = self._collect(calendar, profile, country_code, reference)
        result_calendar = HolidayCalendar(
            country_code=country_code,
            years=years,
            holidays=holidays,
            source_references=[reference],
        )

        # An empty window is a valid answer (many trips simply avoid holidays),
        # so it is reported as success with no errors.
        return ProviderResult(
            provider=self.name,
            data=[result_calendar],
            source_references=[reference],
        )

    def _collect(
        self,
        calendar: Any,
        profile: TripProfile,
        country_code: str,
        reference: SourceReference,
    ) -> list[PublicHoliday]:
        collected: list[PublicHoliday] = []
        for raw_date, raw_name in sorted(calendar.items()):
            day = raw_date if isinstance(raw_date, date) else _coerce_date(raw_date)
            if day is None:
                continue
            if not (profile.start_date <= day <= profile.end_date):
                continue
            name = str(raw_name)
            collected.append(
                PublicHoliday(
                    holiday_date=day,
                    name=name,
                    local_name=name,
                    country_code=country_code,
                    # The library's default category is the nationwide public
                    # calendar, so everything it returns here is nationwide.
                    nationwide=True,
                    category="public",
                    source_reference=reference,
                )
            )
        return collected

    async def health(self) -> ProviderHealth:
        library = _library()
        if library is None:
            return ProviderHealth(
                provider=self.name,
                status="degraded",
                latency_ms=0.0,
                detail="holidays 依赖未安装",
            )
        return ProviderHealth(
            provider=self.name,
            status="healthy",
            latency_ms=0.0,
            detail="holidays library (local, MIT)",
        )


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
