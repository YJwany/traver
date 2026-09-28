"""The one place a name becomes a coordinate, and the one place it is refused.

Two sources can resolve a place name in this program — the free geocoders, and 高德 when a key
is configured — and they must make the same decision about what counts as a match. If each
implemented the check, the two would drift and the drift would be invisible: a deployment with
a key would accept a namesake that a deployment without one rejects, and nothing would say so.
So the *decision* lives here, once, and the providers differ only in how they obtain
candidates.

The name being located is used **exactly as the extractor produced it**. There is deliberately
no tidying step here, and the reason is worth stating because the tempting version of one is
wrong. ``_PLACE_PATTERN`` matches non-greedily up to the first place suffix, so
「推荐去灵隐寺看看」 already yields 灵隐寺 — a downstream trimmer would be fixing nothing. What
the extractor *does* get wrong is the other direction: 断桥残雪 yields 断桥 and 宽窄巷子 yields
宽窄巷, because 桥 and 巷 are themselves suffixes. That is an extraction limitation, it lives in
the extractor, and papering over it here with a second guess would leave two half-rules and no
way to tell which one produced a name. Both manifests state it as a caveat instead.

## How a candidate is chosen

Two ordered questions, and the order matters:

1. **Is it close enough to be the place in question?** Candidates beyond
   ``MAX_DISTANCE_FROM_DESTINATION_KM`` are excluded from the choice entirely. This is a filter
   rather than a preference, because the alternative — ranking by precision first and distance
   second across all candidates — lets a precise match in another province displace a good
   nearby one, and then the result is rejected as 在别处 even though a usable answer was in hand.

2. **Among those, which is the most *specific* thing?** 西湖 as a 风景名胜 and 西湖区 as a
   区县 are both legitimate matches for the same name, and the district's centroid is usually
   nearer the city centre than the scenic area is. Ranking by distance alone therefore answers
   the wrong question. ``precision`` is what separates them, and it is the reason
   :class:`PlaceCandidate` carries it separately from ``confidence``: "how sure the source is"
   and "how precise the thing it found is" are different facts, and a source can be certain
   about an area.

3. Distance, then ``(source, source_id)``, break ties — so the answer is stable across runs. A
   plan that changes without its inputs changing is not reproducible or debuggable.

## A limit stated rather than hidden

Two namesakes *both* inside the radius, with the same precision, cannot be told apart by this.
Distinguishing them needs a source that knows administrative parentage, which this stack does
not have without a credential. The caveat on both manifests says so, and the number is in
``MAX_DISTANCE_FROM_DESTINATION_KM`` where it can be argued with.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from travel_api.domain.geo import straight_line_km
from travel_api.domain.models import (
    Coordinates,
    DataDomain,
    ObservationBasis,
    PlaceResolutionStatus,
    ResolvedPlace,
    SourceReference,
    SourceType,
)

#: How far from the destination centre a result may be and still be accepted, in kilometres.
#:
#: Not a radius of the city — a radius of "the same trip". A destination's own extent is
#: typically under 60 km across, and an itinerary spread over 80 km is a different trip from
#: the one being planned. A namesake in another province is hundreds of kilometres away, so the
#: gap between the accepted and rejected cases is very wide and the exact value is not
#: delicate. That is the point of a threshold here rather than a distance-weighted score that
#: would need tuning and would still be a guess.
MAX_DISTANCE_FROM_DESTINATION_KM = 80.0

#: How many results to ask a source for. More than one so a destination-scoped lookup can fall
#: back to the next candidate when the first is out of area; not so many that a common name
#: turns into a dozen requests.
RESULT_LIMIT = 5

#: Precision bands, for "how specific is the thing this source found". Coarse bands because a
#: decimal would imply a calibration this does not have — the same reasoning as
#: ``ExtractionConfidence`` and the AMap/Nominatim confidence bands.
#:
#: The three values are the only ones a provider should need, and a provider that does not
#: recognise a source's type reports ``None`` rather than guessing a band.
PRECISION_SPECIFIC = 0.9  # a landmark, a building, a named point of interest
PRECISION_VENUE = 0.6  # a named business: a restaurant, a hotel, a station
PRECISION_AREA = 0.3  # an administrative area or a populated place: a real answer, a coarse one


@dataclass(frozen=True)
class PlaceCandidate:
    """One answer from one source, before any decision is made about it.

    A neutral shape so the free geocoders and 高德 can both be judged by the same code. Each
    provider converts its own result type into this; nothing else about a provider leaks into
    the decision.
    """

    source: str
    source_id: str
    latitude: float
    longitude: float
    #: The source's own confidence in the *match*, or ``None`` when it states none. ``None`` is
    #: not 0.5: inventing a number here would produce a figure that looks measured and is not,
    #: which is the defect this whole project keeps finding.
    confidence: float | None = None
    #: How precise the thing found is, on the bands above, or ``None`` when the source did not
    #: say. Used for choosing between candidates; ``confidence`` is used for the citation. A
    #: candidate with no stated precision sorts last among candidates of otherwise equal merit,
    #: which is the honest ranking for "we do not know what this is".
    precision: float | None = None
    display_name: str | None = None
    #: Extra fields for the citation, so a reader can see what was actually asked.
    detail: Mapping[str, object] = field(default_factory=dict)

    @property
    def coordinates(self) -> Coordinates:
        return Coordinates(latitude=self.latitude, longitude=self.longitude)


def _distance_km(candidate: PlaceCandidate, destination: Coordinates) -> float:
    return straight_line_km(
        (destination.latitude, destination.longitude), (candidate.latitude, candidate.longitude)
    )


def nearest(
    candidates: Sequence[PlaceCandidate], destination: Coordinates
) -> tuple[PlaceCandidate, float] | None:
    """The candidate closest to the destination, with its straight-line distance in km.

    Used for the rejection path, where the question is "how far away was the best answer we
    could find" — so it deliberately ignores precision. A precise match 900 km away and a coarse
    one 500 km away are both equally unusable, and the reported distance should be the smaller.
    """
    if not candidates:
        return None
    centre = (destination.latitude, destination.longitude)
    winner = min(
        candidates,
        key=lambda item: (
            straight_line_km(centre, (item.latitude, item.longitude)),
            # Ties broken deterministically: an unstable choice moves a stop in a day plan
            # between two identical requests.
            item.source,
            item.source_id,
        ),
    )
    return winner, straight_line_km(centre, (winner.latitude, winner.longitude))


def choose_within(
    candidates: Sequence[PlaceCandidate],
    destination: Coordinates,
    max_distance_km: float,
) -> PlaceCandidate | None:
    """The best candidate that is close enough to be the place in question, or ``None``.

    Ordering is precision first, then distance. Getting this backwards is the subtle version of
    the namesake bug: 西湖区's centroid is nearer 杭州's centre than 西湖风景名胜区 is, so a
    purely distance-ordered choice answers「哪个西湖离市中心近」 instead of「笔记说的是哪个西湖」.
    """
    in_area = [item for item in candidates if _distance_km(item, destination) <= max_distance_km]
    if not in_area:
        return None
    return min(
        in_area,
        key=lambda item: (
            # Negated so "more precise" sorts first. `None` becomes the lowest, not the
            # highest, because an unstated precision is not a claim of precision.
            -(item.precision if item.precision is not None else -1.0),
            _distance_km(item, destination),
            item.source,
            item.source_id,
        ),
    )


def any_within(
    candidates: Sequence[PlaceCandidate], destination: Coordinates, max_distance_km: float
) -> bool:
    """Whether any candidate is close enough to be worth keeping.

    Used to decide whether a fallback source still needs to be asked: a source that answered
    *somewhere else* has not answered the question.
    """
    return choose_within(candidates, destination, max_distance_km) is not None


def not_attempted(names: Sequence[str], reason: str) -> list[ResolvedPlace]:
    """One ``NOT_ATTEMPTED`` per name, all with the same stated reason.

    Shared by both resolvers because the situation is identical in both and the wording should
    not drift: no lookup could be made, so nothing was learned about any of these names.
    ``NOT_ATTEMPTED`` rather than ``NOT_FOUND`` on purpose — 「没有找到这个地方」 is a claim
    about the place, and in this situation nobody made it.
    """
    return [
        ResolvedPlace(name=raw, status=PlaceResolutionStatus.NOT_ATTEMPTED, reason=reason)
        for raw in names
    ]


def decide(
    name: str,
    candidates: Sequence[PlaceCandidate],
    *,
    destination: Coordinates,
    source_type: SourceType,
    max_distance_km: float = MAX_DISTANCE_FROM_DESTINATION_KM,
) -> ResolvedPlace:
    """Turn candidates into an outcome.

    ``destination`` is a real coordinate for this trip, always. A caller that cannot obtain one
    does not reach here — it reports ``NOT_ATTEMPTED`` for every name instead, because a
    distance check against a made-up origin would either reject every place on earth or accept
    every one, and both of those look like answers.
    """
    if not candidates:
        return ResolvedPlace(
            name=name,
            status=PlaceResolutionStatus.NOT_FOUND,
            reason="已查询地名来源，没有返回该名称的结果",
        )

    chosen = choose_within(candidates, destination, max_distance_km)
    if chosen is None:
        # Nothing was close enough. Report the nearest thing that *was* found and how far away
        # it is, rather than dropping it: the name exists, just not here, and that is a fact
        # about the note. Dropping it would look like the lookup failed; accepting it would put
        # a stop 900 km away into a day plan.
        match = nearest(candidates, destination)
        assert match is not None
        winner, distance_km = match
        return ResolvedPlace(
            name=name,
            status=PlaceResolutionStatus.ELSEWHERE,
            display_name=winner.display_name,
            match_confidence=winner.confidence,
            straight_line_from_centre_km=distance_km,
            reason=(
                f"找到的同名地点距离目的地约 {distance_km:.0f} 公里"
                f"（超过 {max_distance_km:.0f} 公里），很可能不是笔记所指的那个"
            ),
            source_reference=citation_for(
                winner,
                source_type=source_type,
                request_context={
                    "query": name,
                    "display_name": winner.display_name,
                    "distance_from_destination_km": distance_km,
                    "candidates": len(candidates),
                    **dict(winner.detail),
                },
                confidence=winner.confidence,
            ),
        )

    distance_km = _distance_km(chosen, destination)
    return ResolvedPlace(
        name=name,
        status=PlaceResolutionStatus.LOCATED,
        coordinates=chosen.coordinates,
        display_name=chosen.display_name,
        match_confidence=chosen.confidence,
        straight_line_from_centre_km=distance_km,
        reason="按距离目的地最近的结果选取（同名地点可能有多个）",
        source_reference=citation_for(
            chosen,
            source_type=source_type,
            request_context={
                "query": name,
                "display_name": chosen.display_name,
                "distance_from_destination_km": distance_km,
                "precision": chosen.precision,
                "candidates": len(candidates),
                **dict(chosen.detail),
            },
            confidence=chosen.confidence,
        ),
    )


def citation_for(
    candidate: PlaceCandidate,
    *,
    source_type: SourceType,
    request_context: Mapping[str, object],
    confidence: float | None,
) -> SourceReference:
    """A citation for one lookup.

    ``observed_at_basis`` is ``SOURCE_SILENT``, and that is the honest value rather than a
    shortcut. A gazetteer does not publish when it last rebuilt its index, so the only time
    known here is when *we* asked — which is exactly what ``SOURCE_SILENT`` names. Using
    ``LIVE_COMPUTED`` would claim the answer is as fresh as this request, which nobody can
    support for a name index.

    ``confidence`` is floored at a low-but-stated value rather than left ``None`` because
    ``SourceReference.confidence`` is a required float. The floor is reported as what it is: the
    citation says the lookup happened and we cannot say how good the match is.
    """
    now = datetime.now(timezone.utc)
    return SourceReference(
        source=candidate.source,
        source_type=source_type,
        source_id=candidate.source_id,
        observed_at=now,
        observed_at_basis=ObservationBasis.SOURCE_SILENT,
        fetched_at=now,
        # Administrative names and POIs change on the order of months; a month is a bound on how
        # wrong a stale coordinate can be, not a claim that it is exact.
        expires_at=now + timedelta(days=30),
        request_context=dict(request_context),
        data_version="geo-lookup",
        confidence=confidence if confidence is not None else 0.5,
        domain=DataDomain.GEO_FACT,
    )


__all__ = [
    "MAX_DISTANCE_FROM_DESTINATION_KM",
    "PRECISION_AREA",
    "PRECISION_SPECIFIC",
    "PRECISION_VENUE",
    "RESULT_LIMIT",
    "PlaceCandidate",
    "any_within",
    "choose_within",
    "citation_for",
    "decide",
    "nearest",
    "not_attempted",
]
