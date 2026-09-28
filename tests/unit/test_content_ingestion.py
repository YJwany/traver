"""The ingestion extractor: what it reads, and what it refuses to read.

The tests are written around the failure the feature would otherwise have: producing
confident-looking structure from prose that does not contain it. Every case below
therefore checks the *evidence* as much as the value, and several assert that nothing at
all was emitted.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from travel_api.domain.ingestion import (
    ContentPlatform,
    ExtractedFact,
    ExtractionConfidence,
    FactKind,
    IngestionRequest,
    parse_published_at,
    platform_from_url,
    platform_label_for,
)
from travel_api.services.ingestion import ContentExtractionEngine


@pytest.fixture
def engine() -> ContentExtractionEngine:
    return ContentExtractionEngine()


def extract(engine: ContentExtractionEngine, text: str, **kwargs) -> object:
    request = IngestionRequest(text=text, **kwargs)
    return engine.extract(request, ingestion_id="ing-1")


def facts_of(ingestion, kind: FactKind) -> list[ExtractedFact]:
    return [fact for fact in ingestion.facts if fact.kind is kind]


# -- platform attribution --------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.xiaohongshu.com/explore/abc", ContentPlatform.XIAOHONGSHU),
        ("https://xhslink.com/a/b", ContentPlatform.XIAOHONGSHU),
        ("https://www.douyin.com/video/123", ContentPlatform.DOUYIN),
        ("https://www.meituan.com/meishi/1", ContentPlatform.MEITUAN),
        ("https://www.dianping.com/shop/1", ContentPlatform.DIANPING),
        ("https://you.ctrip.com/travels/1.html", ContentPlatform.CTRIP),
        ("https://www.ly.com/1", ContentPlatform.TONGCHENG),
        ("https://www.mafengwo.cn/i/1.html", ContentPlatform.MAFENGWO),
        # An unrecognised host is not guessed at: mislabelling where a claim came from
        # would misrepresent how much weight it deserves.
        ("https://example.com/post", ContentPlatform.WEBSITE),
        (None, ContentPlatform.PASTE),
    ],
)
def test_the_platform_is_read_from_the_host(url, expected):
    assert platform_from_url(url) is expected


def test_every_platform_has_a_label():
    for platform in ContentPlatform:
        assert platform_label_for(platform) not in {"", None}


# -- prices ----------------------------------------------------------------


def test_a_price_is_only_read_next_to_a_currency_marker(engine):
    """A bare number is a date, a bus route or a floor — not a price.

    Treating one as a price is the classic way an extractor invents a budget, so the
    negative case is tested as carefully as the positive.
    """
    ingestion = extract(engine, "我们坐 3 号线到第 12 站下车，2024 年去过一次。")

    assert facts_of(ingestion, FactKind.PRICE) == []


def test_a_currency_symbol_attached_to_a_number_is_read_with_high_confidence(engine):
    ingestion = extract(engine, "门票 ¥120，学生票半价。")

    prices = facts_of(ingestion, FactKind.PRICE)
    assert prices
    assert prices[0].confidence is ExtractionConfidence.HIGH
    assert prices[0].value["amount"] == 120
    assert prices[0].value["currency"] == "CNY"
    # The quote is what makes the reading checkable in one glance.
    assert "¥120" in prices[0].evidence


def test_a_stated_range_stays_a_range(engine):
    """Averaging a range would invent a number nobody wrote down."""
    ingestion = extract(engine, "人均 80-120 元，看点什么菜。")

    price = facts_of(ingestion, FactKind.PRICE)[0]
    assert price.value["amount"] == 80
    assert price.value["amount_max"] == 120
    assert price.value["per"] == "person"
    assert "80" in price.text and "120" in price.text


def test_per_person_is_recorded_as_per_person(engine):
    """「人均 80」 and 「总共 80」 are different budgets."""
    ingestion = extract(engine, "人均 80 元。")

    assert facts_of(ingestion, FactKind.PRICE)[0].value["per"] == "person"


def test_the_same_sentence_does_not_become_two_prices(engine):
    """「人均 80 元」 matches two rules; reporting it twice would inflate the source.

    A fact count is read as a measure of how much the source contributed, so double
    counting is a quiet lie about richness.
    """
    ingestion = extract(engine, "人均 80 元。")

    assert len(facts_of(ingestion, FactKind.PRICE)) == 1


def test_an_ambiguous_amount_is_marked_medium_not_high(engine):
    """「20 块」 could be money or, in some dialects, distance.

    The extractor cannot tell, so it says so in the confidence rather than picking the
    reading that happens to be more useful.
    """
    ingestion = extract(engine, "打车过去 20 块。")

    price = facts_of(ingestion, FactKind.PRICE)[0]
    assert price.confidence is ExtractionConfidence.MEDIUM
    assert "未标明" in price.reason


# -- durations -------------------------------------------------------------


def test_minutes_are_not_converted_as_if_they_were_hours(engine):
    """The unit is read from the pattern that matched, never inferred.

    Inferring it from the number of capture groups once turned 「40 分钟」 into 2400
    minutes, which would have made an itinerary skip a whole day's worth of stops.
    """
    ingestion = extract(engine, "排队大概 40 分钟。")

    duration = facts_of(ingestion, FactKind.DURATION)[0]
    assert duration.value["minutes"] == 40


def test_hours_are_converted_to_minutes(engine):
    ingestion = extract(engine, "建议游玩 2 小时。")

    assert facts_of(ingestion, FactKind.DURATION)[0].value["minutes"] == 120


def test_a_stated_visit_duration_outranks_a_bare_one(engine):
    """Both rules match 「建议游玩 2 小时」; the one that knows what it measured wins."""
    ingestion = extract(engine, "建议游玩 2 小时。")

    durations = facts_of(ingestion, FactKind.DURATION)
    assert len(durations) == 1
    assert durations[0].confidence is ExtractionConfidence.HIGH


# -- opening hours ---------------------------------------------------------


def test_a_closed_day_is_extracted_because_it_ruins_a_morning(engine):
    """Scheduling a museum on its closed day is a wasted half-day.

    This is the single most valuable fact a note carries, and the one structured sources
    most often lack.
    """
    ingestion = extract(engine, "故宫周一闭馆，记得避开。")

    closed = facts_of(ingestion, FactKind.OPENING_HOURS)
    assert closed
    assert closed[0].value["closed"] is True
    assert closed[0].value["closed_weekday"] == "一"
    assert closed[0].confidence is ExtractionConfidence.HIGH


def test_a_closed_day_needs_both_halves(engine):
    """「周一」 alone is a date, and 「休息」 alone is a nap.

    Requiring both in one sentence is what keeps the rule from firing on ordinary prose.
    """
    assert facts_of(extract(engine, "我们是周一出发的。"), FactKind.OPENING_HOURS) == []
    assert facts_of(extract(engine, "走累了就休息一下。"), FactKind.OPENING_HOURS) == []


def test_stated_opening_hours_are_captured(engine):
    ingestion = extract(engine, "营业时间：09:00-17:00，旺季会延长。")

    hours = facts_of(ingestion, FactKind.OPENING_HOURS)
    assert hours
    assert "09:00" in hours[0].value["opening_hours"]


# -- tips and warnings -----------------------------------------------------


def test_a_concrete_negative_signal_becomes_a_warning(engine):
    ingestion = extract(engine, "那边排队很久，而且有宰客的，避坑。")

    warnings = facts_of(ingestion, FactKind.WARNING)
    assert warnings
    assert warnings[0].value["trigger"]


def test_generic_sentiment_is_not_dressed_up_as_a_warning(engine):
    """「不太好玩」 is one person's mood, not a fact about the place.

    Promoting it to a warning would give a stranger's afternoon the weight of evidence.
    """
    ingestion = extract(engine, "感觉不太好玩，也就那样吧。")

    assert facts_of(ingestion, FactKind.WARNING) == []
    assert facts_of(ingestion, FactKind.TIP) == []


def test_advice_phrasing_becomes_a_tip(engine):
    ingestion = extract(engine, "建议早点去，不然要排队。")

    tips = facts_of(ingestion, FactKind.TIP)
    assert tips
    assert tips[0].confidence is ExtractionConfidence.LOW


# -- place mentions --------------------------------------------------------


def test_a_recommended_place_name_is_recovered_without_the_verb(engine):
    """「推荐去灵隐寺」 must yield 「灵隐寺」, not the whole clause."""
    ingestion = extract(engine, "推荐去灵隐寺，香火很旺。")

    names = [fact.value["name"] for fact in facts_of(ingestion, FactKind.PLACE_MENTION)]
    assert "灵隐寺" in names


def test_a_place_embedded_mid_sentence_is_recovered(engine):
    """The realistic case: a name with no boundary marker around it.

    Chinese has no word separators, so a matched run starts wherever the engine found
    it. Cutting on characters that cannot occur inside a name, then keeping the last
    segment, is what recovers the name from the surrounding clause. 「杭州西湖」 is an
    acceptable reading of 「我昨天去了杭州西湖」 — the invariant is that no prose noise
    survives, not that the segmentation matches one particular expectation.
    """
    ingestion = extract(engine, "我昨天去了杭州西湖，风景很好。")

    names = [fact.value["name"] for fact in facts_of(ingestion, FactKind.PLACE_MENTION)]
    assert names
    assert any("西湖" in name for name in names)
    for name in names:
        assert not any(char in name for char in "我昨天去了，。")
        assert "昨天" not in name


def test_a_name_beginning_with_a_conjunction_is_not_mangled(engine):
    """和平公园 is a place; 和 is also a conjunction.

    Treating it as leading noise would corrupt exactly the names the rule was meant to
    clean, which is why it is excluded from both the prefix and the cut set.
    """
    ingestion = extract(engine, "和平公园的银杏很好看。")

    names = [fact.value["name"] for fact in facts_of(ingestion, FactKind.PLACE_MENTION)]
    assert "和平公园" in names


def test_a_sentence_about_a_closure_is_not_read_as_a_place(engine):
    """「周一闭馆」 ends with 馆, so the suffix rule reported it as a destination.

    A closure turned into somewhere to visit is the worst kind of extraction error: it
    is confidently wrong in the direction that wastes a traveller's morning.
    """
    ingestion = extract(engine, "周一闭馆，注意别白跑。")

    names = [fact.value["name"] for fact in facts_of(ingestion, FactKind.PLACE_MENTION)]
    assert names == []
    # The closure itself is still captured, by the rule that understands it.
    assert facts_of(ingestion, FactKind.OPENING_HOURS)


def test_a_mention_is_never_presented_as_a_verified_place(engine):
    """A suffix proves it is a place; it proves nothing about this trip.

    Marking these low-confidence and naming them "mentions" is what keeps them from
    being routed to as if they were discovered POIs.
    """
    ingestion = extract(engine, "推荐去灵隐寺。")

    mention = facts_of(ingestion, FactKind.PLACE_MENTION)[0]
    assert mention.confidence is ExtractionConfidence.LOW
    # Even a recommendation only reports that the *source* liked it.
    assert mention.value["recommended"] is True


# -- what must not be invented ---------------------------------------------


def test_prose_with_no_extractable_content_yields_nothing_and_says_so(engine):
    """ "Nothing found" and "extraction failed" look identical from outside.

    Only one of them is a problem, so the difference is stated in a note.
    """
    ingestion = extract(engine, "这次旅行很开心，下次还想去别的地方看看风景。")

    assert ingestion.facts == []
    assert any("没有识别到" in note for note in ingestion.notes)


def test_a_very_short_input_is_still_read(engine):
    """「人均 80 元。」 is eight characters and carries a real price.

    An earlier version gated extraction behind a minimum length and so discarded exactly
    the terse notes people actually paste. Length is a hint offered when nothing was
    found, never a reason to stop reading.
    """
    ingestion = extract(engine, "人均 80 元。")

    assert facts_of(ingestion, FactKind.PRICE)
    assert ingestion.notes == [] or all("很短" not in note for note in ingestion.notes)


def test_a_short_input_with_nothing_in_it_says_so(engine):
    ingestion = extract(engine, "好的")

    assert ingestion.facts == []
    assert any("很短" in note for note in ingestion.notes)


def test_an_over_long_input_is_truncated_and_the_truncation_is_disclosed(engine):
    """A pasted page can be mostly navigation markup; hiding the cut would be silent."""
    long_text = "西湖很美。" * 20000
    ingestion = extract(engine, long_text)

    assert any("只读取前" in note for note in ingestion.notes)
    assert ingestion.input_chars == 60000


# -- the publication date --------------------------------------------------


def test_a_missing_publication_date_is_reported_as_missing(engine):
    """It is tempting to default this to now so everything looks fresh.

    That would relabel a 2019 note as current advice — the freshness rule's exact
    failure, here applied to the traveller's own material.
    """
    ingestion = extract(engine, "人均 80 元。", url="https://www.xiaohongshu.com/explore/a")

    assert ingestion.published_at is None
    assert any("发布时间" in note for note in ingestion.notes)


def test_without_a_date_the_reference_admits_the_data_age_is_unknown(engine):
    from travel_api.domain.models import ObservationBasis

    ingestion = extract(engine, "人均 80 元。")
    reference = ingestion.source_reference()

    assert reference.observed_at_basis is ObservationBasis.SOURCE_SILENT
    # The reference must never be treated as evidence the advice is current.
    assert reference.observed_at_basis.age_is_about_the_data is False
    # And no timer is applied: re-ingesting the same text would not make it newer.
    assert reference.expires_at is None


def test_with_a_date_the_reference_knows_how_old_the_advice_is(engine):
    from travel_api.domain.models import DataDomain, ObservationBasis, SourceType

    published = datetime(2024, 5, 1, 8, 0)
    ingestion = extract(engine, "人均 80 元。", published_at=published)
    reference = ingestion.source_reference()

    assert reference.observed_at_basis is ObservationBasis.PROVIDER_REPORTED
    assert reference.observed_at == published
    assert reference.observed_at_basis.age_is_about_the_data is True
    # Always a content citation, never a commercial fact: a remembered price must not
    # stand in for a quoted rate.
    assert reference.domain is DataDomain.CONTENT_REFERENCE
    assert reference.source_type is SourceType.USER_PROVIDED


def test_a_reference_records_the_platform_and_never_claims_certainty(engine):
    ingestion = extract(engine, "人均 80 元。", url="https://www.dianping.com/shop/1")
    reference = ingestion.source_reference()

    assert reference.request_context["platform"] == "dianping"
    assert reference.request_context["published_at_known"] is False
    # Somebody's account of a trip, not a measurement.
    assert reference.confidence < 0.5
    assert reference.domain.value == "content_reference"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-05-01 08:00:00", datetime(2024, 5, 1, 8, 0)),
        ("2024-05-01 08:00", datetime(2024, 5, 1, 8, 0)),
        ("2024-05-01", datetime(2024, 5, 1)),
        ("2024/05/01", datetime(2024, 5, 1)),
        ("2024年05月01日", datetime(2024, 5, 1)),
        # Unusable input must yield None, never the current time.
        ("昨天", None),
        ("", None),
        (None, None),
    ],
)
def test_a_page_date_is_parsed_or_reported_absent(raw, expected):
    assert parse_published_at(raw) == expected


# -- evidence integrity ----------------------------------------------------


def test_every_fact_quotes_text_that_is_actually_in_the_input(engine):
    """The invariant that makes the whole feature auditable.

    An extractor that reports facts without quotes asks to be believed. This one has to
    be checkable, so the evidence must be a real span of what the traveller supplied.
    """
    text = (
        "推荐去灵隐寺，建议游玩 2 小时。"
        "门票 ¥75，人均 80 元。"
        "周一闭馆，注意别白跑。"
        "那边排队很久，避坑。"
    )
    ingestion = extract(engine, text)

    assert ingestion.facts
    for fact in ingestion.facts:
        assert fact.evidence in text, f"{fact.kind}: evidence is not a span of the input"
        assert text[fact.char_start : fact.char_end] == fact.evidence


def test_a_realistic_note_yields_several_kinds_of_fact(engine):
    """An end-to-end reading of the sort of text people actually paste."""
    note = (
        "杭州三日游攻略\n"
        "推荐去灵隐寺，建议游玩 2 小时，香火很旺。\n"
        "门票 ¥75，学生半价。\n"
        "周一闭馆，注意别白跑。\n"
        "楼外楼人均 150 元，有点贵但值得。\n"
    )
    ingestion = extract(engine, note, url="https://www.xiaohongshu.com/explore/xyz")

    kinds = {fact.kind for fact in ingestion.facts}
    assert FactKind.PLACE_MENTION in kinds
    assert FactKind.DURATION in kinds
    assert FactKind.PRICE in kinds
    assert FactKind.OPENING_HOURS in kinds
    assert ingestion.platform is ContentPlatform.XIAOHONGSHU
    assert ingestion.input_chars == len(note)
