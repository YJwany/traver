"""An export leaves the application, so it has to carry its own explanations.

Every other view of a plan sits next to the source chips and the freshness panel that say what
the numbers mean. A downloaded file does not: it is opened in a text editor, printed, or
imported into a calendar, months later, with no way to ask what anything meant. That makes the
export the strongest case for the freshness rule rather than an exception to it.

Two failures are easy to ship here and impossible to notice, because both produce a document
that looks right:

**A claim manufactured from a missing field.** "全部来源都提供了数据自身时间" derived from an
empty ``age_unknown_facts`` is a verification nothing performed — the same bug the web panel
was fixed for. The tests below build a preview whose references are *silent* while
``age_unknown_facts`` is ``[]``, which is what an older stored snapshot looks like, and require
the document not to claim verification.

**Silent text destruction.** iCalendar folds long lines at 75 octets, and Chinese is three
bytes per character, so a fold lands mid-character constantly. The first implementation sliced
the encoded bytes and decoded each half with ``errors="ignore"``, which dropped characters while
leaving a file that still parsed. The test folds at many lengths and requires an exact
round-trip.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from travel_api.domain.models import (
    BudgetBreakdown,
    BudgetItem,
    Coordinates,
    DataDomain,
    Itinerary,
    ItineraryDay,
    ItineraryItem,
    ItineraryItemType,
    ObservationBasis,
    Pace,
    SourceReference,
    SourceType,
    TravelMode,
    Trip,
    TripPreview,
    TripProfile,
    WeatherForecast,
)
from travel_api.services.export import (
    _ics_fold,
    content_disposition,
    destination_timezone,
    sources_without_a_known_age,
    to_ics,
    to_markdown,
)

GENERATED = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

#: The instant a reference is stamped with, so the printed times are predictable.
FETCHED = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)


def _reference(
    source: str = "open-meteo",
    *,
    basis: ObservationBasis = ObservationBasis.SOURCE_SILENT,
    source_id: str = "open-meteo:forecast",
) -> SourceReference:
    return SourceReference(
        source=source,
        source_type=SourceType.OPEN_DATA,
        source_id=source_id,
        observed_at=OBSERVED,
        fetched_at=FETCHED,
        observed_at_basis=basis,
        domain=DataDomain.GEO_FACT,
    )


def _profile(*, travel_mode: TravelMode | None = None) -> TripProfile:
    return TripProfile(
        destination="杭州",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=2,
        pace=Pace.BALANCED,
        travel_mode=travel_mode,
    )


def _empty_budget() -> BudgetBreakdown:
    return BudgetBreakdown(
        items=[],
        total=Decimal("0"),
        budget_limit=None,
        remaining=None,
        over_budget=False,
    )


def _preview(
    *,
    references: list[SourceReference] | None = None,
    stale: list[dict] | None = None,
    age_unknown: list[dict] | None = None,
    weather: WeatherForecast | None = None,
) -> TripPreview:
    profile = _profile()
    return TripPreview(
        profile=profile,
        hotels=[],
        hotel_offers=[],
        hotel_decisions=[],
        restaurants=[],
        attractions=[],
        route_legs=[],
        budget=_empty_budget(),
        provider_health=[],
        source_references=references if references is not None else [],
        stale_facts=stale or [],
        age_unknown_facts=age_unknown or [],
        weather=weather,
    )


def _trip(
    *,
    preview: TripPreview | None = None,
    itinerary: Itinerary | None = None,
    budget: BudgetBreakdown | None = None,
    profile: TripProfile | None = None,
) -> Trip:
    return Trip(
        profile=profile or _profile(),
        preview=preview,
        itinerary=itinerary or Itinerary(),
        budget=budget,
        updated_at=FETCHED,
    )


def _day_with_one_item(*, conflicts: list[str] | None = None) -> Itinerary:
    item = ItineraryItem(
        sequence=1,
        item_type=ItineraryItemType.ATTRACTION,
        title="西湖",
        start_time=datetime(2026, 10, 3, 9, 30),
        end_time=datetime(2026, 10, 3, 11, 30),
        visit_duration_minutes=120,
    )
    return Itinerary(
        days=[ItineraryDay(day_date=date(2026, 10, 3), day_index=1, items=[item])],
        conflicts=conflicts or [],
    )


# -- Markdown: the document states when it was made and that it is frozen -------------


def test_the_document_carries_its_own_generation_time_and_freeze_warning():
    """A snapshot has to say it is a snapshot, and when it was taken."""
    markdown = to_markdown(_trip(), generated_at=GENERATED)
    assert "2026-09-28 12:00 UTC" in markdown
    assert "不会自动更新" in markdown


def test_the_itinerary_prints_destination_wall_clock_without_inventing_a_timezone():
    """The stored times are naive, so the document must not present them as UTC."""
    markdown = to_markdown(_trip(itinerary=_day_with_one_item()), generated_at=GENERATED)
    assert "09:30–11:30" in markdown
    assert "目的地当地时间" in markdown
    # And it says positively that it knows no zone, rather than staying silent — silence would
    # let a reader assume the times are already in their own zone.
    assert "没有**任何来源提供目的地时区" in markdown


def test_a_known_destination_timezone_is_named_but_not_applied():
    """When a source supplies the zone it is stated; the times are still not converted."""
    forecast = WeatherForecast(
        location_name="杭州",
        coordinates=Coordinates(latitude=30.25, longitude=120.15),
        timezone="Asia/Shanghai",
        days=[],
        source_references=[],
    )
    trip = _trip(preview=_preview(weather=forecast), itinerary=_day_with_one_item())
    assert destination_timezone(trip) == "Asia/Shanghai"

    markdown = to_markdown(trip, generated_at=GENERATED)
    assert "Asia/Shanghai" in markdown
    # Not converted: the wall-clock time is unchanged and no UTC instant appears for it.
    assert "09:30–11:30" in markdown


def test_a_plan_with_no_timezone_source_says_so_and_names_none():
    assert destination_timezone(_trip(preview=_preview())) is None


# -- Markdown: both times, for every source ------------------------------------------


def test_every_source_reference_lists_both_of_its_times_and_what_the_second_means():
    preview = _preview(
        references=[
            _reference("amap-weather", basis=ObservationBasis.PROVIDER_REPORTED),
            _reference("openstreetmap", basis=ObservationBasis.SOURCE_SILENT),
            _reference("osrm", basis=ObservationBasis.LIVE_COMPUTED),
        ]
    )
    markdown = to_markdown(_trip(preview=preview), generated_at=GENERATED)

    assert "抓取时间" in markdown
    assert "数据自身时间" in markdown
    # The basis is translated, not emitted as an enum value.
    assert "来源自报数据时间" in markdown
    assert "来源未提供数据时间" in markdown
    assert "本次请求实时计算" in markdown
    for raw in ("provider_reported", "source_silent", "live_computed"):
        assert raw not in markdown, f"the raw basis identifier {raw!r} reached the document"
    # The absolute fetch instant is printed, not only a relative age.
    assert "2026-09-28 10:00 UTC" in markdown


# -- The manufactured-verification guard ---------------------------------------------


def test_silent_sources_are_not_reported_as_verified_even_when_the_list_is_empty():
    """The bug this file exists for: an empty list is not evidence of verification.

    ``age_unknown_facts`` is ``[]`` here exactly as it would be for a snapshot written before
    the field existed, while every reference is silent about when its data was produced. The
    document must not conclude that all ages are knowable.
    """
    preview = _preview(references=[_reference(basis=ObservationBasis.SOURCE_SILENT)])
    assert preview.age_unknown_facts == [], "the fixture must exercise the empty-list case"

    markdown = to_markdown(_trip(preview=preview), generated_at=GENERATED)

    assert "全部标明了数据自身的时间" not in markdown, (
        "the export claimed every source stated its data time while the only reference was "
        "silent — a verification manufactured from an empty list"
    )
    # It must say the opposite, and say it in the section a reader consults for this.
    assert "无法确认数据年龄" in markdown
    assert "无法判断" in markdown
    assert sources_without_a_known_age(preview) == 1


def test_all_reported_bases_do_allow_the_stronger_statement():
    """The other direction, so the guard is not passing by refusing to ever claim anything."""
    preview = _preview(
        references=[
            _reference("amap-weather", basis=ObservationBasis.PROVIDER_REPORTED),
            _reference("amap-places", basis=ObservationBasis.PROVIDER_REPORTED),
        ]
    )
    markdown = to_markdown(_trip(preview=preview), generated_at=GENERATED)
    assert "全部标明了数据自身的时间" in markdown
    assert "无法确认数据年龄" not in markdown
    assert sources_without_a_known_age(preview) == 0


def test_an_empty_stale_list_never_reads_as_everything_being_recent():
    """Zero stale findings must not be presented as "everything is current"."""
    preview = _preview(references=[_reference(basis=ObservationBasis.SOURCE_SILENT)])
    markdown = to_markdown(_trip(preview=preview), generated_at=GENERATED)
    assert "不意味着全部数据都被核实过" in markdown


def test_a_stale_fact_is_reported_with_its_three_times():
    """A stale entry is useless without the times that make it judgeable later."""
    finding = {
        "data_type": "weather",
        "source": "amap-weather",
        "source_id": "amap:weather:110000",
        "observed_at": "2026-09-20T00:00:00+00:00",
        "fetched_at": "2026-09-28T10:00:00+00:00",
        "evaluated_at": "2026-09-28T12:00:00+00:00",
        "reason": "too_old",
        "detail": "数据 X 早于可接受范围",
    }
    preview = _preview(
        references=[_reference("amap-weather", basis=ObservationBasis.PROVIDER_REPORTED)],
        stale=[finding],
    )
    markdown = to_markdown(_trip(preview=preview), generated_at=GENERATED)
    assert "已超出可接受范围" in markdown
    assert "2026-09-20T00:00:00+00:00" in markdown
    assert "2026-09-28T10:00:00+00:00" in markdown
    assert "2026-09-28T12:00:00+00:00" in markdown


def test_a_plan_with_no_sources_at_all_says_so_rather_than_staying_silent():
    markdown = to_markdown(_trip(preview=_preview()), generated_at=GENERATED)
    assert "没有来源引用" in markdown


def test_provider_text_cannot_reshape_the_table():
    """A pipe or newline in a provider's own string must not become table structure."""
    item = ItineraryItem(
        sequence=1,
        item_type=ItineraryItemType.ATTRACTION,
        title="西湖 | 断桥\n第二行",
        start_time=datetime(2026, 10, 3, 9, 30),
        end_time=datetime(2026, 10, 3, 11, 30),
        visit_duration_minutes=120,
    )
    itinerary = Itinerary(
        days=[ItineraryDay(day_date=date(2026, 10, 3), day_index=1, items=[item])],
    )
    markdown = to_markdown(_trip(itinerary=itinerary), generated_at=GENERATED)
    row = next(line for line in markdown.splitlines() if "西湖" in line)
    assert "\\|" in row
    assert "\n" not in row
    # Five columns, so five separators: the injected pipe did not add one.
    assert row.count("|") - row.count("\\|") == 6


