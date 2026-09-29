"""Export a plan as a document that can outlive its own freshness.

Every other view of a plan lives inside the application, next to the source chips, the
freshness panel and the capability matrix that explain it. An export does not. Once the file
is in a downloads folder or a calendar, there is no banner, no tooltip and no way to ask what
anything meant — the numbers are simply there, and they look exactly as authoritative a year
later as they did the minute they were written.

That makes the export the strongest case for the freshness rule rather than an exception to
it. Two consequences drive this module:

* **The document states when it was made, and that it will not change.** A plan is a snapshot;
  a file is a more permanent snapshot, and nothing in Markdown or iCalendar says so by default.
* **Every fact carries both of its times.** ``fetched_at`` answers "when did we ask",
  ``observed_at`` plus ``observed_at_basis`` answers "how old is the answer" — and for most of
  the free sources the second is genuinely unknown, which is stated rather than omitted.

Relative ages are always labelled as computed at export time. "3 分钟前" written into a file is
a falsehood waiting for tomorrow, and the roadmap already made this mistake once with
``stale_facts``: a stored relative time rots while sitting still, so the absolute times are
printed alongside and the relative one says what it is relative to.

## Two formats, two different jobs

**Markdown** is the full record: the itinerary, what it could not do, the budget and every
source reference with its times. It is meant to be read and kept — and that includes the
traveller's **own** material, in an appendix at the end. Those notes and the places they named
are the only part of a plan that cannot be fetched again: a data source will answer tomorrow,
a deleted post will not. An earlier version of this module reached every field of a preview
except those, which left the archive missing exactly the part that needs archiving.

**iCalendar** is the part that ends up on a phone. It deliberately does *not* try to be a
complete record — an event description that is a wall of text gets collapsed by every client.
Instead each event carries a compact footer: when the plan was generated, that it is a
snapshot, and how much of the data has a knowable age.

## Times are destination wall-clock, and the file says so

``route_optimizer`` builds ``start_time`` with ``datetime.combine(day_date, time(9, 30))`` —
a **naive** datetime, meaning 09:30 at the destination. It does not know the destination's
timezone, and inventing one would move every event by hours while still looking plausible.

So events are written as **floating time** (``DTSTART:20261003T093000``, no ``Z`` and no
``TZID``), which is RFC 5545's way of saying "this wall-clock time, timezone unspecified" —
which is exactly what is known. Converting to UTC would require the destination's zone, and
``zoneinfo`` needs a tz database that the project does not declare as a dependency, so the
same plan would export differently on different machines. A file whose meaning depends on
which machine produced it is worse than one that says what it does not know.

The cost of floating time is real and is disclosed: a client renders it in *its own* local
zone, so a plan for 东京 imported on a phone set to 伦敦 shows 09:30 London time. Where a
destination timezone is known it is named in the calendar description so a reader can
interpret the wall-clock times, but the file never silently converts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from travel_api.domain.ingestion import CONFIDENCE_LABELS, FACT_KIND_LABELS
from travel_api.domain.models import (
    ITEM_TYPE_LABELS,
    PACE_LABELS,
    PLACE_STATUS_LABELS,
    TRAVEL_MODE_LABELS,
    SourceReference,
    Trip,
)

#: What ``observed_at_basis`` means, in a sentence a reader can act on. The distinction is the
#: whole point of tracking it, and a bare enum name in an exported file communicates nothing.
BASIS_LABELS: dict[str, str] = {
    "provider_reported": "来源自报数据时间",
    "live_computed": "本次请求实时计算",
    "source_silent": "来源未提供数据时间",
    "unknown": "未分类",
}

#: Chinese wording for budget categories. ``BudgetItem.category`` is a free-form string rather
#: than an enum, so this table cannot be checked exhaustively against a member list the way
#: ``ITEM_TYPE_LABELS`` can — ``budget_engine`` is what decides the vocabulary, and every
#: category it can emit is named here. Anything it adds later falls back to the raw identifier,
#: which is a visible English word in a Chinese table rather than a wrong translation.
BUDGET_CATEGORY_LABELS: dict[str, str] = {
    "hotel": "住宿",
    "food": "餐饮",
    "attraction": "门票 / 景点",
    "transport": "交通",
    "miscellaneous": "其他",
}

#: What ``is_estimate`` looks like in prose. Printed from the flag, never inferred from the
#: amount looking round, because an estimate that happens to be exact would then read as sourced.
BUDGET_BASIS_LABELS: dict[bool, str] = {
    True: "估算（无来源数据）",
    False: "有来源",
}


def _as_utc(moment: datetime) -> datetime:
    """Naive timestamps are read as UTC, matching ``SourceReference.data_age_seconds``.

    Providers are expected to send aware values; this exists so one naive value cannot make
    an export fail or, worse, silently shift by the export machine's offset.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return _as_utc(moment).strftime("%Y-%m-%d %H:%M UTC")


