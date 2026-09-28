"""What a traveller's own content contributes, and what it must never claim.

Traver ships no data of its own, and the most detailed trip knowledge is often not in
any API: it is in a 小红书 note, a 抖音 comment thread, a 大众点评 review, a 携程
攻略. None of those platforms offers a public API, and scraping them would be both a
terms violation and a privacy problem. What is legitimate — and what people actually
do — is read a page and keep the useful part.

So this is an **ingestion** path rather than a provider: the traveller supplies text
they can already see, Traver turns it into structured facts, and every fact stays
labelled as a claim by a person.

The honesty rules are the whole design, because this is the one input that is
somebody's opinion rather than a measurement:

1. **A claim is not a measurement.** An extracted price is ``CONTENT_REFERENCE``, never
   ``COMMERCIAL_FACT``. It may inform a budget as an estimate; it may never overwrite a
   quoted rate.
2. **Every fact quotes its evidence.** ``evidence`` is the exact span of the input the
   fact came from, so a reader can check it in one glance instead of trusting an
   extractor. A fact that cannot quote itself is not emitted.
3. **Confidence describes extraction, not truth.** It says how sure we are that the
   text says this — never how sure we are that it is so. Those are different numbers
   and conflating them would launder an opinion into a fact.
4. **A missing value stays missing.** No price mentioned means no price, not zero. The
   failure mode of an eager extractor is to invent the number a reader expects.
5. **The publication date is usually absent, and that is reported as absent.** A note
   from 2019 and a note from yesterday look identical in a paste box, so the post's own
   date is carried when the page states one and marked unknown when it does not. Stale
   travel advice is worse than none: it is confidently wrong about prices and opening
   hours.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from travel_api.domain.models import (
    DataDomain,
    ObservationBasis,
    SourceReference,
    SourceType,
)


class ContentPlatform(StrEnum):
    """Where the text came from.

    Only the platform's *name* is recorded. Nothing here is scraped: the traveller
    supplies the text, so no request is ever made to these hosts and no login is ever
    held. The value exists so attribution can name where a claim originated, which is
    what lets a reader weigh it.
    """

    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    MEITUAN = "meituan"
    DIANPING = "dianping"
    CTRIP = "ctrip"
    TONGCHENG = "tongcheng"
    MAFENGWO = "mafengwo"
    ZHIHU = "zhihu"
    BILIBILI = "bilibili"
    WEBSITE = "website"
    PASTE = "paste"


#: Human labels for the platforms, used by the API and the console. Kept here rather
#: than in the Web app so adding a platform does not require two edits.
PLATFORM_LABELS: dict[str, str] = {
    ContentPlatform.XIAOHONGSHU: "小红书",
    ContentPlatform.DOUYIN: "抖音",
    ContentPlatform.MEITUAN: "美团",
    ContentPlatform.DIANPING: "大众点评",
    ContentPlatform.CTRIP: "携程",
    ContentPlatform.TONGCHENG: "同程",
    ContentPlatform.MAFENGWO: "马蜂窝",
    ContentPlatform.ZHIHU: "知乎",
    ContentPlatform.BILIBILI: "哔哩哔哩",
    ContentPlatform.WEBSITE: "网页",
    ContentPlatform.PASTE: "粘贴文本",
}


def platform_from_url(url: str | None) -> ContentPlatform:
    """Infer the platform from a hostname, defaulting to plain paste.

    Deliberately a lookup rather than a guess: an unrecognised host is ``WEBSITE``, not
    the nearest match, because mislabelling the origin of a claim would misrepresent
    how much weight it deserves.
    """
    if not url:
        return ContentPlatform.PASTE
    lowered = url.lower()
    for platform, hosts in _PLATFORM_HOSTS.items():
        if any(host in lowered for host in hosts):
            return platform
    return ContentPlatform.WEBSITE


#: Hosts per platform. Every value is a tuple, including the single-host ones — the
#: trailing comma is load-bearing. `("dianping.com")` is a *string*, so iterating it
#: yields single characters and `any(char in url)` matches nearly every URL there is;
#: written that way, every host after 大众点评 in this dict was mislabelled as 大众点评.
_PLATFORM_HOSTS: dict[ContentPlatform, tuple[str, ...]] = {
    ContentPlatform.XIAOHONGSHU: ("xiaohongshu.com", "xhslink.com"),
    ContentPlatform.DOUYIN: ("douyin.com", "iesdouyin.com"),
    ContentPlatform.MEITUAN: ("meituan.com", "meituan.net"),
    ContentPlatform.DIANPING: ("dianping.com",),
    ContentPlatform.CTRIP: ("ctrip.com", "trip.com"),
    ContentPlatform.TONGCHENG: ("ly.com", "17u.cn"),
    ContentPlatform.MAFENGWO: ("mafengwo.cn",),
    ContentPlatform.ZHIHU: ("zhihu.com",),
    ContentPlatform.BILIBILI: ("bilibili.com", "b23.tv"),
}


def platform_label_for(platform: ContentPlatform) -> str:
    """Human label for a platform, falling back to the raw value.

    A fallback rather than an exception: a stored ingestion whose platform was later
    removed from the enum should still render, and losing a label is a smaller failure
    than losing the note.
    """
    return PLATFORM_LABELS.get(platform, str(platform))


def parse_published_at(value: str | None) -> datetime | None:
    """Parse a page-supplied publication date, or ``None``.

    Exists because pages disagree about how to write a date: metadata is ISO, while a
    visible byline is often 2024年05月01日. Both are worth reading, and the alternative —
    rejecting the request — would mean a traveller could not save a note because the page
    used a Chinese date format.

    Returns ``None`` rather than the current time when the value is unusable. Defaulting
    to now would silently relabel old advice as current: the exact error the freshness
    layer exists to prevent, and here it would be the traveller's own material that was
    misrepresented.
    """
    if not value:
        return None
    text = value.strip()
    try:
        # Covers ISO-8601 including an offset, which is what page metadata carries.
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y年%m月%d日",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class FactKind(StrEnum):
    """The kinds of claim worth extracting from travel prose.

    A short list on purpose. Each kind has to earn its place by being both reliably
    recognisable in natural text and actionable in a plan; anything vaguer would produce
    a confident-looking label over a guess.
    """

    #: A stated amount, e.g. 人均 80 元 / 门票 60 / ¥120。A claim about a price, and
    #: priced as an estimate wherever it is used.
    PRICE = "price"
    #: A stated amount of time, e.g. 建议游玩 2 小时 / 排队 40 分钟。
    DURATION = "duration"
    #: A stated opening pattern, including the closed-day case that ruins itineraries.
    OPENING_HOURS = "opening_hours"
    #: Advice worth surfacing next to an entity: go early, bring cash, enter from the
    #: east gate.
    TIP = "tip"
    #: A negative signal: crowds, scams, closures, a queue not worth standing in.
    WARNING = "warning"
    #: A place named in the text. A *mention*, not a discovered POI: without
    #: coordinates it cannot be routed, so it is reported as something the source
    #: mentioned rather than added to the itinerary as a visit.
    PLACE_MENTION = "place_mention"


class ExtractionConfidence(StrEnum):
    """Bands, not decimals, for how sure the extractor is about its reading.

    Coarse bands because a specific number would imply a calibration this does not
    have. ``HIGH`` means the pattern is unambiguous (a currency symbol attached to a
    number); ``LOW`` means a pattern matched but the text could be read another way.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Numeric midpoint per band, for code that needs to sort or combine. Exposed as a