# -- Budget --------------------------------------------------------------------------


def test_the_estimate_label_comes_from_the_flag_not_from_the_amount():
    """A round number is not evidence of an estimate, and an odd one is not evidence of a fact."""
    budget = BudgetBreakdown(
        items=[
            BudgetItem(category="hotel", amount=Decimal("1234.56"), is_estimate=False),
            BudgetItem(category="miscellaneous", amount=Decimal("0"), is_estimate=True),
        ],
        total=Decimal("1234.56"),
        budget_limit=Decimal("5000"),
        remaining=Decimal("3765.44"),
        over_budget=False,
    )
    markdown = to_markdown(_trip(budget=budget), generated_at=GENERATED)

    assert "住宿" in markdown, "the category identifier should be shown in Chinese"
    assert "其他" in markdown
    sourced, estimated = [
        line
        for line in markdown.splitlines()
        if line.startswith("| 住宿") or line.startswith("| 其他")
    ]
    assert "有来源" in sourced
    assert "估算（无来源数据）" in estimated


def test_a_budget_with_no_items_adds_no_empty_section():
    markdown = to_markdown(_trip(), generated_at=GENERATED)
    assert "## 预算" not in markdown


# -- iCalendar: structure that fails silently in a client ----------------------------


def test_the_calendar_uses_crlf_and_ends_with_one():
    """RFC 5545 requires CRLF; a client given bare LF may reject the file outright."""
    ics = to_ics(_trip(itinerary=_day_with_one_item()), generated_at=GENERATED)
    assert "\r\n" in ics
    assert ics.endswith("\r\n")
    # No bare LF anywhere: every LF is preceded by CR.
    assert ics.replace("\r\n", "").count("\n") == 0


