"""Reviews are modelled, routed, dated and reported — but not served.

The user's requirement for this tool was stated as a rule about time: 「只能用近期的评价……
不能是那种几年前那种」. A tool cannot follow that rule with a `rating: float`, because a score
has nowhere to put a date — 「4.8 分」 from 2019 and 「4.8 分」 from last month are the same number
and different facts. So reviews exist as their own model with `published_at`, and the horizon
that applies to them is the *reviewer's* date rather than the listing's.

No source in this deployment publishes guest reviews, and these tests do not pretend
otherwise — the honest state is asserted directly (`test_no_shipped_source_serves_reviews`).
What they pin is that the capability is *wired*: a source that does publish them is called,
its results are attached, its stale reviews are reported, and its undated ones are not passed
off as recent.

That distinction is the thing under test, and it is the same one that made room rates
misleading for months. A capability declared in the matrix as 「接入一个来源即可」 makes a
promise: supplying that source changes the plan. If nothing calls `search_reviews`, the
promise is false, and the settings page becomes a lie told in the direction of optimism. So
`test_a_connected_review_source_is_actually_called` fails if the call site is removed — which
is the only reason to believe the matrix's advice.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from travel_api.domain.models import (
    ProviderHealth,
    ProviderResult,
    Review,
    SourceReference,
    TripProfile,
)
from travel_api.providers.base import (
    CAPABILITY_BY_METHOD,
    ReviewProvider,
)
from travel_api.providers.free import build_free_providers
from travel_api.providers.manifest import (
    CAPABILITY_GAP_NOTES,
    CAPABILITY_LABELS,
    capability_gaps,
)
from travel_api.providers.mock import MockTravelProvider
from travel_api.providers.registry import ProviderRegistry
from travel_api.services.aggregation import build_preview
from travel_api.services.freshness import MAX_AGE_SECONDS, FreshnessService

#: Long enough to be past the review horizon (18 months), and not so long that the test is
#: really about a different regime. 800 days is ~26 months.
ANCIENT_REVIEW = timedelta(days=800)


def _profile() -> TripProfile:
    return TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
        total_budget=5000,
    )


class _ReviewSource:
    """A source that answers `search_reviews`, recording what it was asked for.

    Deliberately minimal: it is not a travel provider and implements no other capability, which
    is the point — reviews are their own capability, and a source should be able to supply them
    without being able to supply anything else.
    """

    def __init__(
        self,
        *,
        ages: dict[str, timedelta | None] | None = None,
        extra_subject: str | None = None,
    ) -> None:
        self.name = "review-source"
        self.asked: list[str] = []
        self._ages = ages or {}
        self._extra_subject = extra_subject

    async def search_reviews(self, subject_ids: list[str], profile: TripProfile) -> ProviderResult:
        self.asked = list(subject_ids)
        now = datetime.now(timezone.utc)
        reference = SourceReference(
            source=self.name,
            source_type="official_api",
            source_id="search_reviews",
            observed_at=now,
            fetched_at=now,
            domain="content_reference",
        )
        subjects = list(subject_ids)
        if self._extra_subject is not None:
            subjects.append(self._extra_subject)

        reviews = []
        for subject in subjects:
            # Every subject gets a review so the count is predictable, with the age taken from
            # the table when the test named one and a fresh date otherwise.
            age = self._ages.get(subject, timedelta(days=10))
            reviews.append(
                Review(
                    subject_id=subject,
                    rating=4.5,
                    text="很好",
                    published_at=None if age is None else now - age,
                    source=self.name,
                    source_references=[reference],
                )
            )
        return ProviderResult(provider=self.name, data=reviews, source_references=[reference])

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, status="healthy")


# --------------------------------------------------------------------------------------
# The model: a score without a date is not a recent score
# --------------------------------------------------------------------------------------


def test_an_undated_review_is_not_a_recent_one():
    """`None` and `0` must stay distinguishable, which is the whole reason for the date field.

    An undated review is the dangerous case, not the irrelevant one: it is exactly what a
    scraper returns when it drops the timestamp, and a model that defaulted `published_at` to
    now would present it as written this minute. `age_days` therefore returns `None` rather
    than `0`, and `is_dated` lets a consumer exclude it instead of guessing.
    """
    undated = Review(subject_id="x", rating=5.0, source="s")
    assert undated.published_at is None
    assert undated.is_dated is False
    assert undated.age_days is None

    today = Review(
        subject_id="x",
        rating=5.0,
        source="s",
        published_at=datetime.now(timezone.utc),
    )
    assert today.is_dated is True
    # Zero means "written today", which is a claim the undated review must not make.
    assert today.age_days == 0
    assert today.age_days != undated.age_days


def test_age_is_measured_from_the_reviewers_date():
    """The age is the reviewer's, not our fetch time — the same rule the sources follow."""
    moment = datetime.now(timezone.utc) - timedelta(days=400)
    review = Review(subject_id="x", source="s", published_at=moment)

    assert review.age_days == 400


