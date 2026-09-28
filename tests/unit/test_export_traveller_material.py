"""The exported document must carry the traveller's own material, and say who said it.

The Markdown export reached every field of a preview except ``ingested_notes`` and
``ingested_places``. That is the wrong way round, and it is the defect this file exists to pin:
the itinerary's POIs can be fetched again tomorrow from a data source, while a saved 小红书 post
can be deleted, a 抖音 video taken down and an account made private. The notes are the **only**
copy of the part that cannot be recovered, so an archive that omits them is missing what it was
built to keep.

Three things must survive the trip into a document that has no application around it:

* **The note is somebody's account, not a measurement**, and the appendix says so. A remembered
  「人均 80 元」 printed in a budget-looking table would read as a quote.
* **Both of its times.** A page that states no publication date must be printed as *not stating
  one*, not left blank — a note saved today from a 2019 post otherwise reads as current. This is
  the traveller's own material, so the error would misrepresent them rather than a provider.
* **The four place outcomes stay four.** 「没有找到」 for a name nobody looked for is a claim
  about the world made out of a fact about this deployment's configuration.

The notes are untyped dicts on the model (see ``TripPreview.ingested_notes``), which is exactly
why they need a test: no field name here is checked by a type.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from travel_api.domain.models import (
    BudgetBreakdown,
    Itinerary,
    Trip,
    TripPreview,
    TripProfile,
)
from travel_api.services.export import to_markdown

GENERATED = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _profile() -> TripProfile:
    return TripProfile(
        destination="杭州",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
    )


def _empty_budget() -> BudgetBreakdown:
    from decimal import Decimal

    return BudgetBreakdown(
        items=[],
        total=Decimal("0"),
        budget_limit=None,
        remaining=None,
        over_budget=False,
    )


def _note(
    *,
    kind: str = "price",
    text: str = "人均约 80 元",
    evidence: str = "人均 80 元左右",
    confidence: str = "high",
    reason: str = "「元」紧跟数字",
    ingestion_id: str = "ing-1",
    platform: str = "小红书",
    title: str | None = "杭州三天两夜",
    author: str | None = "某位用户",
    url: str | None = "https://example.invalid/note/1",
    published_at: str | None = "2024-05-01T00:00:00+00:00",
    ingested_at: str = "2026-09-28T10:00:00+00:00",
    matched: bool = False,
    matched_entity_name: str | None = None,
) -> dict:
    return {
        "ingestion_id": ingestion_id,
        "platform": platform,
        "url": url,
        "title": title,
        "author": author,
        "kind": kind,
        "text": text,
        "evidence": evidence,
        "confidence": confidence,
        "reason": reason,
        "published_at": published_at,
        "ingested_at": ingested_at,
        "data_age_is_known": published_at is not None,
        "matched": matched,
        "matched_entity_id": "entity-1" if matched else None,
        "matched_entity_name": matched_entity_name,
        "matched_entity_kind": "restaurant" if matched else None,
    }


def _place(
    *,
    name: str = "灵隐寺",
    status: str = "located",
    coordinates: dict | None = None,
    display_name: str | None = "灵隐寺",
    distance: float | None = 6.4,
    reason: str = "按距离目的地最近的结果选取（同名地点可能有多个）",
) -> dict:
    return {
        "name": name,
        "status": status,
        "coordinates": coordinates
        if coordinates is not None
        else {"latitude": 30.2408, "longitude": 120.101},
        "display_name": display_name,
        "match_confidence": 0.85,
        "straight_line_from_centre_km": distance,
        "reason": reason,
        "source_reference": {"source": "amap"},
    }


def _trip(*, notes: list[dict] | None = None, places: list[dict] | None = None) -> Trip:
    preview = TripPreview(
        profile=_profile(),
        hotels=[],
        hotel_offers=[],
        hotel_decisions=[],
        restaurants=[],
        attractions=[],
        route_legs=[],
        budget=_empty_budget(),
        provider_health=[],
        source_references=[],
        stale_facts=[],
        age_unknown_facts=[],
        ingested_notes=notes or [],
        ingested_places=places or [],
    )
    return Trip(
        profile=_profile(),
        preview=preview,
        itinerary=Itinerary(),
        updated_at=datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc),
    )


# -- the hole that was there: the notes did not reach the document ----------------


def test_a_supplied_note_appears_in_the_export_at_all():
    """The regression itself. Nothing else in this file matters if this fails."""
    markdown = to_markdown(_trip(notes=[_note()]), generated_at=GENERATED)

    assert "你提供的内容" in markdown
    assert "人均约 80 元" in markdown
    # The verbatim span it was read from, because a note without its source sentence is an
    # assertion rather than a quotation.
    assert "人均 80 元左右" in markdown


def test_a_plan_with_no_notes_gets_no_section():
    """Nothing supplied means nothing to say — not an empty heading implying a lost note."""
    markdown = to_markdown(_trip(), generated_at=GENERATED)
    assert "你提供的内容" not in markdown


def test_the_notes_are_printed_as_somebody_s_claim_not_as_a_measurement():
    """The sentence that stops a remembered price reading as a quote."""
    markdown = to_markdown(_trip(notes=[_note()]), generated_at=GENERATED)

    assert "某个人说的" in markdown
    assert "不是实测值" in markdown
    # And that it did not overwrite the sources above it.
    assert "没有覆盖" in markdown


def test_the_note_carries_the_pages_time_and_our_fetch_time_separately():
    """Both times, because one says how old the advice is and the other when we took it down."""
    markdown = to_markdown(_trip(notes=[_note()]), generated_at=GENERATED)

    assert "2024-05-01" in markdown
    assert "抓取时间" in markdown
    assert "2026-09-28 10:00 UTC" in markdown


def test_an_undated_note_says_the_page_did_not_state_a_date():
    """The absence must be printed. A blank field reads as "recent"."""
    markdown = to_markdown(
        _trip(notes=[_note(published_at=None)]),
        generated_at=GENERATED,
    )

    assert "页面没有标注" in markdown
    assert "无法判断" in markdown
    # It must not fall back to the fetch time and present that as the note's age.
    assert "原文发布时间：2026-09-28" not in markdown


def test_the_reading_confidence_is_worded_and_qualified():
    """「识别明确」 means the pattern was unambiguous, and the document has to say that."""
    markdown = to_markdown(
        _trip(notes=[_note(confidence="low", reason="换一种读法也说得通")]),
        generated_at=GENERATED,
    )

    assert "仅为线索" in markdown
    assert "换一种读法也说得通" in markdown
    assert "读得准不准" in markdown
    # A raw band identifier or a decimal would imply a calibration this does not have.
    assert "confidence" not in markdown
    assert "| low " not in markdown


def test_notes_are_grouped_by_the_page_they_came_from():
    """One URL and one publication date per source, not repeated on every row."""
    markdown = to_markdown(
        _trip(
            notes=[
                _note(kind="price", text="人均约 80 元"),
                _note(kind="opening_hours", text="周一闭馆", evidence="周一闭馆"),
            ]
        ),
        generated_at=GENERATED,
    )

    assert markdown.count("https://example.invalid/note/1") == 1
    assert markdown.count("原文发布时间") == 1
    # Both facts are still there, with their own kinds worded in Chinese.
    assert "价格" in markdown
    assert "开放 / 闭馆" in markdown


def test_two_pages_are_reported_separately():
    markdown = to_markdown(
        _trip(
            notes=[
                _note(ingestion_id="a", platform="小红书", url="https://example.invalid/a"),
                _note(ingestion_id="b", platform="抖音", url="https://example.invalid/b"),
            ]
        ),
        generated_at=GENERATED,
    )

    assert "https://example.invalid/a" in markdown
    assert "https://example.invalid/b" in markdown
    assert "小红书" in markdown
    assert "抖音" in markdown


def test_matched_and_unmatched_notes_are_told_apart():
    markdown = to_markdown(
        _trip(
            notes=[
                _note(matched=True, matched_entity_name="楼外楼"),
                _note(kind="tip", text="建议早点去", matched=False),
            ]
        ),
        generated_at=GENERATED,
    )

    assert "已关联到行程中的地点" in markdown
    assert "楼外楼" in markdown


def test_a_page_whose_notes_all_failed_to_attach_says_so():
    markdown = to_markdown(_trip(notes=[_note(matched=False)]), generated_at=GENERATED)
    assert "没有任何一条关联到行程中的实体" in markdown


def test_a_missing_title_does_not_produce_an_empty_heading():
    markdown = to_markdown(
        _trip(notes=[_note(title=None, author=None, url=None)]),
        generated_at=GENERATED,
    )
    # Stated, not left blank — and not replaced by the platform name, which is printed on the
    # line below and would then say the same thing twice.
    assert "这篇内容没有标题" in markdown
    assert "小红书" in markdown


def test_a_note_from_a_platform_this_build_does_not_know_still_renders():
    """A stored note outlives the code. Losing it would be worse than losing its label."""
    markdown = to_markdown(
        _trip(notes=[_note(platform="某个新平台", kind="某个新类型")]),
        generated_at=GENERATED,
    )
    assert "某个新平台" in markdown
    assert "某个新类型" in markdown


# -- the located places appendix ---------------------------------------------------


def test_a_located_place_is_printed_and_called_a_mention_not_a_sight():
    markdown = to_markdown(_trip(places=[_place()]), generated_at=GENERATED)

    assert "文中提到的地点" in markdown
    assert "灵隐寺" in markdown
    assert "已定位" in markdown
    assert "30.2408" in markdown
    # The claim that this is not a discovered POI and not a scheduled stop.
    assert "不是查到的景点" in markdown
    assert "没有被排进任何一天" in markdown


def test_a_place_rejected_as_elsewhere_keeps_its_distance():
    """「在 950 公里外」 is a fact about the note. Dropping it looks like a failed lookup."""
    markdown = to_markdown(
        _trip(
            places=[
                _place(
                    name="西湖",
                    status="elsewhere",
                    display_name="惠州西湖",
                    coordinates=None,
                    distance=979.1,
                    reason="找到的同名地点距离目的地约 979 公里（超过 80 公里）",
                )
            ]
        ),
        generated_at=GENERATED,
    )

    assert "在别处" in markdown
    assert "979.1 公里" in markdown
    assert "惠州西湖" in markdown


def test_not_attempted_is_never_written_as_not_found():
    """The fabrication this status exists to prevent, checked in the exported words.

    A deployment with no geocoder produces this outcome constantly. Writing 「没有找到」 would
    turn "we have no source for this" into "this place does not exist".
    """
    markdown = to_markdown(
        _trip(
            places=[
                _place(
                    status="not_attempted",
                    coordinates=None,
                    display_name=None,
                    distance=None,
                    reason="无法确定目的地的位置，因此没有对笔记里的地名做定位",
                )
            ]
        ),
        generated_at=GENERATED,
    )

    assert "没有查询" in markdown
    assert "不是「没有这个地方」，也不是「没有找到」" in markdown
    # The status identifier itself must not reach the page.
    assert "not_attempted" not in markdown


def test_a_lookup_that_was_made_and_found_nothing_says_a_source_was_asked():
    markdown = to_markdown(
        _trip(
            places=[
                _place(
                    name="查不到的地方",
                    status="not_found",
                    coordinates=None,
                    display_name=None,
                    distance=None,
                    reason="已查询地名来源，没有返回该名称的结果",
                )
            ]
        ),
        generated_at=GENERATED,
    )

    assert "已查询地名来源" in markdown
    # The distinction from not_attempted is the point, so the two must not read alike.
    assert "没有查询（" not in markdown


def test_the_distance_is_called_straight_line_and_no_travel_time_is_derived():
    markdown = to_markdown(_trip(places=[_place()]), generated_at=GENERATED)

    assert "直线距离" in markdown
    assert "不能用来推算走过去要多久" in markdown
    assert "没有从它推出任何通行时间" in markdown


def test_a_plan_with_no_located_places_gets_no_appendix():
    markdown = to_markdown(_trip(notes=[_note()]), generated_at=GENERATED)
    assert "文中提到的地点" not in markdown


def test_the_place_citation_explains_that_disambiguation_is_a_judgement():
    """A located name is one decision among several possible, and the document says so."""
    markdown = to_markdown(_trip(places=[_place()]), generated_at=GENERATED)
    assert "一次判定" in markdown
    assert "以你的判断为准" in markdown


def test_the_index_age_is_admitted_to_be_unknown():
    markdown = to_markdown(_trip(places=[_place()]), generated_at=GENERATED)
    assert "索引" in markdown
    assert "不知道索引本身有多新" in markdown


# -- the calendar gets the short version -------------------------------------------


def test_the_calendar_mentions_located_places_so_it_does_not_look_complete():
    """A phone calendar that silently omitted the traveller's own findings would look finished."""
    from travel_api.services.export import to_ics

    ics = to_ics(_trip(places=[_place()]), generated_at=GENERATED)
    assert "未排进行程" in ics
    assert "Markdown" in ics


def test_the_calendar_says_nothing_about_places_when_there_are_none():
    from travel_api.services.export import to_ics

    ics = to_ics(_trip(), generated_at=GENERATED)
    assert "未排进行程" not in ics
