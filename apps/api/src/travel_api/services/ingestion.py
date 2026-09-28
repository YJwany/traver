"""Reading structured facts out of travel prose, without pretending to understand it.

This is a **rule-based** extractor, and that is a design decision rather than a
limitation to be apologised for. Three reasons:

* It always works. A traveller who has plugged in no model still gets their note's
  prices, closed days and warnings pulled out — the same 渐进增强 rule the rest of the
  system follows. Requiring a model here would make the ingestion path exist only for
  people who had already configured one.
* Every rule states its own reason. ``ExtractedFact.reason`` says what the pattern
  relied on ("「元」紧跟数字"), so a wrong reading is diagnosable instead of mysterious.
  A model's extraction cannot offer that.
* It cannot fill a gap from imagination. A language model asked to "extract the facts"
  from a note will produce plausible prices that the note never contained; this
  produces nothing where the text says nothing, which is the behaviour a planning tool
  needs.

The extractor is deliberately conservative in one direction: it would rather miss a
fact than invent one, because a missed tip costs a traveller a little and a fabricated
price costs them money.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from travel_api.domain.ingestion import (
    ExtractedFact,
    ExtractionConfidence,
    FactKind,
    Ingestion,
    IngestionRequest,
    platform_from_url,
    platform_label_for,
)

#: Inputs longer than this are truncated rather than rejected. A pasted page can carry
#: hundreds of kilobytes of navigation markup, and scanning all of it would be slow
#: without finding anything the first stretch did not already contain — travel prose
#: lives at the top of a page. The truncation is reported in ``Ingestion.notes`` so it
#: is never silent.
MAX_INPUT_CHARS = 60_000

#: Below this, a *note about the input being short* is worth showing when nothing was
#: extracted. It is not a gate: "人均 80 元。" is eight characters and carries a real price,
#: so refusing to read short input would discard exactly the terse notes people paste.
SHORT_INPUT_CHARS = 12


# -- sentence segmentation -------------------------------------------------

#: Split on sentence-final punctuation and newlines, keeping the delimiter out of the
#: segments. Sentence granularity is the right unit for evidence: a shorter span would
#: lose the context that makes a claim readable, and a longer one would make the quote
#: useless for checking.
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _sentences(text: str) -> list[tuple[str, int]]:
    """``(sentence, offset)`` pairs, so a quote can point back into the input."""
    out: list[tuple[str, int]] = []
    offset = 0
    for raw in _SENTENCE_SPLIT.split(text):
        if not raw:
            continue
        out.append((raw, offset))
        offset += len(raw)
    return out


# -- patterns --------------------------------------------------------------

#: An amount is only recognised next to a currency marker. A bare number is not a price
#: — it is a date, a bus number, a floor count — and treating it as one is the classic
#: way an extractor invents a budget.
_PRICE_PATTERNS: tuple[tuple[re.Pattern[str], str, ExtractionConfidence], ...] = (
    # 「人均 80 元」/「人均 80-120 元」: a range is kept as a range rather than averaged,
    # because collapsing it would invent a number nobody stated.
    (
        re.compile(r"人均\s*[约大概]?\s*(\d+(?:\.\d+)?)\s*(?:[-~～至到]\s*(\d+(?:\.\d+)?))?\s*元?"),
        "「人均」后跟金额",
        ExtractionConfidence.HIGH,
    ),
    # 「门票 60 元」/「门票：60」
    (
        re.compile(r"(?:门票|票价|房费|房价|价格|收费)\s*[：:约为]?\s*(\d+(?:\.\d+)?)\s*元?"),
        "「门票/票价/房费」明确标出金额",
        ExtractionConfidence.HIGH,
    ),
    # A currency symbol before the number, the least ambiguous form.
    (
        re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)"),
        "「¥」直接连接数字",
        ExtractionConfidence.HIGH,
    ),
    # A number followed by a currency word. Medium, not high: 「走了 20 块」 is a
    # distance in some dialects, and this pattern cannot tell.
    (
        re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块|人民币)"),
        "数字后跟「元/块」，但未标明是什么费用",
        ExtractionConfidence.MEDIUM,
    ),
)

_DURATION_PATTERNS: tuple[tuple[re.Pattern[str], str, ExtractionConfidence], ...] = (
    (
        re.compile(
            r"(?:建议|推荐)?\s*(?:游玩|游览|参观|逛|停留|玩)\s*(?:时间)?\s*[约为]?\s*"
            r"(\d+(?:\.\d+)?)\s*(?:个)?\s*(小时|分钟)"
        ),
        "明确写出「游玩/停留」的时长",
        ExtractionConfidence.HIGH,
    ),
    (
        re.compile(r"(\d+(?:\.\d+)?)\s*(?:个)?\s*(小时)"),
        "数字后跟「小时」",
        ExtractionConfidence.MEDIUM,
    ),
    (
        re.compile(r"(\d+)\s*(分钟)"),
        "数字后跟「分钟」，但未说明是什么耗时",
        ExtractionConfidence.LOW,
    ),
)

#: Closed days matter more than opening times: a plan that schedules a museum on its
#: closed day is a wasted morning, and the note is where that fact usually lives.
_CLOSED_DAY = re.compile(r"(?:周|星期|礼拜)\s*([一二三四五六日天1-7])")
_CLOSED_VERB = re.compile(r"(闭馆|闭园|休息|不开放|不营业|停业|关门|休馆)")
_OPENING_HOURS = re.compile(
    r"(?:营业时间|开放时间|开门时间|开放时段)\s*[：:是为]?\s*([0-9]{1,2}\s*[:：点时][0-9]{0,2}"
    r"(?:\s*[-~～至到]\s*[0-9]{1,2}\s*[:：点时][0-9]{0,2})?)"
)

_TIP_MARKERS = re.compile(
    r"(建议|推荐|值得|一定要|最好|记得|注意|提醒|可以试|别忘|务必|小贴士|攻略|窍门)"
)
_WARNING_MARKERS = re.compile(
    r"(避坑|踩坑|避雷|坑人|宰客|骗|很坑|排队很|人特别多|人超级多|不要去|不推荐|"
    r"名不副实|商业化严重|涨价|维修|改造中|在装修)"
)

#: Place-name suffixes. Constraining on the suffix is what keeps this honest at all: a
#: Chinese place name has no capitalisation to detect, so without a suffix the extractor
#: would be guessing at arbitrary substrings. A suffix is evidence that the token is a
#: place, though only medium confidence — 「火锅店」 is a kind of shop, not a name.
_PLACE_SUFFIXES = (
    "博物馆",
    "美术馆",
    "纪念馆",
    "图书馆",
    "动物园",
    "植物园",
    "海洋馆",
    "水族馆",
    "古镇",
    "古城",
    "公园",
    "乐园",
    "温泉",
    "海滩",
    "沙滩",
    "码头",
    "步行街",
    "小吃街",
    "夜市",
    "市场",
    "广场",
    "大厦",
    "大楼",
    "寺",
    "庙",
    "塔",
    "宫",
    "湖",
    "山",
    "岛",
    "桥",
    "巷",
    "街",
    "馆",
    "园",
    "城",
    "湾",
    "坑",
    "瀑布",
    "峡谷",
    "湿地",
)

#: Words that cling to the front of a place name in prose. Stripping them turns
#: 「一定要去西湖」 into 「西湖」 instead of leaving the verb attached.
#:
#: 和 / 与 / 及 are deliberately absent: they are conjunctions *and* the first character
#: of real names (和平公园, 与谁同坐轩), so treating them as leading noise would corrupt
#: the names it was meant to clean.
_PLACE_PREFIXES = (
    "推荐去",
    "一定要去",
    "值得去",
    "可以去",
    "推荐",
    "一定要",
    "必去",
    "值得",
    "打卡",
    "安利",
    "去",
    "到",
    "逛",
    "看",
)

#: Characters that cannot appear inside a place name, used as cut points. Chinese has
#: no word boundaries, so a matched run often starts mid-sentence; splitting on these
#: and keeping the last segment is what recovers 「西湖」 from 「我昨天去了杭州西湖」.
#: Particles, pronouns, adverbs, motion verbs and punctuation only — nothing that
#: plausibly belongs to a name.
_PLACE_LEFT_STOP = (
    "的了在是我你他她它都也很就还又只把被给对向从跟同吗呢吧啊去到逛看 \t\r\n、，。！？；：!?;:"
)

_PLACE_SPLIT = re.compile("[" + re.escape(_PLACE_LEFT_STOP) + "]")

#: Phrases that end in a place suffix but are not names. 「闭馆」 ends with 馆, so
#: 「周一闭馆」 was reported as a place called 周一闭馆 — a sentence about a *closure*
#: turned into a destination. The rule is principled rather than a list of guesses: the
#: same verbs that mean "this place is shut" cannot be part of its name.
_PLACE_NOT_A_NAME = re.compile(
    r"(闭馆|闭园|休馆|休息|停业|关门|不开放|不营业|没有|不要|不值得|免费|票价|门票|时间|"
    r"附近|这边|那边|里面|外面|上面|下面|前面|后面)"
)

_PLACE_PATTERN = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{0,8}?(?:" + "|".join(_PLACE_SUFFIXES) + r")")


@dataclass(frozen=True)
class _Candidate:
    """A fact under construction, before deduplication."""

    fact: ExtractedFact


class ContentExtractionEngine:
    """Turns supplied text into facts, quoting the text that supports each one.

    Stateless and synchronous: extraction is CPU work over a bounded input, so a caller
    can run it inline without a task queue, and a test can call it directly.
    """

    def extract(self, request: IngestionRequest, *, ingestion_id: str) -> Ingestion:
        """Read ``request`` and return a stored-shaped :class:`Ingestion`.

        ``ingestion_id`` is passed in rather than generated here so the caller owns
        identity, which keeps this function pure with respect to the database.
        """
        text = request.text
        notes: list[str] = []
        if len(text) > MAX_INPUT_CHARS:
            original = len(text)
            text = text[:MAX_INPUT_CHARS]
            notes.append(
                f"输入共 {original} 字符，只读取前 {MAX_INPUT_CHARS} 字符。"
                "页面正文通常在前面，但更靠后的内容未被读取。"
            )

        platform = platform_from_url(request.url)
        ingestion = Ingestion(
            id=ingestion_id,
            platform=platform,
            platform_label=platform_label_for(platform),
            url=request.url,
            title=request.title,
            author=request.author,
            published_at=request.published_at,
            content_text=text,
            input_chars=len(text),
            notes=notes,
        )

        stripped = text.strip()
        if not stripped:
            ingestion.notes.append("没有收到任何文本。")
            return ingestion

        facts: list[ExtractedFact] = []
        for sentence, offset in _sentences(text):
            if not sentence.strip():
                continue
            facts.extend(self._price_facts(sentence, offset))
            facts.extend(self._duration_facts(sentence, offset))
            facts.extend(self._opening_facts(sentence, offset))
            facts.extend(self._advisory_facts(sentence, offset))
            facts.extend(self._place_facts(sentence, offset))

        ingestion.facts = self._deduplicate(facts)
        if not ingestion.facts:
            # Said out loud, because "nothing extracted" and "extraction failed" look
            # identical from the outside and only one of them is a problem.
            ingestion.notes.append(
                "这段文本里没有识别到价格、时长、开放时间、提示或地名。"
                "它仍然会被保存并作为来源引用，只是没有可结构化的内容。"
            )
            if len(stripped) < SHORT_INPUT_CHARS:
                # Offered as the likely explanation rather than as a verdict: a real note
                # can be short and simply contain nothing extractable.
                ingestion.notes.append("这段文本很短，可提取的内容有限。")
        if request.published_at is None:
            ingestion.notes.append(
                "页面没有提供这篇内容的发布时间，因此只能记录「我们何时收到它」，"
                "无法判断这些建议有多新。"
            )
        return ingestion

    # -- per-kind extraction ------------------------------------------------

    def _price_facts(self, sentence: str, offset: int) -> list[ExtractedFact]:
        out: list[ExtractedFact] = []
        for pattern, reason, confidence in _PRICE_PATTERNS:
            for match in pattern.finditer(sentence):
                low, high = self._amounts(match)
                if low is None:
                    continue
                value: dict[str, object] = {"currency": "CNY", "amount": low}
                if high is not None:
                    value["amount_max"] = high
                if "人均" in match.group(0):
                    value["per"] = "person"
                text = f"约 {low:g}–{high:g} 元" if high is not None else f"约 {low:g} 元"
                if value.get("per") == "person":
                    text = "人均 " + text.removeprefix("约 ")
                out.append(
                    ExtractedFact(
                        kind=FactKind.PRICE,
                        text=text,
                        evidence=sentence,
                        char_start=offset,
                        char_end=offset + len(sentence),
                        value=value,
                        confidence=confidence,
                        reason=reason,
                    )
                )
        return out

    @staticmethod
    def _amounts(match: re.Match[str]) -> tuple[float | None, float | None]:
        """First two numeric groups of a match, as floats, ignoring absent ones."""
        found: list[float] = []
        for group in match.groups():
            if group is None:
                continue
            try:
                found.append(float(group))
            except ValueError:
                continue
        if not found:
            return None, None
        if len(found) == 1:
            return found[0], None
        return found[0], found[1]

    def _duration_facts(self, sentence: str, offset: int) -> list[ExtractedFact]:
        out: list[ExtractedFact] = []
        for pattern, reason, confidence in _DURATION_PATTERNS:
            for match in pattern.finditer(sentence):
                amount = float(match.group(1))
                # Every pattern carries its own unit group. Inferring the unit from the
                # number of capture groups is how a "40 分钟" became 2400 minutes: the
                # bare-分钟 pattern has one group, so the missing unit was assumed to be
                # hours. The unit is now always stated by the pattern that matched.
                unit = match.group(2)
                minutes = amount if unit == "分钟" else amount * 60
                out.append(
                    ExtractedFact(
                        kind=FactKind.DURATION,
                        text=f"约 {minutes:g} 分钟",
                        evidence=sentence,
                        char_start=offset,
                        char_end=offset + len(sentence),
                        value={"minutes": minutes},
                        confidence=confidence,
                        reason=reason,
                    )
                )
        return out

    def _opening_facts(self, sentence: str, offset: int) -> list[ExtractedFact]:
        out: list[ExtractedFact] = []
        closed = _CLOSED_DAY.search(sentence) and _CLOSED_VERB.search(sentence)
        if closed:
            day = _CLOSED_DAY.search(sentence)
            verb = _CLOSED_VERB.search(sentence)
            assert day is not None and verb is not None
            out.append(
                ExtractedFact(
                    kind=FactKind.OPENING_HOURS,
                    text=f"周{day.group(1)}{verb.group(1)}",
                    evidence=sentence,
                    char_start=offset,
                    char_end=offset + len(sentence),
                    value={"closed_weekday": day.group(1), "closed": True},
                    confidence=ExtractionConfidence.HIGH,
                    reason="句子里同时出现「周X」与「闭馆/休息」类词",
                )
            )
        hours = _OPENING_HOURS.search(sentence)
        if hours:
            out.append(
                ExtractedFact(
                    kind=FactKind.OPENING_HOURS,
                    text=f"开放时间 {hours.group(1)}",
                    evidence=sentence,
                    char_start=offset,
                    char_end=offset + len(sentence),
                    value={"opening_hours": hours.group(1)},
                    confidence=ExtractionConfidence.MEDIUM,
                    reason="句子里明确写了「营业/开放时间」",
                )
            )
        return out

    def _advisory_facts(self, sentence: str, offset: int) -> list[ExtractedFact]:
        """Tips and warnings.

        A warning is only emitted for a sentence carrying a concrete negative signal.
        Generic sentiment ("不太好玩") is not enough: it is an opinion, and dressing it
        as a warning would give one stranger's mood the weight of a fact.
        """
        out: list[ExtractedFact] = []
        warning = _WARNING_MARKERS.search(sentence)
        if warning:
            out.append(
                ExtractedFact(
                    kind=FactKind.WARNING,
                    text=sentence.strip(),
                    evidence=sentence,
                    char_start=offset,
                    char_end=offset + len(sentence),
                    value={"trigger": warning.group(1)},
                    confidence=ExtractionConfidence.MEDIUM,
                    reason=f"出现负面信号词「{warning.group(1)}」",
                )
            )
            return out
        tip = _TIP_MARKERS.search(sentence)
        if tip:
            out.append(
                ExtractedFact(
                    kind=FactKind.TIP,
                    text=sentence.strip(),
                    evidence=sentence,
                    char_start=offset,
                    char_end=offset + len(sentence),
                    value={"trigger": tip.group(1)},
                    confidence=ExtractionConfidence.LOW,
                    reason=f"出现建议口吻的词「{tip.group(1)}」",
                )
            )
        return out

    def _place_facts(self, sentence: str, offset: int) -> list[ExtractedFact]:
        out: list[ExtractedFact] = []
        seen: set[str] = set()
        for match in _PLACE_PATTERN.finditer(sentence):
            name = self._trim_place(match.group(0))
            # Two characters is the shortest plausible Chinese place name (西湖), so a
            # single character is dropped as noise rather than reported as a place.
            if len(name) < 2 or name in seen:
                continue
            # A phrase saying a place is shut is not the place's name. Without this,
            # 「周一闭馆」 was reported as a destination.
            if _PLACE_NOT_A_NAME.search(name):
                continue
            seen.add(name)
            # A recommendation verb may be separated from the name by a motion verb:
            # 「推荐去灵隐寺」. Requiring them adjacent missed the most common phrasing.
            recommended = bool(
                re.search(
                    rf"(?:推荐|必去|一定要去|值得去|打卡|安利)\s*[去到逛]*\s*{re.escape(name)}",
                    sentence,
                )
            )
            out.append(
                ExtractedFact(
                    kind=FactKind.PLACE_MENTION,
                    text=name,
                    evidence=sentence,
                    char_start=offset,
                    char_end=offset + len(sentence),
                    value={"name": name, "recommended": recommended},
                    # A suffix is evidence of a place, never evidence that this trip
                    # should include it — hence LOW even when recommended, where the
                    # recommendation only says the *source* liked it.
                    confidence=ExtractionConfidence.LOW,
                    reason=(
                        f"以「{self._suffix_of(name)}」结尾，且句子里有推荐词"
                        if recommended
                        else f"以「{self._suffix_of(name)}」结尾"
                    ),
                )
            )
        return out

    @staticmethod
    def _trim_place(raw: str) -> str:
        """Recover the place name from the run of Chinese characters that matched.

        Two passes, because a Chinese place name has no capital letter or space to
        anchor on. First drop a leading recommendation verb, then split on characters
        that cannot occur inside a name and keep the last segment. The second pass is
        what turns 「我昨天去了杭州西湖」 into 「杭州西湖」 rather than leaving the whole
        clause as the name.
        """
        name = raw.strip()
        # Longest prefix first, so 「一定要去」 is not reduced to 「去」, which would leave
        # 「要去西湖」 behind.
        for prefix in sorted(_PLACE_PREFIXES, key=len, reverse=True):
            if name.startswith(prefix) and len(name) > len(prefix) + 1:
                name = name[len(prefix) :]
                break
        segments = [segment for segment in _PLACE_SPLIT.split(name) if segment]
        if segments:
            # The suffix is at the end of the run, so the name is in the final segment.
            name = segments[-1]
        # Punctuation and space only. 和 / 与 / 及 are deliberately not stripped here:
        # `str.strip` would happily remove them from 「和平公园」, which is how a name got
        # truncated to 「平公园」 by the very rule meant to clean it.
        return name.strip("、，。！？!?;:；： \t\r\n")

    @staticmethod
    def _suffix_of(name: str) -> str:
        for suffix in sorted(_PLACE_SUFFIXES, key=len, reverse=True):
            if name.endswith(suffix):
                return suffix
        return name[-1:]

    @staticmethod
    def _deduplicate(facts: list[ExtractedFact]) -> list[ExtractedFact]:
        """Drop repeats, keeping the most confident reading of each.

        The same sentence often matches several patterns (「人均 80 元」 matches both the
        「人均」 and the bare 「元」 rule). Reporting it twice would inflate the apparent
        richness of the source, which is the opposite of what a count of facts should do.
        """
        best: dict[tuple[str, str], ExtractedFact] = {}
        for fact in facts:
            key = (str(fact.kind), fact.evidence.strip())
            current = best.get(key)
            if current is None or fact.confidence_value > current.confidence_value:
                best[key] = fact
            elif (
                fact.confidence_value == current.confidence_value
                and fact.char_start < current.char_start
            ):
                best[key] = fact
        return sorted(best.values(), key=lambda item: (item.char_start, str(item.kind)))