def test_a_naive_date_is_read_as_utc_rather_than_rejected():
    """A source that omits the offset must not crash the plan, and must not shift the age.

    ISO timestamps without a `Z` are common in real APIs, and `datetime - datetime` raises on
    mixed awareness. The choice here is to assume UTC rather than to drop the review: dropping
    it would silently discard the very date the requirement is about.
    """
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    review = Review(subject_id="x", source="s", published_at=naive)

    assert review.age_days == 30


# --------------------------------------------------------------------------------------
# The capability: declared, routed, and unserved by everything that ships
# --------------------------------------------------------------------------------------


def test_the_capability_is_routed_and_has_a_label():
    """A capability in `CAPABILITY_BY_METHOD` is dispatchable; one without a label has no name.

    Both halves matter. Without the routing entry `providers_for("search_reviews")` falls back
    to *every* provider (the registry's behaviour for unknown methods), so a review source
    would be called with the wrong protocol assumed and a non-source would be called at all.
    """
    assert CAPABILITY_BY_METHOD["search_reviews"] is ReviewProvider
    assert CAPABILITY_LABELS["search_reviews"] == "评价与评分"


@pytest.mark.asyncio
async def test_no_shipped_source_serves_reviews():
    """The honest state, asserted so it cannot quietly become a false claim.

    If a keyless source ever gains guest reviews, this test fails and the gap note and the
    capability matrix must be revisited together — which is the correct coupling, because the
    note currently tells the reader that no free source publishes them.
    """
    providers, http_client = build_free_providers()
    try:
        serving = [p.name for p in providers if isinstance(p, ReviewProvider)]
    finally:
        await http_client.aclose()

    assert serving == [], (
        "a keyless source now serves reviews; the capability is no longer a gap and "
        "CAPABILITY_GAP_NOTES must stop saying it is"
    )


def test_the_gap_is_reported_as_a_gap_not_as_coverage():
    """`everyone implements the method` must not read as `someone answers it`."""
    gaps = capability_gaps(["openstreetmap", "osrm"], None)
    ids = [gap["capability"] for gap in gaps]

    assert "search_reviews" in ids
    gap = next(item for item in gaps if item["capability"] == "search_reviews")
    assert gap["note"] == CAPABILITY_GAP_NOTES["search_reviews"]
    assert gap["closable_by"] == "source"


def test_reviews_have_their_own_horizon_distinct_from_the_places_they_are_about():
    """The number the user's requirement turns into, kept apart from the listing horizon.

    Judging a review by `MAX_AGE_SECONDS["restaurant"]` (one day) would flag every review ever
    written; judging it by nothing would let 「几年前那种」 through. The assertion is two-sided
    for that reason.
    """
    policy = FreshnessService().policy
    horizon = policy.max_age_for("review")

    assert horizon is not None, "reviews have no horizon, so an old opinion reads as current"
    assert horizon == MAX_AGE_SECONDS["review"]
    assert horizon > policy.max_age_for("restaurant") * 30
    assert horizon < 5 * 365 * 86400


# --------------------------------------------------------------------------------------
# The wiring: a connected source is called, and its answer is used
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_connected_review_source_is_actually_called():
    """The assertion that makes the settings page's advice true.

    Removing the `search_reviews` call from `build_preview` leaves every other test in this
    file passing and makes this one fail — which is the difference between a capability that is
    advertised and one that is usable.
    """
    source = _ReviewSource()
    registry = ProviderRegistry([MockTravelProvider("mock"), source])

    preview = await build_preview(_profile(), registry)

    assert source.asked, (
        "the review source was never asked, so plugging one in would have no effect — the "
        "capability matrix would be promising something the pipeline cannot use"
    )
    # Asked about the plan's own entities, so the aggregate is not empty and the ids are
    # exactly the ones the plan holds.
    known = {str(item.id) for item in (*preview.hotels, *preview.restaurants, *preview.attractions)}
    assert set(source.asked) == known
    assert len(source.asked) == len(known)

    # And the answer reached the result rather than being collected and dropped.
    assert len(preview.reviews) == len(known)
    assert {review.subject_id for review in preview.reviews} == known
    assert all(review.source == "review-source" for review in preview.reviews)