def _format_age(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} 秒"
    if seconds < 5400:
        return f"{seconds // 60} 分钟"
    if seconds < 172800:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def destination_timezone(trip: Trip) -> str | None:
    """The destination's IANA zone when some source supplied one.

    Progressive enhancement applied to an export: open-meteo echoes the zone it resolved and
    the geocoder may carry one, so a plan with weather usually knows this and a plan without
    does not. ``None`` is therefore the ordinary case for a minimal configuration, not a
    failure — the export names the zone when it has it and never guesses when it does not.
    """
    if trip.preview is None:
        return None
    forecast = trip.preview.weather
    if forecast is not None and forecast.timezone:
        return forecast.timezone
    return None


def _source_lines(trip: Trip) -> list[str]:
    """One line per source reference, with both times and the basis between them."""
    if trip.preview is None:
        return []
    lines: list[str] = []
    for reference in trip.preview.source_references:
        lines.append(
            f"| `{reference.source}` | {reference.source_id} | "
            f"{_iso(reference.fetched_at)} | {_iso(reference.observed_at)} | "
            f"{BASIS_LABELS.get(reference.observed_at_basis.value, reference.observed_at_basis.value)} | "
            f"{_format_age(reference.data_age_seconds)} |"
        )
    return lines


def _escape_cell(value: str) -> str:
    """Make a value safe inside a Markdown table cell.

    A newline would end the row and a pipe would start a new column, so a provider's own text
    could otherwise reshape the document. Escaped rather than stripped, because dropping the
    character would quietly alter what the source said.
    """
    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def to_markdown(trip: Trip, *, generated_at: datetime | None = None) -> str:
    """The full record of a plan, as a document that explains its own limits.

    Nothing is summarised away. A reader who has only this file should be able to answer, for
    any number in it, where it came from and how old it was — which is the requirement in the
    user's own words (「什么时候拉取到的数据就标注为什么时候的」) applied to the one artifact
    that leaves the application.
    """
    generated = generated_at or datetime.now(timezone.utc)
    profile = trip.profile
    preview = trip.preview
    itinerary = trip.itinerary

    out: list[str] = []
    out.append(f"# {profile.destination} · {profile.start_date} — {profile.end_date}")
    out.append("")

    item_count = sum(len(day.items) for day in itinerary.days)
    out.append(
        f"> 由 Traver 于 **{_iso(generated)}** 生成，共 {len(itinerary.days)} 天、{item_count} 个行程项。"
    )
    out.append(">")
    out.append(
        "> 本文档是导出那一刻的快照，**其中的数字不会自动更新**。"
        "每条来源都同时标注了「抓取时间」（我们何时去问）与「数据自身时间」（数据何时产生）："
        "前者永远已知，后者不少来源并不提供。判断某个数字是否仍然成立时，请以这两者为依据。"
    )
    out.append("")
    out.append(f"- 日期：{profile.start_date} — {profile.end_date}")
    out.append(f"- 人数：{profile.travelers}")
    out.append(f"- 节奏：{PACE_LABELS.get(profile.pace.value, profile.pace.value)}")
    if profile.travel_mode:
        mode_label = TRAVEL_MODE_LABELS.get(profile.travel_mode.value, profile.travel_mode.value)
        out.append(f"- 要求的出行方式：{mode_label}（未指定时按节奏推导）")
    out.append("")

    zone = destination_timezone(trip)
    out.append("## 时间基准")
    out.append("")
    if zone:
        out.append(
            f"行程时间为**目的地当地时间**（本行程已知目的地时区为 `{zone}`，由天气来源提供）。"
            "文档不对时间做时区换算。"
        )
    else:
        out.append(
            "行程时间为**目的地当地时间**（如 09:30 指当地 09:30）。"
            "本行程**没有**任何来源提供目的地时区，因此文档不标注时区、也不做换算。"
        )
    out.append("")

    out.append("## 行程 / itinerary")
    out.append("")

    # Printed before the tables, not after: a reader takes the numbers first, so the caveat
    # has to arrive before them or it is decoration.
    disclosures = list(itinerary.conflicts)
    if disclosures:
        out.append("**本行程的已知限制（读下面的时间前请先看这里）：**")
        out.append("")
        for conflict in disclosures:
            out.append(f"- {conflict}")
        out.append("")

    if not itinerary.days:
        out.append("_没有可行的行程项。_")
        out.append("")

    for day in itinerary.days:
        out.append(f"### Day {day.day_index} · {day.day_date}")
        out.append("")
        if day.warnings:
            for warning in day.warnings:
                out.append(f"> {_escape_cell(warning)}")
            out.append("")
        if not day.items:
            out.append("_当日没有可行的项目。_")
            out.append("")
            continue
        out.append("| 时间 | 项目 | 类型 | 停留 | 与上一站的路程 |")
        out.append("| --- | --- | --- | --- | --- |")
        for item in day.items:
            kind = ITEM_TYPE_LABELS.get(item.item_type.value, item.item_type.value)
            travel = (
                "—"
                if item.sequence == 1 and item.travel_from_previous_minutes == 0
                else f"{item.travel_from_previous_minutes} 分钟"
            )
            out.append(
                f"| {item.start_time.strftime('%H:%M')}–{item.end_time.strftime('%H:%M')} "
                f"| {_escape_cell(item.title)} | {kind} "
                f"| {item.visit_duration_minutes} 分钟 | {travel} |"
            )
        out.append("")

    if preview is not None:
        out.extend(_markdown_freshness(preview))
        out.extend(_markdown_sources(trip))
    # Outside the preview guard on purpose. The budget is a field of the *trip*, and a trip
    # can carry one while its preview is absent — being unable to re-derive the plan is not a
    # reason to hide the numbers the traveller already has. Gating this on the preview dropped
    # the section silently, which is the same defect shape as a value no other part of the
    # program can produce.
    out.extend(_markdown_budget(trip))

    # Last, as an appendix, and that placement is a decision. Everything above comes from a
    # source Traver fetched; this comes from the traveller. Putting it first would put one
    # person's remembered prices ahead of the measured ones, which is the ordering the server
    # refuses everywhere else.
    out.extend(_markdown_notes(trip))
    out.extend(_markdown_places(trip))

    return "\n".join(out).rstrip() + "\n"