#: mapping rather than a method on the enum so the numbers stay in one visible place.
CONFIDENCE_VALUES: dict[str, float] = {
    ExtractionConfidence.HIGH: 0.8,
    ExtractionConfidence.MEDIUM: 0.55,
    ExtractionConfidence.LOW: 0.3,
}


class ExtractedFact(BaseModel):
    """One claim read out of the supplied text, with the text that supports it.

    The ``evidence`` field is not a nicety. An extractor that reports "人均 80 元" with
    no quote asks to be believed; one that also shows the sentence it read lets the
    traveller disagree in one second. Everything here is built so the second is
    possible.
    """

    model_config = ConfigDict(extra="forbid")

    kind: FactKind
    #: A short normalised statement, e.g. "人均约 80 元".
    text: str
    #: The exact substring of the input this was read from. Never synthesised.
    evidence: str
    #: Offset of ``evidence`` in the input, so a UI can highlight it.
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    #: Structured payload, kind-dependent. Kept as a dict because the shape differs per
    #: kind and a union of six models would not make callers any safer.
    value: dict[str, Any] = Field(default_factory=dict)
    confidence: ExtractionConfidence = ExtractionConfidence.MEDIUM
    #: What the extractor relied on, in plain language, e.g. "「元」紧跟数字". Shown to
    #: the traveller so the reading can be judged rather than merely accepted.
    reason: str = ""

    @property
    def confidence_value(self) -> float:
        return CONFIDENCE_VALUES.get(self.confidence, 0.3)