@pytest.mark.asyncio
async def test_a_review_about_a_place_not_in_the_plan_is_dropped():
    """A provider must not be able to introduce a subject the plan does not contain.

    The same guard enrichment gets. Without it, one response could attach an opinion to an
    arbitrary id, and the reader could not tell — a review carries the id, not the name, so
    nothing on the page would contradict it.
    """
    source = _ReviewSource(extra_subject="not-on-this-plan")
    registry = ProviderRegistry([MockTravelProvider("mock"), source])

    preview = await build_preview(_profile(), registry)

    assert "not-on-this-plan" not in {review.subject_id for review in preview.reviews}
    # The legitimate ones still arrive, so the guard filters rather than discarding the batch.
    assert preview.reviews


@pytest.mark.asyncio
async def test_a_review_past_its_horizon_is_reported_as_too_old():
    """The requirement, enforced: an old review is named rather than silently trusted.

    Reported under its own kind. Pairing reviews with the `restaurant` kind would apply the
    one-day listing horizon and mark the whole batch stale; pairing them with nothing would let
    them through unjudged. Both are asserted here, because the failure is invisible either way.
    """
    source = _ReviewSource()
    registry = ProviderRegistry([MockTravelProvider("mock"), source])
    preview = await build_preview(_profile(), registry)

    # Ask for a set whose every review is old, then re-run: the subject table is filled in
    # after the first run because the ids are the plan's own entities.
    old_source = _ReviewSource(ages={subject: ANCIENT_REVIEW for subject in source.asked})
    old_registry = ProviderRegistry([MockTravelProvider("mock"), old_source])
    old_preview = await build_preview(_profile(), old_registry)

    findings = [fact for fact in old_preview.stale_facts if fact["data_type"] == "review"]
    assert findings, "an 800-day-old review was not reported as too old"
    assert all(finding["reason"] == "too_old" for finding in findings)
    assert findings[0]["max_age_seconds"] == MAX_AGE_SECONDS["review"]
    # And the report says the members were *written*, not modified. Pinned at the producer as
    # well as at the consumer, because `test_export_documents.py` builds its own `ElementAge`:
    # a mutation that aged reviews with the OSM verb was invisible to every test until this
    # assertion existed.
    assert findings[0]["element_age"]["member_kind"] == "published"
    assert "写于" in findings[0]["detail"], (
        "the review's date is described as a modification, which no reviewer performed"
    )

    # And the fresh batch from the first run is *not* reported, so the horizon discriminates
    # rather than firing on every review.
    assert [fact for fact in preview.stale_facts if fact["data_type"] == "review"] == []


@pytest.mark.asyncio
async def test_an_undated_review_is_reported_as_age_unknown_not_as_fresh():
    """Somebody's opinion with no date is a fact we cannot judge, and saying so is the point.

    `age_unknown` rather than `too_old`: we do not know that it is old, and claiming staleness
    would be inventing a fact. But it must not vanish either — an unreported undated review is
    one a reader will assume is recent, which is precisely the mistake the requirement names.
    """
    known = _ReviewSource()
    registry = ProviderRegistry([MockTravelProvider("mock"), known])
    await build_preview(_profile(), registry)

    undated = _ReviewSource(ages={subject: None for subject in known.asked})
    undated_registry = ProviderRegistry([MockTravelProvider("mock"), undated])
    preview = await build_preview(_profile(), undated_registry)

    types = {fact["data_type"] for fact in preview.age_unknown_facts}
    assert "review" in types, "an undated review was silently treated as recent"


@pytest.mark.asyncio
async def test_the_review_step_is_announced_even_with_no_source():
    """A step list that changes shape hides the difference between "skipped" and "failed".

    Nothing serves reviews in the shipped deployment, so the step reports 无来源，跳过 — and the
    value of announcing it is that the reader learns the capability exists and is unconnected,
    instead of never seeing it and assuming the tool has no notion of reviews at all.
    """
    steps: list[tuple[str, str]] = []

    class _Reporter:
        async def step(self, name: str, detail: str = "") -> None:
            steps.append((name, detail))

        async def finish_step(self) -> None:
            return None

    registry = ProviderRegistry([MockTravelProvider("mock")])
    await build_preview(_profile(), registry, progress=_Reporter())

    reviews = [entry for entry in steps if entry[0] == "search_reviews"]
    assert len(reviews) == 1, f"the review step was not announced exactly once: {steps}"
    assert "无来源" in reviews[0][1]