def _markdown_freshness(preview: Any) -> list[str]:
    """What is known to be too old, and what cannot be judged at all.

    Both, and separately. Printing only the stale list would leave an empty section that reads
    as "everything was checked and everything is current" on a plan where most sources never
    said when their data was produced.

    The claim in the all-clear case is derived from the **references themselves**, not from
    ``age_unknown_facts`` being empty. ``FreshnessService.age_is_known`` is a pure function of
    each reference's ``observed_at_basis``, and the reference table printed below already
    carries that column — so counting the knowable bases is a fact about the document, whereas
    an empty list is a fact about a field. The difference matters for the same reason
    ``AgeUnknownPanel`` refuses to treat a missing ``age_unknown_facts`` as "nothing to
    report": an older snapshot whose field is absent would otherwise export as fully verified.
    """
    out: list[str] = ["## 时效性 / freshness", ""]
    stale = preview.stale_facts or []
    unknown = preview.age_unknown_facts or []
    references = list(preview.source_references or [])
    # Derived from the references, so it cannot be manufactured by a missing key.
    knowable = [r for r in references if r.observed_at_basis.age_is_about_the_data]
    unknowable = [r for r in references if not r.observed_at_basis.age_is_about_the_data]

    if references and not unknowable:
        out.append(
            f"{len(references)} 条来源引用全部标明了数据自身的时间，"
            "因此每一条的年龄都可以核实（判定于导出时）。"
        )
        if not stale:
            out.append("")
            out.append("其中没有一条的数据自身时间超出该类事实可接受的范围。")
        out.append("")
    elif not references:
        out.append("本行程没有来源引用，因此无法判断任何数据的时效性。")
        out.append("")

    if stale:
        out.append(f"### 已超出可接受范围（{len(stale)} 项，不应作为当前信息使用）")
        out.append("")
        for finding in stale:
            out.append(
                f"- `{_escape_cell(str(finding.get('source', '')))}` · "
                f"{_escape_cell(str(finding.get('data_type', '')))}："
                f"{_escape_cell(str(finding.get('detail', '')))}"
            )
            out.append(f"  - 数据自身时间：{finding.get('observed_at', '未知')}")
            out.append(f"  - 抓取时间：{finding.get('fetched_at', '未知')}")
            out.append(f"  - 判定时间：{finding.get('evaluated_at', '未知')}")
        out.append("")

    if unknown or unknowable:
        # The audit's own list when it has one, because it already applied the per-data-type
        # horizon policy, and declining to judge a fact type with no horizon is deliberate —
        # deriving this section from the bases alone would report a holiday calendar as
        # "cannot confirm age" when the policy's position is that nothing should be claimed
        # about it. When the list is absent the reference bases are the fallback, so the
        # section still appears rather than the plan reading as fully verified.
        rows = list(unknown) or [{"source": reference.source} for reference in unknowable]
        out.append(f"### 无法确认数据年龄（{len(rows)} 项）")
        out.append("")
        by_source: dict[str, int] = {}
        for finding in rows:
            key = str(finding.get("source", "未知来源"))
            by_source[key] = by_source.get(key, 0) + 1
        for source, count in sorted(by_source.items()):
            out.append(f"- `{_escape_cell(source)}`：{count} 条")
        out.append("")
        if knowable:
            out.append(f"另有 {len(knowable)} 条来源标明了数据自身的时间。")
            out.append("")
        out.append(
            "这些来源没有说明数据是什么时候产生的，因此只知道抓取时间，不知道数据本身有多新。"
            "**这不等于数据过期，也不等于数据是新的**——只等于无法判断。"
        )
        if not stale:
            out.append("")
            out.append(
                "本行程没有「数据自身时间已超出可接受范围」的条目；这只意味着没有任何一条"
                "能被证明过期，不意味着全部数据都被核实过。"
            )
        out.append("")

    return out