def test_no_line_exceeds_seventy_five_octets():
    """Over-long lines are dropped silently by clients rather than reported."""
    ics = to_ics(_trip(itinerary=_day_with_one_item()), generated_at=GENERATED)
    for index, line in enumerate(ics.split("\r\n")):
        width = len(line.encode("utf-8"))
        assert width <= 75, f"line {index} is {width} octets"


def test_folding_round_trips_exactly_and_counts_octets():
    """The fold must never split a character.

    The first version sliced encoded bytes and decoded each chunk with ``errors="ignore"``,
    which silently deleted the characters straddling a boundary: the calendar still parsed and
    the events still appeared, but text was missing. Chinese is three bytes per character, so
    this happens constantly rather than rarely.
    """
    for repeats in (1, 5, 20, 60, 200):
        line = "DESCRIPTION:" + "数据来源未提供时间，无法判断数据新旧。" * repeats
        folded = _ics_fold(line)
        parts = folded.split("\r\n")
        assert max(len(part.encode("utf-8")) for part in parts) <= 75
        assert all(part.startswith(" ") for part in parts[1:]), (
            "every continuation line must begin with a space"
        )
        assert folded.replace("\r\n ", "") == line, (
            f"unfolding the fold at {repeats} repeats did not restore the original text"
        )


