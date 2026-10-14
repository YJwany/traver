"""Every provider result a plan collects must be offered to the freshness audit.

``build_preview`` pairs each source reference with the *kind* of fact it carries before
handing the pairs to :class:`FreshnessService`. The pairing is what makes a verdict possible
at all — an hour-old forecast is stale and an hour-old restaurant listing is not, and a bare
reference cannot say which it is.

That pairing is a hand-written list, and a hand-written list can quietly fall behind the set
of results the function collects. The failure is invisible in both directions: nothing warns
about the missing entry, and nothing checks that nothing warned. So these tests do not
assert that the list contains a particular entry — which would only restate the code — they
stamp a distinctly ancient, provider-reported observation time on every reference a provider
returns and require each one to come back with a verdict. Anything never offered for
judgement cannot come back, so the test names it.

``holiday`` is the entry that was in fact missing. It has no horizon in
``MAX_AGE_SECONDS``, so adding it changes no output today; the reason to add it is that
:class:`FreshnessService` distinguishes "no horizon, so nothing is claimed about this kind
of fact" from "a horizon exists and the caller did not apply it". Only the first is
acceptable, and the two are indistinguishable from the outside unless the reference is
offered.

Both directions are checked, because a coverage test that only checks presence would pass if
the audit reported *everything* as stale: a horizon-less fact must come back clean, and a
fact past its horizon must come back stale.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from travel_api.domain.models import (
    Coordinates,
    HolidayCalendar,
    ObservationBasis,
    ProviderResult,
    SourceReference,
    TripProfile,
    WeatherForecast,
)
from travel_api.providers.mock import MockTravelProvider
from travel_api.providers.registry import ProviderRegistry
from travel_api.services.aggregation import build_preview

#: Older than every horizon in MAX_AGE_SECONDS (the longest is one day), so a reference that
#: reaches the audit is reported as too old rather than as age-unknown.
ANCIENT = timedelta(days=400)

#: The methods the mock supplies. Each is wrapped so its references are identifiable
#: one-to-one in the findings.
MOCK_METHODS = (
    "search_attractions",
    "calculate_routes",
    "search_hotels",
    "search_restaurants",
    "search_room_offers",
)

#: Methods the mock does not supply, provided here because their data types are the whole
#: point: weather has a one-hour horizon and holidays have none.
EXTRA_METHODS = ("search_weather", "search_holidays")


def _ancient_reference(source: str, source_id: str) -> SourceReference:
    now = datetime.now(timezone.utc)
    return SourceReference(
        source=source,
        source_type="open_data",
        source_id=source_id,
        observed_at=now - ANCIENT,
        # PROVIDER_REPORTED is deliberate. Under SOURCE_SILENT the reference would also be
        # reported, but as age-unknown — which would leave "audited and judged past its
        # horizon" indistinguishable from "audited and judged unverifiable".
        observed_at_basis=ObservationBasis.PROVIDER_REPORTED,
        fetched_at=now,
        domain="geo_fact",
    )


class _AncientProvider:
    """A provider whose every reference is undeniably stale and says so itself."""

    def __init__(self, inner: MockTravelProvider) -> None:
        self._inner = inner
        self.name = inner.name

    async def _wrap(self, method: str, *args: Any) -> ProviderResult:
        result: ProviderResult = await getattr(self._inner, method)(*args)
        rewritten = [
            _ancient_reference(result.provider or self.name, method)
            for _ in result.source_references
        ]
        return ProviderResult(
            provider=result.provider, data=result.data, source_references=rewritten
        )

    async def search_attractions(self, profile: TripProfile) -> ProviderResult:
        return await self._wrap("search_attractions", profile)

    async def calculate_routes(self, profile: TripProfile, points: list[Any]) -> ProviderResult:
        return await self._wrap("calculate_routes", profile, points)

    async def search_hotels(self, profile: TripProfile) -> ProviderResult:
        return await self._wrap("search_hotels", profile)

    async def search_restaurants(self, profile: TripProfile) -> ProviderResult:
        return await self._wrap("search_restaurants", profile)

    async def search_room_offers(self, hotels: list[Any], profile: TripProfile) -> ProviderResult:
        return await self._wrap("search_room_offers", hotels, profile)

    async def search_weather(self, profile: TripProfile) -> ProviderResult:
        ref = _ancient_reference(self.name, "search_weather")
        forecast = WeatherForecast(
            location_name=profile.destination,
            coordinates=Coordinates(latitude=30.65, longitude=104.08),
            days=[],
            source_references=[ref],
        )
        return ProviderResult(provider=self.name, data=[forecast], source_references=[ref])

    async def search_holidays(self, profile: TripProfile) -> ProviderResult:
        ref = _ancient_reference(self.name, "search_holidays")
        calendar = HolidayCalendar(country_code="CN", years=[2026])
        calendar.source_references = [ref]  # type: ignore[attr-defined]
        return ProviderResult(provider=self.name, data=[calendar], source_references=[ref])

    async def health(self) -> Any:
        return await self._inner.health()


def _profile() -> TripProfile:
    return TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
        total_budget=5000,
    )


def _registry() -> ProviderRegistry:
    return ProviderRegistry([_AncientProvider(MockTravelProvider("ctrip-mock"))])


def _judged(preview: Any) -> dict[str, dict]:
    return {fact["source_id"]: fact for fact in preview.stale_facts}


@pytest.mark.asyncio
async def test_every_reference_past_its_horizon_is_reported():
    """No result set may be missing from the pairing, and none may be reported as fine.

    Computed as coverage between two independently-derived sets: the methods that produced
    references, and the methods whose references came back stale. A result set absent from
    the hand-written pairing appears on the left and not on the right, and the assertion
    message names it.
    """
    preview = await build_preview(_profile(), _registry())

    produced = {
        ref.source_id
        for ref in preview.source_references
        if ref.source_id in (*MOCK_METHODS, *EXTRA_METHODS)
    }
    judged = set(_judged(preview))

    assert produced == set(MOCK_METHODS) | set(EXTRA_METHODS), (
        f"the fixture did not exercise every method, so this check would be partial: {produced}"
    )
    # Holidays are the one horizon-less type, so they are deliberately not in the report.
    expected = produced - {"search_holidays"}
    assert judged == expected, (
        "these provider results carry source references that never reach the freshness "
        f"audit, so their age is never reported: {sorted(expected - judged)}"
    )


@pytest.mark.asyncio
async def test_a_stale_hotel_listing_is_reported_as_stale():
    """A named case, because "hotel" is the type most easily forgotten.

    ``MAX_AGE_SECONDS["hotel"]`` is 86400. An OSM hotel listing stamped 400 days ago must
    not reach the page unlabelled — and the failure mode is silent, so it is worth pinning
    the concrete type alongside the general coverage check.
    """
    preview = await build_preview(_profile(), _registry())

    finding = _judged(preview)["search_hotels"]
    assert finding["reason"] == "too_old"
    assert finding["data_type"] == "hotel"
    assert finding["max_age_seconds"] == 86400
    # The age is stated in seconds *and* anchored to an absolute instant, so the sentence
    # stays true when the stored plan is read later.
    assert finding["data_age_seconds"] >= int(ANCIENT.total_seconds())
    assert finding["observed_at"]
    assert finding["evaluated_at"]


@pytest.mark.asyncio
async def test_a_stale_weather_forecast_is_reported():
    """Weather has the tightest horizon, so a missed pairing here is the worst case.

    A superseded forecast is not merely old: it describes yesterday's rain with the same
    confidence as tomorrow's.
    """
    preview = await build_preview(_profile(), _registry())

    finding = _judged(preview)["search_weather"]
    assert finding["reason"] == "too_old"
    assert finding["data_type"] == "weather"
    assert finding["max_age_seconds"] == 3600


@pytest.mark.asyncio
async def test_a_holiday_calendar_reaches_the_audit_so_a_future_horizon_would_apply():
    """Absence from the report must mean "no horizon", not "we forgot".

    A public-holiday calendar does not expire the way a forecast does, so the shipped policy
    gives it no horizon — and with no horizon there is no finding, which means the shipped
    output looks identical whether or not the reference was ever offered for judgement. That
    is the whole problem: the omission is unobservable until someone adds a horizon, at
    which point the warning silently fails to appear.

    So this test does not inspect the shipped policy. It supplies a policy that *does* put a
    horizon on holidays and asserts the ancient calendar is then reported. Only a reference
    that actually reaches the audit can be judged by it, so this fails if the pairing is
    missing — which is the state it was written to fix.
    """
    from travel_api.services.freshness import FreshnessPolicy, FreshnessService

    judging_service = FreshnessService(FreshnessPolicy(max_age={"holiday": 3600}))
    preview = await build_preview(_profile(), _registry(), freshness_service=judging_service)

    finding = _judged(preview).get("search_holidays")
    assert finding is not None, (
        "the holiday calendar never reached the audit: with a horizon configured it should "
        "have been reported, so its absence means the caller is not passing it in"
    )
    assert finding["reason"] == "too_old"
    assert finding["data_type"] == "holiday"
    assert finding["max_age_seconds"] == 3600

    # And the shipped policy is the one that declines to judge, so a clean report in normal
    # operation is a decision rather than an oversight.
    assert FreshnessService().policy.max_age_for("holiday") is None
    # Not because the fixture's age is unknowable: it reports its own, 400 days back.
    holiday_ref = next(
        ref for ref in preview.source_references if ref.source_id == "search_holidays"
    )
    assert FreshnessService().age_is_known(holiday_ref) is True
    assert holiday_ref.observed_at < datetime.now(timezone.utc) - ANCIENT / 2