def _markdown_budget(trip: Trip) -> list[str]:
    """The trip's budget, preferring whichever copy actually has items in it.

    Two places can hold one — the trip itself and the preview it was generated from — and a
    plain ``trip.budget or preview.budget`` is wrong because a ``BudgetBreakdown`` is a
    Pydantic model and therefore always truthy, so an empty trip-level budget would shadow a
    populated preview one and print nothing.
    """
    candidates = [trip.budget]
    if trip.preview is not None:
        candidates.append(trip.preview.budget)
    budget = next((item for item in candidates if item is not None and item.items), None)
    if budget is None:
        return []
    out: list[str] = ["## 预算 / budget", ""]
    out.append("| 类别 | 金额 | 依据 |")
    out.append("| --- | --- | --- |")
    for item in budget.items:
        # "估算" comes from the flag, never inferred from the amount being round. An
        # estimate that happens to be exact would otherwise read as sourced.
        category = BUDGET_CATEGORY_LABELS.get(item.category, item.category)
        basis = BUDGET_BASIS_LABELS[bool(item.is_estimate)]
        out.append(f"| {_escape_cell(category)} | {item.amount} | {basis} |")
    out.append("")
    out.append(f"合计：{budget.total}")
    if budget.budget_limit is not None:
        out.append(
            f"预算上限：{budget.budget_limit}（{'已超预算' if budget.over_budget else '预算内'}）"
        )
    out.append("")
    if any(item.is_estimate for item in budget.items):
        out.append("标注为「估算」的金额没有来源数据支撑，是按可配置的估算额度得出的，不是报价。")
        out.append("")
    return out


def _element_age_lines(references: list[SourceReference]) -> list[str]:
    """The sources whose own members carry ages, spelled out as a distribution.

    A source like Overpass returns a *set*, and no single timestamp describes it: the snapshot
    is when OSM cut the database, and the elements inside were last edited years apart. Measured
    against the live service, 杭州西湖's snapshot was 1 minute old while the median element had
    last been edited 914 days earlier and 19 of 46 were over five years old.

    So the source table's 「数据自身时间」 column is true and nonetheless misleading about the
    facts, and this section is what makes the 时效性 requirement hold for these sources. Written
    as a sentence per source rather than a column, because the honest answer is a shape — "half
    of these are two years old and the oldest is from 2019" is the thing a reader can act on,
    and a single averaged number would destroy exactly that.
    """
    aged = [reference for reference in references if reference.element_age is not None]
    if not aged:
        return []

    out: list[str] = ["### 逐条数据的自身时间", ""]
    out.append(
        "以下来源返回的是一批各自独立编辑的记录。"
        "上表「数据自身时间」是整个批次的时间，描述的是我们拿到的这份副本，"
        "而不是其中任何一条记录；每条记录自己的时间见下："
    )
    out.append("")
    for reference in aged:
        element_age = reference.element_age
        assert element_age is not None  # narrowed by the filter above
        out.append(
            f"- `{reference.source}`（{reference.source_id}）共 {element_age.count} 条："
            f"中位 **{element_age.median_days} 天**，"
            f"最早一条最后修改于 **{element_age.oldest_at.date().isoformat()}**"
            f"（{round(element_age.oldest_seconds / 86400)} 天前）；"
            f"其中 {element_age.older_than_horizon} 条已超出该类事实可接受的 "
            f"{_format_age(element_age.horizon_seconds)}。"
        )
        if element_age.any_older_than_horizon:
            out.append(
                "  - 超出的条目未从计划中删除，因为删除会把「可能已经过时」说成「不存在」。"
                "它们仍作为候选列出，请按上表自行判断。"
            )
    if any(reference.element_age.older_than_horizon for reference in aged if reference.element_age):
        out.append("")
        # Deliberately *not* a claim about how old such snapshots usually are. The first draft of
        # this sentence read 「通常只有几分钟」, from 杭州 where the snapshot really was 1 minute
        # old — and it then printed directly beneath a row reading 「27 小时」, and 成都's mirror
        # served a 146-day-old snapshot. The magnitude belongs to one observation, not to the
        # source, so the sentence names the two things being distinguished and sizes neither.
        out.append(
            "「数据自身时间」与「逐条时间」是两件事：前者是整批数据的时刻，"
            "后者是记录本身。对于由社区长期编辑的来源（例如 OpenStreetMap），"
            "前者可以很新而后者仍然很久，本节的数字以后者为准。"
        )
    out.append("")
    return out


