"""Merging the same place reported by several sources.

The interesting case is not the one where a source stays silent — it is the one where two
sources speak and *disagree*. The previous implementation only handled the first: it filled a
field in when the retained record lacked it, and when both records had a value it kept
whichever provider happened to be iterated first. Because the merged entity then carried both
sources' references, the result read as corroboration — a ticket price of ¥60 shown against
two sources, one of which had actually said ¥80.

Two things fix that, and both are needed:

* **Every claim is kept, not only the winning one.** A reader has to be able to see that the
  sources differed, which value each gave, and when each said it.
* **The winner is chosen by a stated rule, not by iteration order.** Recency, because this
  product treats the age of a fact as decisive. Ties break on source name so the outcome is
  deterministic: the same inputs always produce the same entity, which is what makes this
  testable and stops provider registration order from being a hidden input.

One consequence is worth stating plainly: **an entity with no disagreements is not a verified
entity.** It means every source that spoke happened to agree, or only one spoke, or none did.
Corroboration is evidence only when at least two sources are present, and the source list is
where that is visible.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import atan2, cos, radians, sin, sqrt
from typing import Any, TypeVar
from uuid import uuid4

from travel_api.domain.models import (
    Attraction,
    FieldClaim,
    FieldDisagreement,
    Hotel,
    Money,
    OpeningHours,
    Restaurant,
    SourceReference,
)
from travel_api.services.normalization import normalize_name

T = TypeVar("T")

#: How far apart two identically-named places may be and still be one place.
_MERGE_RADIUS_METERS = 500

#: Used when a claim carries no source reference and therefore no observation time. Set to
#: the earliest representable instant so an unattributed claim can never win on recency
#: against a claim that has a real timestamp — inventing "now" for it would make the least
#: attributable value look like the freshest one.
_NO_TIME = datetime.min.replace(tzinfo=timezone.utc)


def geo_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    d_lat, d_lon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * radius * atan2(sqrt(a), sqrt(1 - a))


def _same_entity(
    name_a: str, name_b: str, lat_a: float, lon_a: float, lat_b: float, lon_b: float
) -> bool:
    normalized_a, normalized_b = normalize_name(name_a), normalize_name(name_b)
    name_match = (
        normalized_a == normalized_b or normalized_a in normalized_b or normalized_b in normalized_a
    )
    return name_match and geo_distance_meters(lat_a, lon_a, lat_b, lon_b) <= _MERGE_RADIUS_METERS


def _same_place(item: Any, other: Any) -> bool:
    return _same_entity(
        item.canonical_name,
        other.canonical_name,
        item.location.coordinates.latitude,
        item.location.coordinates.longitude,
        other.location.coordinates.latitude,
        other.location.coordinates.longitude,
    )


# -- one rule per comparable field: how to compare it, and how it reads --------------


def _decimal_text(amount: Decimal) -> str:
    # normalize() can return exponent form (60 -> 6E+1), which is not what a price looks
    # like; "f" formats it back to plain notation.
    return format(amount.normalize(), "f")


def _money_key(value: Money) -> str:
    # Currency belongs in the identity of a price: ¥60 and $60 are two different facts,
    # not a disagreement about a number.
    return f"{value.currency}:{_decimal_text(value.value)}"


def _money_text(value: Money) -> str:
    symbol = "¥" if value.currency == "CNY" else f"{value.currency} "
    rendered = f"{symbol}{_decimal_text(value.value)}"
    # An estimate and a quote are different kinds of claim, and someone comparing two
    # sources needs to know which one they are reading.
    return f"{rendered}（估算）" if value.is_estimate else rendered


def _number_key(value: float | int) -> str:
    return f"{float(value):.4f}"


def _rating_text(value: float) -> str:
    return f"{float(value):.1f} 分"


def _review_count_text(value: int) -> str:
    return f"{int(value)} 条评价"


_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _opening_hours_key(value: Sequence[OpeningHours]) -> str:
    # Sorted, so the same schedule listed in a different order is still one schedule
    # rather than a spurious disagreement.
    return "|".join(
        sorted(
            f"{entry.weekday}:{entry.opens_at.strftime('%H:%M')}-"
            f"{entry.closes_at.strftime('%H:%M')}"
            for entry in value
        )
    )


def _opening_hours_text(value: Sequence[OpeningHours]) -> str:
    """A weekly schedule on one line, grouping only days that share a window.

    Grouping is presentation: days with different hours are never folded together, so the
    text cannot claim a uniformity the data does not have.
    """
    by_window: dict[str, list[int]] = {}
    for entry in value:
        window = f"{entry.opens_at.strftime('%H:%M')}–{entry.closes_at.strftime('%H:%M')}"
        by_window.setdefault(window, []).append(entry.weekday)

    segments: list[str] = []
    for window, weekdays in by_window.items():
        days = sorted({day for day in weekdays if 0 <= day <= 6})
        if len(days) == 7:
            label = "每天"
        elif days:
            label = "、".join(_WEEKDAY_LABELS[day] for day in days)
        else:
            label = "（未知星期）"
        segments.append(f"{label} {window}")
    return "；".join(segments)


@dataclass(frozen=True)
class _FieldSpec:
    """How to read, compare, render and write back one comparable field."""

    name: str
    get: Callable[[Any], Any]
    set: Callable[[Any, Any], None]
    key: Callable[[Any], str]
    render: Callable[[Any], str]


def _field(name: str, key: Callable[[Any], str], render: Callable[[Any], str]) -> _FieldSpec:
    return _FieldSpec(
        name=name,
        get=lambda item: getattr(item, name, None),
        set=lambda item, value: setattr(item, name, value),
        key=key,
        render=render,
    )


#: Comparable fields per entity kind. A field is listed here only when two sources can
#: meaningfully contradict each other about it and the difference changes a decision.
_SPECS: dict[type, tuple[_FieldSpec, ...]] = {
    Attraction: (
        _field("ticket_price", _money_key, _money_text),
        _field("opening_hours", _opening_hours_key, _opening_hours_text),
        _field("rating", _number_key, _rating_text),
    ),
    Restaurant: (
        _field("average_cost", _money_key, _money_text),
        _field("rating", _number_key, _rating_text),
        _field("review_count", _number_key, _review_count_text),
    ),
    Hotel: (
        _field("rating", _number_key, _rating_text),
        _field("review_count", _number_key, _review_count_text),
    ),
}


def specs_for(kind: type) -> tuple[_FieldSpec, ...]:
    """The comparable fields declared for an entity kind.

    Raises rather than returning an empty tuple, so adding a new kind to the merge
    without deciding what can be contradicted about it fails loudly.
    """
    try:
        return _SPECS[kind]
    except KeyError:  # pragma: no cover - a programming error, not a data condition
        raise TypeError(f"no comparable fields are declared for {kind.__name__}") from None


def _reference_for(item: Any) -> SourceReference | None:
    references = getattr(item, "source_references", None) or []
    return references[0] if references else None


def _claim_of(spec: _FieldSpec, item: Any) -> FieldClaim | None:
    """One source's claim about a field, or ``None`` when that source said nothing.

    ``None`` and an empty list both mean "this source did not say". A real zero is a
    ``Money`` with value 0, which is not ``None``, so "free" and "unknown" stay distinct.
    """
    value = spec.get(item)
    if value is None or (isinstance(value, (list, tuple)) and not value):
        return None

    reference = _reference_for(item)
    return FieldClaim(
        source=reference.source if reference else "未知来源",
        source_id=reference.source_id if reference else str(getattr(item, "id", uuid4())),
        value_text=spec.render(value),
        is_estimate=bool(getattr(value, "is_estimate", False)),
        observed_at=reference.observed_at if reference else _NO_TIME,
        fetched_at=reference.fetched_at if reference else _NO_TIME,
    )


def _choose(entries: Sequence[tuple[Any, FieldClaim]]) -> tuple[Any, FieldClaim]:
    """The retained claim.

    A value a source actually reported beats a value we derived, regardless of age: an
    estimate is our own inference, so comparing it against a real observation on recency
    is comparing incomparable things. When that does not separate them, newest observation
    wins, then newest fetch, then source name — the last purely so the result does not
    depend on provider registration order.
    """
    return max(
        entries,
        key=lambda entry: (
            not entry[1].is_estimate,
            entry[1].observed_at,
            entry[1].fetched_at,
            entry[1].source,
        ),
    )


def _disagreement_for(
    spec: _FieldSpec, entries: Sequence[tuple[Any, FieldClaim]]
) -> FieldDisagreement:
    chosen_value, chosen_claim = _choose(entries)
    timed = any(claim.observed_at != _NO_TIME for _, claim in entries)
    beat_an_estimate = chosen_claim.is_estimate is False and any(
        claim.is_estimate for _, claim in entries
    )
    if beat_an_estimate:
        reason = (
            "取来源实际报告的值而不是系统推算的估算值：估算不是证据，"
            "因此不参与「谁更新」的比较。这不代表其他来源是错的，也不代表差异已经解决。"
        )
    elif timed:
        reason = (
            f"取观测时间最新的一条（{chosen_claim.source}）：这条事实的时效性优先。"
            "这不代表其他来源是错的，也不代表差异已经解决。"
        )
    else:
        reason = (
            "各来源都没有给出这条事实的观测时间与可辨认的来源，因此按稳定顺序取一个结果；"
            "这个选择不代表它更可信。"
        )
    return FieldDisagreement(
        field=spec.name,
        claims=[claim for _, claim in entries],
        chosen_source=chosen_claim.source,
        chosen_value_text=chosen_claim.value_text,
        chosen_reason=reason,
    )


def _unique_references(references: Iterable[SourceReference]) -> list[SourceReference]:
    """De-duplicate by source and id, keeping the first."""
    unique: dict[str, SourceReference] = {}
    for reference in references:
        unique.setdefault(f"{reference.source}:{reference.source_id}", reference)
    return list(unique.values())


def _collect_claims(
    group: Sequence[Any], specs: Sequence[_FieldSpec]
) -> dict[str, list[tuple[Any, FieldClaim]]]:
    """Read every member's claims *before* any member is mutated.

    This ordering is load-bearing. Merging source references first would leave the primary
    holding another source's reference, and every claim read from it afterwards would be
    attributed to that other source — with that other source's timestamp, so the value
    would also win the recency comparison. The result is a wrong number wearing a confident
    label, which is worse than no number.

    A member is skipped for any field it already carries a finding about. Reading its scalar
    would credit the merged value to whichever reference happens to be listed first, which
    is how a nested merge would report that openstreetmap said ¥80 when openstreetmap
    actually said ¥60. These functions therefore expect freshly collected, single-source
    entities; this guard is what makes that expectation safe rather than merely assumed.
    """
    ledger: dict[str, list[tuple[Any, FieldClaim]]] = {spec.name: [] for spec in specs}
    for member in group:
        already_adjudicated = {finding.field for finding in member.disagreements}
        for spec in specs:
            if spec.name in already_adjudicated:
                continue
            claim = _claim_of(spec, member)
            if claim is not None:
                ledger[spec.name].append((spec.get(member), claim))
    return ledger


def _adjudicate(
    primary: Any,
    ledger: dict[str, list[tuple[Any, FieldClaim]]],
    specs: Sequence[_FieldSpec],
) -> list[FieldDisagreement]:
    """Write the best-evidence value for each field and report the genuine conflicts.

    Claims are gathered from every member before anything is decided, because a third
    source can change the outcome in either direction: if A says ¥60, B says ¥80 and C
    says ¥60, the retained value has to be able to move *back* to the ¥60 that A and C
    agree on. Deciding incrementally as members arrive cannot do that.
    """
    findings: list[FieldDisagreement] = []
    for spec in specs:
        entries = ledger[spec.name]
        if not entries:
            continue
        # The best-evidence claim is retained regardless of whether the values conflict.
        # Two sources can agree on the number while differing in what kind of claim it is
        # — a reported ¥60 and our own estimated ¥60 — and keeping the estimate when a
        # real report is available would be a downgrade for no reason.
        spec.set(primary, _choose(entries)[0])
        if len({spec.key(value) for value, _ in entries}) <= 1:
            # Agreement, or only one source spoke: nothing was discarded, so there is
            # nothing to disclose.
            continue
        findings.append(_disagreement_for(spec, entries))
    return findings


def _merge_all(entities: Sequence[T], specs: Sequence[_FieldSpec]) -> list[T]:
    """Group entities that describe the same place, then merge each group in place."""
    groups: list[list[T]] = []
    for item in entities:
        for group in groups:
            if _same_place(item, group[0]):
                group.append(item)
                break
        else:
            groups.append([item])

    canonical: list[T] = []
    for group in groups:
        primary = group[0]
        # Order matters twice over here. Claims are read first, while every member still
        # carries its own references. Findings the members arrived with are collected
        # before anything is mutated, so a nested merge cannot mistake its own output for
        # an incoming fact.
        ledger = _collect_claims(group, specs)
        inherited = [finding for member in group for finding in member.disagreements]
        for other in group[1:]:
            primary.aliases = sorted({*primary.aliases, other.canonical_name, *other.aliases})
            primary.source_references = [
                *primary.source_references,
                *other.source_references,
            ]
        primary.source_references = _unique_references(primary.source_references)

        fresh = _adjudicate(primary, ledger, specs)
        recomputed = {finding.field for finding in fresh}
        # The ledger saw every member, so for the fields it covers it is the complete
        # answer; inherited findings about other fields are still worth keeping.
        primary.disagreements = [
            *fresh,
            *(d for d in inherited if d.field not in recomputed),
        ]
        canonical.append(primary)
    return canonical


def deduplicate_hotels(hotels: list[Hotel]) -> list[Hotel]:
    return _merge_all(hotels, specs_for(Hotel))


def deduplicate_restaurants(restaurants: list[Restaurant]) -> list[Restaurant]:
    return _merge_all(restaurants, specs_for(Restaurant))


def deduplicate_attractions(attractions: list[Attraction]) -> list[Attraction]:
    """Merge attractions that describe the same place.

    Without this, de-duplicating by id alone keeps only the last provider's copy and
    silently drops every other source's reference, which destroys cross-source
    corroboration.
    """
    return _merge_all(attractions, specs_for(Attraction))