class IngestionRequest(BaseModel):
    """What the traveller hands over.

    ``published_at`` is supplied only when the page states the post's date. It is
    tempting to default it to "now" so every fact looks fresh; that would be the same
    defect the freshness work exists to remove, with the traveller's own advice as the
    victim.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    url: str | None = None
    title: str | None = None
    author: str | None = None
    #: The post's own publication date, when the page states one. Accepted as free text
    #: and parsed leniently, because pages write dates in several formats and refusing an
    #: unusual one would mean the traveller could not save their note at all.
    published_at: datetime | None = None

    @field_validator("published_at", mode="before")
    @classmethod
    def _read_page_date(cls, value: object) -> object:
        """Parse a date string, or turn an unusable one into ``None``.

        Note the direction of the fallback: an unparseable date becomes *absent*, never
        *now*. Treating a failed parse as "just published" is how an undated note from
        years ago would be presented as current advice.
        """
        if isinstance(value, str):
            return parse_published_at(value)
        return value


class Ingestion(BaseModel):
    """A stored ingestion: the supplied text, and what was read out of it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    owner_id: str | None = None
    platform: ContentPlatform
    platform_label: str
    url: str | None = None
    title: str | None = None
    author: str | None = None
    #: When we received the text. Always known, and always reported as such — never
    #: presented as the age of the advice.
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    #: The post's own date, when stated. ``None`` is a real answer meaning "the page did
    #: not say", and must be rendered that way rather than as "recent".
    published_at: datetime | None = None
    facts: list[ExtractedFact] = Field(default_factory=list)
    #: The supplied text, kept verbatim. Not decoration: without it the extracted facts
    #: are assertions rather than citations, because nobody can check them. It is also
    #: what lets a later extraction improve without the traveller pasting again.
    content_text: str = ""
    #: Character count of the supplied text, so a UI can show what was read.
    input_chars: int = 0
    #: Anything the traveller should know about the ingestion itself — a truncated
    #: input, an unusably short text, a platform whose page text carries no date.
    notes: list[str] = Field(default_factory=list)

    @property
    def observation_basis(self) -> ObservationBasis:
        """Which of the two times ``published_at`` would be, if it exists.

        With a stated date the source told us when the advice was written, so the age
        of the *content* is known. Without one, only the ingestion time is known and the
        advice could be any age at all — which is exactly the case that must not be
        presented as current.
        """
        if self.published_at is not None:
            return ObservationBasis.PROVIDER_REPORTED
        return ObservationBasis.SOURCE_SILENT

    def source_reference(self) -> SourceReference:
        """A reference for this ingestion, honest about which time it holds.

        ``observed_at`` is the post's own date when the page stated one and the
        ingestion moment otherwise — and ``observed_at_basis`` says which of the two
        happened, so the freshness layer can treat an undated note as unknown-age
        rather than as fresh.
        """
        basis = self.observation_basis
        observed = (
            self.published_at if basis is ObservationBasis.PROVIDER_REPORTED else self.ingested_at
        )
        return SourceReference(
            source=f"user-content:{self.platform}",
            source_type=SourceType.USER_PROVIDED,
            source_id=self.url or f"ingestion:{self.id}",
            observed_at=observed,
            fetched_at=self.ingested_at,
            # An ingested note never expires on a timer: it is a fixed piece of text, and
            # re-reading the same paste would not make it newer. Its *age* is what
            # matters, and that is judged by the freshness policy instead.
            expires_at=None,
            observed_at_basis=basis,
            request_context={
                "platform": str(self.platform),
                "title": self.title,
                "author": self.author,
                "fact_count": len(self.facts),
                "input_chars": self.input_chars,
                "published_at_known": self.published_at is not None,
            },
            data_version="user-content",
            # Low, and deliberately so. This is somebody's account of a trip, not a
            # measurement, and the number has to leave room for it being out of date or
            # simply wrong.
            confidence=0.4,
            # Every ingested reference is a content citation. Chosen here, in one place,
            # so it cannot drift into ``COMMERCIAL_FACT`` — which would let a blog
            # post's remembered price stand in for a quoted rate.
            domain=DataDomain.CONTENT_REFERENCE,
        )