def _markdown_sources(trip: Trip) -> list[str]:
    preview = trip.preview
    if preview is None or not preview.source_references:
        return []
    out: list[str] = ["## 数据来源与时间 / sources", ""]
    out.append(
        "| 来源 | source id | 抓取时间 | 数据自身时间 | observed_at 的含义 | 数据年龄（导出时计算） |"
    )
    out.append("| --- | --- | --- | --- | --- | --- |")
    out.extend(_source_lines(trip))
    out.append("")
    out.append(
        "「抓取时间」是我们发出请求的时刻，永远已知。「数据自身时间」只有在该来源自报时才真的"
        "描述数据；若其含义是「本次请求实时计算」或「来源未提供」，那么上面的年龄指的是请求"
        "或未知，而不是数据的新旧。"
    )
    out.append("")
    out.extend(_element_age_lines(preview.source_references))
    return out


# -- the traveller's own material -------------------------------------------


def _markdown_notes(trip: Trip) -> list[str]:
    """What the traveller supplied, grouped by where it came from.

    This section existed as a hole. Every other field of a preview reached the document and the
    notes did not, which is the wrong way round: the notes are the **only** copy of material
    that has no API behind it. A 小红书 post can be deleted, a 抖音 video taken down, an account
    made private — the itinerary's POIs can always be fetched again from a data source, and this
    cannot. An export that omits it is the archive missing the irreplaceable part.

    Grouped by ingestion rather than listed flat, because the questions a reader has are about
    the *source* — who wrote it, when, and is the link still good — and those answers are the
    same for every fact from one page. Repeating the URL on each of a dozen rows would bury it.

    Every fact keeps its ``evidence``, the verbatim span it was read from, and the reason it was
    read that way. That is the whole point of quoting: a reader who disagrees with 「人均 80」 can
    see which sentence produced it and decide for themselves. A note printed without its source
    sentence is an assertion.
    """
    preview = trip.preview
    if preview is None:
        return []
    notes = list(preview.ingested_notes or [])
    if not notes:
        return []

    grouped: dict[str, list[dict]] = {}
    for note in notes:
        grouped.setdefault(str(note.get("ingestion_id", "")), []).append(note)

    out: list[str] = ["## 你提供的内容 / your notes", ""]
    out.append(
        f"共 {len(notes)} 条，来自 {len(grouped)} 篇你交进来的内容。"
        "这些是**某个人说的**，不是实测值：它们只作为提示与估算进入上面那份行程，"
        "没有覆盖任何来源的实测路线、报价或评分。"
    )
    out.append("")

    for index, (_, facts) in enumerate(grouped.items(), start=1):
        first = facts[0]
        title = first.get("title") or "（这篇内容没有标题）"
        out.append(f"### {index}. {_escape_cell(str(title))}")
        out.append("")
        out.append(f"- 平台：{_escape_cell(str(first.get('platform', '未知')))}")
        if first.get("author"):
            out.append(f"- 作者：{_escape_cell(str(first['author']))}")
        if first.get("url"):
            out.append(f"- 原文：{first['url']}")
        # Both times, always, and the missing one is stated rather than left blank. This is the
        # traveller's own material, so mis-stating its age would misrepresent them rather than a
        # provider: a note saved today from a 2019 post would read as current advice.
        if first.get("published_at"):
            out.append(
                f"- 原文发布时间：{str(first['published_at'])[:10]}"
                "（这是这段建议的年龄；页面自己写的）"
            )
        else:
            out.append("- 原文发布时间：**页面没有标注**（因此这段建议有多旧无法判断）")
        out.append(
            f"- 抓取时间：{_iso(_parse_moment(first.get('ingested_at')))}（我们把它存下来的时刻）"
        )
        out.append("")

        out.append("| 读到的内容 | 类型 | 识别把握 | 依据 | 原文片段 |")
        out.append("| --- | --- | --- | --- | --- |")
        for note in facts:
            kind = FACT_KIND_LABELS.get(
                str(note.get("kind", "")), str(note.get("kind", "未知类型"))
            )
            confidence = CONFIDENCE_LABELS.get(
                str(note.get("confidence", "")), str(note.get("confidence", ""))
            )
            out.append(
                f"| {_escape_cell(str(note.get('text', '')))} "
                f"| {_escape_cell(kind)} "
                f"| {_escape_cell(confidence)} "
                f"| {_escape_cell(str(note.get('reason', '')))} "
                f"| {_escape_cell(str(note.get('evidence', '')))} |"
            )
        out.append("")

        matched = [note for note in facts if note.get("matched")]
        if matched:
            names = sorted({str(note.get("matched_entity_name")) for note in matched})
            out.append(f"其中 {len(matched)} 条已关联到行程中的地点：{'、'.join(names)}。")
        else:
            out.append("这一篇里没有任何一条关联到行程中的实体。")
        out.append("")

    out.append(
        "「识别把握」说的是**我们读得准不准**，不是这件事是不是真的——"
        "「识别明确」意味着那句话按这个读法没有第二种解释，「仅为线索」意味着换一种读法也说得通。"
        "两者都不代表这个价格今天还成立。"
    )
    out.append("")
    return out