def test_a_short_line_is_not_folded():
    assert _ics_fold("SUMMARY:西湖") == "SUMMARY:西湖"


def test_itinerary_times_are_written_as_floating_time():
    """Floating time is how RFC 5545 says "this wall clock, zone unspecified".

    The itinerary is stored naive in destination local time. A trailing ``Z`` would assert UTC
    and move every event by the destination's offset; a ``TZID`` would assert a zone nothing in
    the plan established.
    """
    ics = to_ics(_trip(itinerary=_day_with_one_item()), generated_at=GENERATED)
    for line in ics.split("\r\n"):
        if line.startswith(("DTSTART", "DTEND")):
            assert line.rstrip().endswith(("093000", "113000")), line
            assert not line.rstrip().endswith("Z"), f"a wall-clock time was written as UTC: {line}"
            assert "TZID" not in line, f"a zone was asserted: {line}"
    # DTSTAMP is a real instant and must be UTC, which is how the two differ.
    assert "DTSTAMP:20260928T120000Z" in ics


def test_the_disclaimer_is_on_every_event_not_only_on_the_calendar():
    """Events get forwarded individually, so a calendar-level note does not travel with them."""
    item = ItineraryItem(
        sequence=1,
        item_type=ItineraryItemType.ATTRACTION,
        title="西湖",
        start_time=datetime(2026, 10, 3, 9, 30),
        end_time=datetime(2026, 10, 3, 11, 30),
        visit_duration_minutes=120,
    )
    second = item.model_copy(
        update={
            "sequence": 2,
            "title": "灵隐寺",
            "start_time": datetime(2026, 10, 3, 14, 0),
            "end_time": datetime(2026, 10, 3, 16, 0),
        }
    )
    itinerary = Itinerary(
        days=[ItineraryDay(day_date=date(2026, 10, 3), day_index=1, items=[item, second])]
    )
    ics = to_ics(_trip(itinerary=itinerary), generated_at=GENERATED)
    flat = ics.replace("\r\n ", "")
    events = flat.count("BEGIN:VEVENT")
    assert events == 2
    assert flat.count("是快照，不会自动更新") >= events
    assert flat.count("STATUS:TENTATIVE") >= events


