"""What happens when two sources disagree about the same place.

This is the heart of a tool that aggregates other people's data. The failure it guards
against is subtle and worse than an outright error: when two sources gave different ticket
prices, the old merge kept whichever provider came first and the entity still carried
*both* source references — so the screen showed one number attributed to two sources, which
reads as agreement. A traveller would see "¥60, confirmed by two sources" where the second
source had actually said ¥80.

So the tests below assert two things at once, and either alone is insufficient:

  * the disagreement is recorded, with every claim, its source and its time;
  * the value that is shown is one source's claim, chosen by a rule the payload states.

Agreement is asserted too, because a system that flags everything as disputed would be
useless — the point is that the flag means something.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from travel_api.domain.models import (
    Attraction,
    Coordinates,
    DataDomain,
    FieldDisagreement,
    Hotel,
    Location,
    Money,
    ObservationBasis,
    OpeningHours,
    Restaurant,
    SourceReference,
    SourceType,
)
from travel_api.services.entity_resolution import (
    deduplicate_attractions,
    deduplicate_hotels,
    deduplicate_restaurants,
    specs_for,
)

BASE = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
#: 西湖, so the two providers describe the same point.
WEST_LAKE = Coordinates(latitude=30.2489, longitude=120.1496)


def reference(source: str, *, minutes: int = 0) -> SourceReference:
    """A source reference whose observation time is ``minutes`` after BASE."""
    when = BASE + timedelta(minutes=minutes)
    # The two sources a traveller actually ends up comparing: a community geo dataset and
    # a commercial one with a real contract behind it.
    kind = SourceType.OPEN_DATA if source == "openstreetmap" else SourceType.OFFICIAL_API
    return SourceReference(
        source=source,
        source_type=kind,
        source_id=f"{source}-west-lake",
        observed_at=when,
        fetched_at=when,
        observed_at_basis=ObservationBasis.PROVIDER_REPORTED,
        domain=DataDomain.GEO_FACT,
    )


def location(reference_id: str | None = None) -> Location:
    return Location(
        address="杭州市西湖区",
        city="杭州",
        coordinates=WEST_LAKE,
        source_reference_id=reference_id,
    )


def attraction(
    source: str,
    *,
    name: str = "西湖",
    ticket: str | None = None,
    rating: float | None = None,
    hours: list[OpeningHours] | None = None,
    minutes: int = 0,
    currency: str = "CNY",
    is_estimate: bool = False,
    latitude: float = WEST_LAKE.latitude,
    longitude: float = WEST_LAKE.longitude,
) -> Attraction:
    ref = reference(source, minutes=minutes)
    return Attraction(
        canonical_name=name,
        location=Location(
            address="杭州市西湖区",
            city="杭州",
            coordinates=Coordinates(latitude=latitude, longitude=longitude),
            source_reference_id=f"{source}-west-lake",
        ),
        rating=rating,
        ticket_price=None
        if ticket is None
        else Money(
            value=Decimal(ticket),
            currency=currency,
            is_estimate=is_estimate,
            source_reference_id=ref.source_id,
        ),
        opening_hours=hours or [],
        source_references=[ref],
    )


def fields_of(entity, field: str) -> FieldDisagreement:
    finding = next((d for d in entity.disagreements if d.field == field), None)
    assert finding is not None, (
        f"expected a recorded disagreement about {field}; "
        f"got {[d.field for d in entity.disagreements]}"
    )
    return finding


# -- agreement is silent, and that silence is meaningful ---------------------------


def test_two_sources_that_agree_produce_no_disagreement():
    """Agreement is not a claim of verification, but it must not be flagged as a dispute.

    Both references are still kept, which is what makes the corroboration visible.
    """
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", minutes=0),
            attraction("amap", ticket="60", minutes=30),
        ]
    )

    assert len(merged) == 1
    assert merged[0].disagreements == []
    assert {ref.source for ref in merged[0].source_references} == {"openstreetmap", "amap"}


def test_a_source_that_says_nothing_is_not_a_disagreement():
    """Silence is not contradiction. Flagging it would make the warning meaningless."""
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", minutes=0),
            attraction("amap", ticket=None, minutes=30),
        ]
    )

    assert merged[0].disagreements == []
    # The source that knew more wins by default, which is the correct merge.
    assert merged[0].ticket_price is not None
    assert merged[0].ticket_price.value == Decimal("60")


def test_the_same_value_written_differently_is_not_a_disagreement():
    """``60`` and ``60.00`` are one price; comparing strings would invent a conflict."""
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", minutes=0),
            attraction("amap", ticket="60.00", minutes=30),
        ]
    )

    assert merged[0].disagreements == []


def test_a_weekly_schedule_listed_in_another_order_is_not_a_disagreement():
    """The same opening hours in a different order are the same opening hours."""
    weekday = [OpeningHours(weekday=0, opens_at=_time(8), closes_at=_time(17))]
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", hours=weekday, minutes=0),
            attraction("amap", hours=list(weekday), minutes=30),
        ]
    )

    assert merged[0].disagreements == []


# -- disagreement is recorded, and nothing is discarded ----------------------------


def test_different_ticket_prices_are_recorded_with_every_claim():
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", minutes=0),
            attraction("amap", ticket="80", minutes=30),
        ]
    )

    finding = fields_of(merged[0], "ticket_price")
    # Both claims survive: a reader can see the conflict for themselves.
    assert {claim.source for claim in finding.claims} == {"openstreetmap", "amap"}
    assert {claim.value_text for claim in finding.claims} == {"¥60", "¥80"}
    assert sorted(finding.disputed_values) == ["¥60", "¥80"]


def test_the_retained_value_is_attributable_and_the_rule_is_stated():
    """The number on screen must be traceable, and why it was chosen must be readable."""
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", minutes=0),
            attraction("amap", ticket="80", minutes=30),
        ]
    )

    finding = fields_of(merged[0], "ticket_price")
    # 高德 is 30 minutes newer, so it wins — and the entity's own value matches it.
    assert finding.chosen_source == "amap"
    assert finding.chosen_value_text == "¥80"
    assert merged[0].ticket_price is not None
    assert merged[0].ticket_price.value == Decimal("80")
    # The rule is explained rather than left for the reader to infer.
    assert "最新" in finding.chosen_reason
    assert "不代表其他来源是错的" in finding.chosen_reason


def test_the_result_does_not_depend_on_provider_order():
    """Provider registration order must not be a hidden input to a factual decision.

    The old behaviour was exactly this order dependence: first provider wins. Two runs of
    the same data would then disagree about the ticket price.
    """
    forward = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", minutes=0),
            attraction("amap", ticket="80", minutes=30),
        ]
    )
    backward = deduplicate_attractions(
        [
            attraction("amap", ticket="80", minutes=30),
            attraction("openstreetmap", ticket="60", minutes=0),
        ]
    )

    assert forward[0].ticket_price.value == backward[0].ticket_price.value == Decimal("80")
    assert (
        fields_of(forward[0], "ticket_price").chosen_source
        == fields_of(backward[0], "ticket_price").chosen_source
    )


def test_recency_decides_in_the_other_direction_too():
    """The rule is recency, not a preference for any particular source."""
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", minutes=90),
            attraction("amap", ticket="80", minutes=5),
        ]
    )

    finding = fields_of(merged[0], "ticket_price")
    assert finding.chosen_source == "openstreetmap"
    assert merged[0].ticket_price.value == Decimal("60")


def test_a_third_source_can_tip_the_balance_back():
    """A later-merging source has to be able to change the outcome, not just append to it.

    A says ¥60, B says ¥80, C says ¥60. Two of three agree on ¥60 and C is also the most
    recent, so the retained value must move back. These entities always arrive in one
    batch, but the merge is called once per batch and must consider every claim.
    """
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", minutes=0),
            attraction("amap", ticket="80", minutes=10),
            attraction("user-content", ticket="60", minutes=20),
        ]
    )

    finding = fields_of(merged[0], "ticket_price")
    assert merged[0].ticket_price.value == Decimal("60")
    # All three claims are listed, so "the sources disagreed" is not overstated into
    # "the sources split evenly" either.
    assert len(finding.claims) == 3
    assert finding.disputed_values == ["¥60", "¥80"]


def test_a_different_currency_is_a_different_fact():
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", currency="CNY", minutes=0),
            attraction("amap", ticket="60", currency="USD", minutes=30),
        ]
    )

    finding = fields_of(merged[0], "ticket_price")
    assert finding.disputed_values == ["¥60", "US$ 60"] or set(finding.disputed_values) == {
        "¥60",
        "USD 60",
    }


def test_an_estimate_and_a_report_of_the_same_price_are_not_a_disagreement():
    """The number agrees, so this is not a conflict — but the report must win.

    Two sources can agree that the ticket is ¥60 while only one of them actually asserts
    it; the other is our own inference. Keeping the estimate when a real report is on the
    table would present a derivation where a statement exists.
    """
    merged = deduplicate_attractions(
        [
            attraction("user-content", ticket="60", is_estimate=True, minutes=30),
            attraction("openstreetmap", ticket="60", minutes=0),
        ]
    )

    assert merged[0].disagreements == []
    # The older-but-reported claim is the one retained, despite the estimate being newer.
    assert merged[0].ticket_price is not None
    assert merged[0].ticket_price.is_estimate is False
    assert {ref.source for ref in merged[0].source_references} == {"openstreetmap", "user-content"}


def test_a_disputed_estimate_loses_to_a_reported_value_and_says_why():
    """When the numbers differ, the rule has to prefer evidence over recency — and explain it.

    An estimate is our own derivation, so "which is newer" is not a meaningful comparison
    against a real observation.
    """
    merged = deduplicate_attractions(
        [
            attraction("user-content", ticket="80", is_estimate=True, minutes=120),
            attraction("openstreetmap", ticket="60", minutes=0),
        ]
    )

    finding = fields_of(merged[0], "ticket_price")
    assert finding.chosen_source == "openstreetmap"
    assert merged[0].ticket_price.value == Decimal("60")
    assert "估算不是证据" in finding.chosen_reason


def test_different_opening_hours_are_recorded_readably():
    merged = deduplicate_attractions(
        [
            attraction(
                "openstreetmap",
                hours=[OpeningHours(weekday=0, opens_at=_time(8), closes_at=_time(17))],
                minutes=0,
            ),
            attraction(
                "amap",
                hours=[OpeningHours(weekday=0, opens_at=_time(9), closes_at=_time(18))],
                minutes=30,
            ),
        ]
    )

    finding = fields_of(merged[0], "opening_hours")
    assert set(finding.disputed_values) == {"周一 08:00–17:00", "周一 09:00–18:00"}


def test_several_disagreeing_fields_are_all_reported():
    """Reporting only the first conflict would hide the rest behind a fix."""
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", ticket="60", rating=4.5, minutes=0),
            attraction("amap", ticket="80", rating=3.0, minutes=30),
        ]
    )

    assert {d.field for d in merged[0].disagreements} == {"ticket_price", "rating"}


# -- claims with no attributable source is a distinct, weaker situation -------------


def test_claims_with_no_source_reference_are_not_attributed_to_anyone():
    """With nothing to attribute a value to, the payload must not invent a source.

    It still has to retain one value, so it does so deterministically — and the recorded
    reason says that is all it did, rather than claiming a recency judgement there was no
    evidence for.
    """
    unattributed_a = attraction("openstreetmap", ticket="60")
    unattributed_b = attraction("amap", ticket="80")
    unattributed_a.source_references = []
    unattributed_b.source_references = []

    merged = deduplicate_attractions([unattributed_a, unattributed_b])
    finding = fields_of(merged[0], "ticket_price")

    assert {claim.source for claim in finding.claims} == {"未知来源"}
    assert "没有给出" in finding.chosen_reason
    # No recency claim, because there were no times to compare.
    assert "最新" not in finding.chosen_reason
    assert "估算不是证据" not in finding.chosen_reason


def test_an_unattributed_claim_cannot_win_on_recency():
    """A claim with no observation time must never beat one with a real timestamp.

    Defaulting a missing time to "now" would make the least attributable value look like
    the freshest one, which is the exact inversion the freshness work exists to prevent.
    """
    attributed = attraction("openstreetmap", ticket="60", minutes=0)
    unattributed = attraction("amap", ticket="80")
    unattributed.source_references = []

    merged = deduplicate_attractions([unattributed, attributed])
    finding = fields_of(merged[0], "ticket_price")

    assert finding.chosen_source == "openstreetmap"
    assert merged[0].ticket_price.value == Decimal("60")


def test_a_claim_is_attributed_to_its_own_source_not_to_the_merged_one():
    """Regression: the claim ledger has to be read before the references are merged.

    Merging references first left the primary holding the other source's reference, so its
    value was attributed to that source *and* inherited its timestamp — which made the
    wrong value win the recency comparison while wearing a confident, wrong label. Caught
    by the unattributed-claim test above; pinned here because the ordering is invisible in
    the code and trivially reversible.
    """
    first = attraction("amap", ticket="80", minutes=0)
    first.source_references = []
    second = attraction("openstreetmap", ticket="60", minutes=0)

    finding = fields_of(deduplicate_attractions([first, second])[0], "ticket_price")

    claims = {claim.value_text: claim.source for claim in finding.claims}
    # The ¥80 value came from a record with no reference at all, so it must not be
    # credited to openstreetmap merely because that source was merged in beside it.
    assert claims["¥80"] == "未知来源"
    assert claims["¥60"] == "openstreetmap"


# -- the three entity kinds --------------------------------------------------------


def test_restaurants_report_a_price_disagreement():
    def restaurant(source: str, cost: str, *, minutes: int) -> Restaurant:
        ref = reference(source, minutes=minutes)
        return Restaurant(
            canonical_name="楼外楼",
            location=location(ref.source_id),
            average_cost=Money(value=Decimal(cost), currency="CNY"),
            source_references=[ref],
        )

    merged = deduplicate_restaurants(
        [restaurant("openstreetmap", "150", minutes=0), restaurant("amap", "220", minutes=30)]
    )

    finding = fields_of(merged[0], "average_cost")
    assert finding.disputed_values == ["¥150", "¥220"]
    assert merged[0].average_cost.value == Decimal("220")


def test_hotels_report_a_review_count_disagreement():
    def hotel(source: str, reviews: int, *, minutes: int) -> Hotel:
        ref = reference(source, minutes=minutes)
        return Hotel(
            canonical_name="杭州西湖国宾馆",
            location=location(ref.source_id),
            review_count=reviews,
            source_references=[ref],
        )

    merged = deduplicate_hotels(
        [hotel("openstreetmap", 120, minutes=0), hotel("amap", 4300, minutes=30)]
    )

    finding = fields_of(merged[0], "review_count")
    assert finding.disputed_values == ["120 条评价", "4300 条评价"]
    # The old code took the larger count as an improvement, which silently discarded the
    # other source's number while still listing the source.
    assert merged[0].review_count == 4300


def test_places_far_apart_are_never_merged_at_all():
    """The 500 m rule is what makes two same-named places one place. Beyond it, two."""
    merged = deduplicate_attractions(
        [
            attraction("openstreetmap", name="人民公园", minutes=0),
            attraction("amap", name="人民公园", minutes=30, latitude=30.30, longitude=120.30),
        ]
    )

    assert len(merged) == 2
    assert all(item.disagreements == [] for item in merged)


# -- interfaces --------------------------------------------------------------------


def test_an_undeclared_entity_kind_fails_loudly():
    """Adding a kind to the merge must require deciding what can contradict it."""
    with pytest.raises(TypeError, match="no comparable fields"):
        specs_for(object)


def test_the_declared_fields_are_the_ones_a_traveller_can_be_misled_by():
    """Guards against a field being dropped from the ledger by accident."""
    assert {spec.name for spec in specs_for(Attraction)} == {
        "ticket_price",
        "opening_hours",
        "rating",
    }
    assert {spec.name for spec in specs_for(Restaurant)} == {
        "average_cost",
        "rating",
        "review_count",
    }
    assert {spec.name for spec in specs_for(Hotel)} == {"rating", "review_count"}


def _time(hour: int):
    from datetime import time

    return time(hour=hour)