def _parse_moment(value: Any) -> datetime:
    """A stored ISO string back to a datetime, for printing only.

    The notes arrive as JSON dicts (``TripPreview.ingested_notes`` is deliberately untyped — see
    its docstring), so their timestamps are strings. Falls back to the epoch rather than raising:
    a malformed timestamp in one note must not cost the traveller the whole document.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _markdown_places(trip: Trip) -> list[str]:
    """Where the names in those notes are, for the ones our sources did not have.

    An appendix to the appendix, because these come from the same material. The section states
    the four outcomes in full rather than printing only the hits: a name that was looked up and
    is 950 km away is a fact about the note, and a name nobody looked for is a fact about this
    deployment. Both are things the reader can act on, and neither is "not found".

    Nothing here is a scheduled visit, and the section says so. A mention is evidence somebody
    liked a place; putting it on a day would be this tool deciding the traveller's trip for
    them, which is not a decision an export may make on their behalf.
    """
    preview = trip.preview
    if preview is None:
        return []
    places = list(preview.ingested_places or [])
    if not places:
        return []

    out: list[str] = ["## 文中提到的地点 / places your notes named", ""]
    out.append(
        "这些地名来自上面的内容，**不是查到的景点**：没有评分、票价或营业时间，"
        "也**没有被排进任何一天**。把它们落到坐标上，是为了让你知道那篇内容说的具体是哪儿——"
        "下面这张表里有定位到的、有同名地点在别处的，也有本次没有来源可查的，"
        "三种结果的下一步不一样，所以分开写。"
    )
    out.append("")
    out.append("| 地名 | 定位结果 | 来源给出的名称 | 坐标 | 距目的地中心（直线） | 判定依据 |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for place in places:
        status = PLACE_STATUS_LABELS.get(str(place.get("status", "")), str(place.get("status", "")))
        coordinates = place.get("coordinates") or {}
        if coordinates:
            position = (
                f"{coordinates.get('latitude'):.4f}, {coordinates.get('longitude'):.4f}"
                if coordinates.get("latitude") is not None
                else "—"
            )
        else:
            position = "—"
        distance = place.get("straight_line_from_centre_km")
        out.append(
            f"| {_escape_cell(str(place.get('name', '')))} "
            f"| {_escape_cell(status)} "
            f"| {_escape_cell(str(place.get('display_name') or '—'))} "
            f"| {position} "
            f"| {'—' if distance is None else f'{distance} 公里'} "
            f"| {_escape_cell(str(place.get('reason', '')))} |"
        )
    out.append("")

    located = [place for place in places if place.get("status") == "located"]
    elsewhere = [place for place in places if place.get("status") == "elsewhere"]
    unlooked = [place for place in places if place.get("status") == "not_attempted"]

    if located:
        out.append(
            f"{len(located)} 个已定位。同名地点（西湖在杭州、惠州、扬州都有）按「离目的地最近」"
            "消歧，所以一个定位结果是**一次判定**而不是一次查证："
            "上面那一列来源给出的名称就是被选中的那个，若与你所知的不符，以你的判断为准。"
        )
        out.append("")
    if elsewhere:
        out.append(
            f"{len(elsewhere)} 个在别处。这些名称确实存在，但离目的地很远，"
            "很可能不是那篇内容所指的那个。"
        )
        out.append("")
    if unlooked:
        out.append(
            f"{len(unlooked)} 个没有查过：本次规划没有可用的地名定位来源。"
            "**这不是「没有这个地方」，也不是「没有找到」**——是没有人去查。"
        )
        out.append("")
    out.append(
        "距离是**直线距离**（两点之间的大圆距离），只能回答「是不是同一个城市」，"
        "不能用来推算走过去要多久；本文档没有从它推出任何通行时间。"
        "地理编码的名称索引不公布自己的重建时间，所以这些结果只知道抓取时间，"
        "不知道索引本身有多新。"
    )
    out.append("")
    return out


# -- iCalendar --------------------------------------------------------------


def _ics_escape(value: str) -> str:
    """Escape a TEXT value per RFC 5545 §3.3.11.

    Backslash first, or escaping the others would then escape the escapes. Newlines become the
    literal ``\\n`` sequence, which is how a calendar client knows to show a line break.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _ics_fold(line: str) -> str:
    """Fold a content line to 75 **octets**, continuing with a leading space.

    Octets, not characters. Every Chinese character here is three bytes in UTF-8, so folding
    by character count produces lines well over the limit — and a client that rejects an
    over-long line drops the event rather than reporting anything.

    Characters are accumulated until the next one would not fit, so a fold **never lands inside
    a multi-byte sequence**. An earlier version sliced the encoded bytes and decoded each
    chunk with ``errors="ignore"``, on the reasoning that the following chunk would supply the
    rest — which is wrong, and silently destroyed text:

        expected  间，无法判断数据新旧。数据来源未提供时间
        got       间，无法判断数据新旧。数来源未提供时间

    据 and 新 were split across the boundary and dropped in both halves. The calendar still
    parsed, the event still appeared, and two characters were gone. Anything byte-sliced has
    to know where the characters are, which is why this walks the string instead.
    """
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks: list[str] = []
    current = ""
    # The first line takes 75 octets; every continuation carries a leading space, which counts
    # against the same limit, so it gets 74.
    limit = 75
    for character in line:
        if len((current + character).encode("utf-8")) > limit:
            chunks.append(current)
            current = character
            limit = 74
        else:
            current += character
    if current:
        chunks.append(current)
    return "\r\n".join([chunks[0], *(f" {chunk}" for chunk in chunks[1:])])