def test_each_event_is_stable_across_re_exports():
    """A re-import should update the existing events rather than duplicate the trip."""
    itinerary = _day_with_one_item()
    trip = _trip(itinerary=itinerary)
    first = to_ics(trip, generated_at=GENERATED)
    second = to_ics(trip, generated_at=GENERATED + timedelta(hours=3))

    def uid_of(text: str) -> str:
        return next(line for line in text.split("\r\n") if line.startswith("UID:"))

    assert uid_of(first) == uid_of(second)
    # But the timestamp records the later export, so a client can tell it changed.
    assert "DTSTAMP:20260928T120000Z" in first
    assert "DTSTAMP:20260928T150000Z" in second


def test_the_calendar_footer_counts_only_what_it_can_know():
    """The ICS footer must not claim full coverage from an empty list either."""
    preview = _preview(references=[_reference(basis=ObservationBasis.SOURCE_SILENT)])
    ics = to_ics(_trip(preview=preview, itinerary=_day_with_one_item()), generated_at=GENERATED)
    flat = ics.replace("\r\n ", "")
    assert "每条来源的年龄都可以核实" not in flat
    assert "未提供数据自身时间" in flat


def test_an_estimated_leg_is_carried_into_the_calendar():
    """A limitation that only appears in the app version of the plan is a limitation hidden."""
    itinerary = _day_with_one_item(conflicts=["3 段路程按距离估算，不是实测路线"])
    ics = to_ics(_trip(itinerary=itinerary), generated_at=GENERATED)
    assert "3 段路程按距离估算" in ics.replace("\r\n ", "")


# -- The download name ---------------------------------------------------------------


def test_a_chinese_destination_produces_a_header_that_can_be_encoded():
    """The original implementation crashed here.

    HTTP header values are latin-1, and ``"杭".isalnum()`` is ``True``, so a sanitiser that
    stripped non-alphanumerics passed the Chinese straight through and the response raised
    ``UnicodeEncodeError`` — a 500 on exactly the trips this project is for.
    """
    header = content_disposition(_trip(), extension="md")
    header.encode("latin-1")  # must not raise
    assert "attachment" in header
    # RFC 5987 form carries the real name; the plain parameter stays ASCII as a fallback.
    assert "filename*=UTF-8''" in header
    assert "%E6%9D%AD%E5%B7%9E" in header  # 杭州
    plain = header.split('filename="', 1)[1].split('"', 1)[0]
    assert plain.isascii()
    assert plain.endswith(".md")


def test_a_destination_cannot_inject_a_header():
    """Quotes and newlines in user-supplied text must not terminate the header."""
    profile = _profile().model_copy(update={"destination": 'x"\r\nX-Evil: 1'})
    header = content_disposition(_trip(profile=profile), extension="ics")
    header.encode("latin-1")
    assert "\r" not in header and "\n" not in header
    encoded = header.split("filename*=UTF-8''", 1)[1]
    assert '"' not in encoded
    assert "%0D%0A" in encoded or "%0A" in encoded


def test_a_destination_with_no_ascii_at_all_still_yields_a_usable_fallback():
    profile = _profile().model_copy(update={"destination": "東京"})
    header = content_disposition(_trip(profile=profile), extension="md")
    plain = header.split('filename="', 1)[1].split('"', 1)[0]
    assert plain == "trip-2026-10-03.md"