def _ics_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        out.extend(_ics_fold(line).split("\r\n"))
    return out


def _floating(moment: datetime) -> str:
    """``YYYYMMDDTHHMMSS`` with no trailing ``Z`` and no ``TZID``.

    Floating time is RFC 5545's representation of a wall-clock time whose zone is unknown,
    which is precisely the situation: the itinerary is stored naive, in destination local
    time. Appending ``Z`` would assert UTC and shift every event by the destination's offset;
    naming a ``TZID`` would assert a zone nothing in the plan established.
    """
    return moment.strftime("%Y%m%dT%H%M%S")


def _ics_utc(moment: datetime) -> str:
    return _as_utc(moment).strftime("%Y%m%dT%H%M%SZ")


def to_ics(trip: Trip, *, generated_at: datetime | None = None) -> str:
    """The plan as a calendar, carrying a compact statement of what it does not know.

    Each event is written as ``STATUS:TENTATIVE``. These are plans, not reservations — a
    confirmed status would assert that a table exists, which is the same class of claim as
    presenting an estimated travel time as a measured one.
    """
    generated = generated_at or datetime.now(timezone.utc)
    profile = trip.profile
    preview = trip.preview
    zone = destination_timezone(trip)

    stale = list(preview.stale_facts) if preview else []
    unknown = list(preview.age_unknown_facts) if preview else []
    references = list(preview.source_references) if preview else []
    # Counted from the bases rather than from the list, for the same reason as the Markdown
    # section: the bases are printed on the reference itself and cannot be absent.
    unknowable = [
        reference
        for reference in references
        if not reference.observed_at_basis.age_is_about_the_data
    ]

    # A short, factual footer, repeated on every event. Events get separated from their
    # calendar and forwarded individually, so the disclaimer has to be on the event itself.
    footer_bits = [
        f"由 Traver 于 {_iso(generated)} 生成，是快照，不会自动更新。",
        "时间为目的地当地时间"
        + (f"（已知时区 {zone}，但文档不做换算）" if zone else "（本行程未知目的地时区，不做换算）")
        + "。",
    ]
    if references:
        newest_fetch = max(reference.fetched_at for reference in references)
        # Names the counts only when they are known to be countable. Saying "全部来源都提供了
        # 数据自身时间" from an empty list would be the same manufactured verification the
        # Markdown section refuses to make.
        coverage = (
            f"其中 {len(unknowable)} 条未提供数据自身时间（只能确认抓取时间，无法判断数据新旧）。"
            if unknowable
            else "每条来源的年龄都可以核实。"
        )
        footer_bits.append(
            f"数据 {len(references)} 条来源，最新一次抓取于 {_iso(newest_fetch)}；{coverage}"
        )
        if unknown and len(unknown) != len(unknowable):
            footer_bits.append(
                f"按各类事实的可接受时长判定，其中 {len(unknown)} 条被列为无法判断。"
            )
    if stale:
        footer_bits.append(
            f"有 {len(stale)} 条数据自身时间已超出可接受范围，不应作为当前信息使用。"
        )
    if trip.itinerary.conflicts:
        footer_bits.append("本行程的已知限制：" + "；".join(trip.itinerary.conflicts) + "。")
    # Named on every event when any exists, because a calendar that silently omits material the
    # traveller supplied would look complete. The count is derived from the resolutions rather
    # than from the notes: a note that matched a POI is already represented by that POI's day.
    located = [
        place
        for place in ((preview.ingested_places or []) if preview is not None else [])
        if place.get("status") == "located"
    ]
    if located:
        footer_bits.append(
            f"另有 {len(located)} 个来自你自己笔记的地点已定位但未排进行程，详见 Markdown 导出。"
        )
    footer = " ".join(footer_bits)

    calendar_description = (
        footer + "\n\n行程时间为目的地当地时间，以「浮动时间」写入日历：客户端会按本机时区显示它，"
        "因此当本机时区与目的地不同时，显示的时刻不会等于当地时刻。"
    )

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Traver//Travel Planner//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(f'{profile.destination} {profile.start_date} 行程')}",
        f"X-WR-CALDESC:{_ics_escape(calendar_description)}",
    ]

    for day in trip.itinerary.days:
        for item in day.items:
            kind = ITEM_TYPE_LABELS.get(item.item_type.value, item.item_type.value)
            description = [
                f"{kind} · 停留 {item.visit_duration_minutes} 分钟",
            ]
            if item.travel_from_previous_minutes:
                description.append(f"与上一站的路程 {item.travel_from_previous_minutes} 分钟")
            if item.warnings:
                description.extend(item.warnings)
            description.append("")
            description.append(footer)

            lines.extend(
                [
                    "BEGIN:VEVENT",
                    # Stable across re-exports, so importing a refreshed plan updates the
                    # existing events instead of duplicating the whole trip.
                    f"UID:{item.id}@traver",
                    f"SEQUENCE:{trip.current_version}",
                    f"DTSTAMP:{_ics_utc(generated)}",
                    f"LAST-MODIFIED:{_ics_utc(trip.updated_at)}",
                    f"DTSTART:{_floating(item.start_time)}",
                    f"DTEND:{_floating(item.end_time)}",
                    f"SUMMARY:{_ics_escape(item.title)}",
                    f"DESCRIPTION:{_ics_escape(chr(10).join(description))}",
                    "STATUS:TENTATIVE",
                    "TRANSP:TRANSPARENT",
                    "END:VEVENT",
                ]
            )

    lines.append("END:VCALENDAR")
    # RFC 5545 requires CRLF, and a trailing newline after the final component.
    return "\r\n".join(_ics_lines(lines)) + "\r\n"


def sources_without_a_known_age(preview: Any) -> int:
    """How many references cannot be judged for age.

    Counted from the references' own ``observed_at_basis``, which is what
    ``FreshnessService.age_is_known`` tests — not from ``age_unknown_facts``, whose absence is
    indistinguishable from "nothing to report" for a caller holding parsed JSON.
    """
    if preview is None:
        return 0
    return sum(
        1
        for reference in (preview.source_references or [])
        if not reference.observed_at_basis.age_is_about_the_data
    )


def reference_summary(reference: SourceReference) -> str:
    """A one-line, both-times summary of a single reference."""
    return (
        f"{reference.source} · 抓取于 {_iso(reference.fetched_at)} · "
        f"数据自身时间 {_iso(reference.observed_at)} "
        f"（{BASIS_LABELS.get(reference.observed_at_basis.value, reference.observed_at_basis.value)}）"
    )


#: Longest ASCII slug kept in a download filename. Long enough for a city and a date, short
#: enough that no client truncates it into something unrecognisable.
_MAX_SLUG = 60


def filename_for(trip: Trip, *, extension: str) -> str:
    """A download name built from the destination, or ``None`` if it cannot be one."""
    return f"{trip.profile.destination}-{trip.profile.start_date}.{extension}"


def content_disposition(trip: Trip, *, extension: str) -> str:
    """An RFC 6266 ``Content-Disposition`` that survives a non-ASCII destination.

    Two things are easy to get wrong here, and the first crashed this endpoint outright.

    **HTTP header values are latin-1.** A destination like 杭州 cannot be encoded, so
    ``Response(headers={"Content-Disposition": ...})`` raised ``UnicodeEncodeError`` and the
    export returned 500 for exactly the trips this project is for. The old sanitiser replaced
    any character that was not alphanumeric — but ``"杭".isalnum()`` is ``True``, so it passed
    the Chinese straight through. The check has to be for *ASCII* alphanumerics.

    **Dropping the real name is not the fix either.** ``filename*=UTF-8''…`` (RFC 5987) is the
    standard way to carry a non-ASCII name, and every current browser prefers it over the
    plain ``filename``. So both are sent: the ASCII slug as the fallback for anything old, and
    the percent-encoded real name for everything else.

    Everything in the ``filename*`` value is percent-encoded (``safe=""``), which is also what
    makes header injection impossible: a quote or a newline in a user-supplied destination
    becomes ``%22`` or ``%0A`` rather than terminating the header.
    """
    real = filename_for(trip, extension=extension)
    slug = "".join(
        character if (character.isascii() and character.isalnum()) else "-"
        for character in trip.profile.destination
    ).strip("-")
    slug = slug[:_MAX_SLUG] or "trip"
    ascii_fallback = f"{slug}-{trip.profile.start_date}.{extension}"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(real, safe='')}"
