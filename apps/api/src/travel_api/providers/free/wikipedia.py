"""Attraction descriptions from Wikipedia/Wikidata (free, keyless, CC BY-SA 4.0).

The matching rule is deliberately narrow
----------------------------------------
This provider **never guesses** which article belongs to an attraction. It uses
only the explicit ``wikipedia`` / ``wikidata`` tags that an OSM mapper attached
to the element, because every name-based approach was measured to produce wrong
facts on real Chengdu data:

* ``火烈鸟`` (a flamingo enclosure) matched the article 紅鸛科 — the taxonomic
  family, not the enclosure.
* ``人民公园`` matched a disambiguation page describing parks in 海口, 广州,
  上海 and 天津 instead of Chengdu's.
* ``望江楼公园`` has no article at all despite nearby hits.
* Conversely ``成都永陵博物馆`` *legitimately* resolves to ``王建墓`` (永陵 is
  王建's tomb), so requiring the article's coordinates to match would have
  rejected a correct match.

No rule separates those cases reliably, so the rule is "follow the mapper's
explicit link, otherwise leave the description empty". Coverage is about 8% of
Chengdu attractions, and correctness is 100% on what it does cover — which is
the right trade for a fact-grounded system: an empty description is honest, a
wrong one is a fabrication.

Licence
-------
Wikipedia and Wikivoyage text is **CC BY-SA 4.0**: attribution is mandatory and
derived text must be shared alike. ``WIKIPEDIA_ATTRIBUTION`` is attached to
every fact produced here and is part of the mandatory attribution set.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from travel_api.domain.models import (
    Attraction,
    DataDomain,
    ProviderHealth,
    ProviderResult,
    SourceReference,
    SourceType,
    TripProfile,
)
from travel_api.providers.free.attribution import WIKIPEDIA_ATTRIBUTION
from travel_api.providers.free.http import FreeHttpClient

logger = logging.getLogger(__name__)

ZH_WIKIPEDIA_API = "https://zh.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

#: MediaWiki accepts 50 titles per query for normal users; 20 keeps requests
#: small and well inside the limit even when titles are long.
MAX_TITLES_PER_REQUEST = 20

#: Extracts are introductory prose only. A long extract would bloat every trip
#: payload, and the point is to identify the place, not to mirror the article.
EXTRACT_CHAR_LIMIT = 600


def normalize_wikipedia_tag(value: str) -> tuple[str | None, str | None]:
    """Split an OSM ``wikipedia`` tag into (language, title).

    The tag format is ``lang:Title`` (e.g. ``zh:王建墓``). A bare title has no
    language; the configured default is used in that case.
    """
    text = (value or "").strip()
    if not text:
        return None, None
    if ":" in text:
        language, _, title = text.partition(":")
        language = language.strip().lower()
        title = title.strip()
        # Guard against a title that legitimately contains a colon but no
        # language prefix (e.g. "Category:Foo" style values are not attractions).
        if language and title and len(language) <= 12 and language.isalpha():
            return language, title
    return None, text


class WikipediaContentProvider:
    """Fills ``Attraction.description`` from explicitly linked articles."""

    def __init__(
        self,
        name: str = "wikipedia",
        *,
        http: FreeHttpClient,
        language: str = "zh",
        timeout_seconds: float = 20.0,
        ttl_seconds: int = 604800,
        enabled: bool = True,
        max_attractions: int = 200,
    ) -> None:
        self.name = name
        self.http = http
        self.language = language
        self.timeout_seconds = timeout_seconds
        #: Article text changes slowly; a week is a reasonable freshness window.
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        self.max_attractions = max_attractions

    async def enrich_attractions(
        self, attractions: list[Attraction], profile: TripProfile
    ) -> ProviderResult:
        if not self.enabled:
            return ProviderResult(provider=self.name, data=[])

        linked = _linked_attractions(attractions, self.max_attractions)
        if not linked:
            # No attraction carries an explicit link: success with no work done,
            # not an error. Most destinations have few such tags.
            return ProviderResult(provider=self.name, data=[])

        want_titles: dict[str, list] = {}
        want_qids: dict[str, list] = {}
        for item, link in linked:
            # Both tags feed the title list independently. OSM often carries both
            # (e.g. 永陵 has wikipedia=zh:王建墓 *and* wikidata=Q1540709), and
            # treating wikidata as the sole route meant that a QID with no zhwiki
            # sitelink silently discarded a working wikipedia tag.
            if link.wikipedia:
                language, title = normalize_wikipedia_tag(link.wikipedia)
                language = (language or self.language).lower()
                if language != "zh":
                    # Only the zh wiki is queried; a link pointing at another
                    # language is skipped rather than silently fetched in the
                    # wrong language.
                    continue
                if title:
                    want_titles.setdefault(title, []).append((item, link))
            if link.wikidata:
                want_qids.setdefault(link.wikidata, []).append((item, link))

        errors: list[str] = []
        references: list[SourceReference] = []
        resolved_titles: dict[str, tuple[str, str]] = {}

        # Wikidata first: it turns a QID into the correct article title, which is
        # how 杜甫草堂博物馆 (wikidata-only in OSM) still gets its text.
        if want_qids:
            try:
                sitelinks = await self._resolve_qids(list(want_qids))
            except Exception as exc:  # noqa: BLE001
                logger.warning("wikidata lookup failed: %r", exc)
                errors.append(f"Wikidata 查询失败：{type(exc).__name__}")
                sitelinks = {}
            for qid, title in sitelinks.items():
                want_titles.setdefault(title, []).extend(want_qids.get(qid, []))

        if want_titles:
            try:
                extracts = await self._fetch_extracts(list(want_titles))
            except Exception as exc:  # noqa: BLE001
                logger.warning("wikipedia extract lookup failed: %r", exc)
                errors.append(f"维基百科查询失败：{type(exc).__name__}")
                extracts = {}
            resolved_titles = extracts

        now = datetime.now(timezone.utc)
        by_attraction: dict = {}
        title_to_reference: dict[str, SourceReference] = {}

        for title, holders in want_titles.items():
            payload = resolved_titles.get(title)
            if not payload:
                continue
            text, url = payload
            if not text:
                continue
            reference = SourceReference(
                source=self.name,
                source_type=SourceType.OPEN_DATA,
                source_id=f"wikipedia:zh:{title}",
                observed_at=now,
                fetched_at=now,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
                request_context={
                    "title": title,
                    "url": url,
                    "language": "zh",
                    "attribution": WIKIPEDIA_ATTRIBUTION.as_credit(),
                },
                data_version="wikipedia-live",
                confidence=0.8,
                domain=DataDomain.CONTENT_REFERENCE,
            )
            references.append(reference)
            title_to_reference[title] = reference

            for attraction, _link in holders:
                by_attraction.setdefault(attraction.id, (text, url, reference))

        enriched: list[Attraction] = []
        for attraction in attractions:
            found = by_attraction.get(attraction.id)
            existing_context: dict = {}
            for reference in attraction.source_references:
                if isinstance(reference.request_context, dict):
                    existing_context.update(reference.request_context)
            updates: dict = {}
            if found:
                text, url, reference = found
                updates["description"] = text
                updates["wikipedia_url"] = url
                updates["source_references"] = [*attraction.source_references, reference]
            # Record which link was followed, and which ones were skipped, so the
            # decision is auditable instead of invisible.
            link_context = {
                key: existing_context[key]
                for key in ("wikipedia", "wikidata")
                if key in existing_context
            }
            if link_context:
                updates.setdefault("source_references", attraction.source_references)
                attached = _attach_link_context(updates["source_references"], link_context)
                updates["source_references"] = attached
            enriched.append(attraction.model_copy(update=updates) if updates else attraction)

        linked_contexts = _link_statistics(attractions)
        skipped = linked_contexts["linked"] - len(by_attraction)
        logger.info(
            "wikipedia enrichment: %d linked, %d described, %d skipped",
            linked_contexts["linked"],
            len(by_attraction),
            max(0, skipped),
        )

        return ProviderResult(
            provider=self.name,
            data=enriched,
            source_references=references,
            errors=errors,
        )

    async def _resolve_qids(self, qids: list[str]) -> dict[str, str]:
        """Map Wikidata QIDs to their Chinese Wikipedia article titles."""
        if not qids:
            return {}
        mapping: dict[str, str] = {}
        for chunk in _chunks(qids, 50):
            payload = await self.http.get_json(
                WIKIDATA_API,
                params={
                    "action": "wbgetentities",
                    "ids": "|".join(chunk),
                    "props": "sitelinks",
                    "sitefilter": "zhwiki",
                    "format": "json",
                },
            )
            entities = (payload or {}).get("entities") or {}
            for qid, entity in entities.items():
                title = ((entity.get("sitelinks") or {}).get("zhwiki") or {}).get("title")
                if title:
                    mapping[qid] = str(title)
        return mapping

    async def _fetch_extracts(self, titles: list[str]) -> dict[str, tuple[str, str]]:
        """Fetch intro extracts for many titles, batched and following redirects."""
        resolved: dict[str, tuple[str, str]] = {}
        for chunk in _chunks(titles, MAX_TITLES_PER_REQUEST):
            payload = await self.http.get_json(
                ZH_WIKIPEDIA_API,
                params={
                    "action": "query",
                    "prop": "extracts|info|pageprops",
                    "titles": "|".join(chunk),
                    "exintro": 1,
                    "explaintext": 1,
                    "exlimit": "max",
                    "inprop": "url",
                    "ppprop": "disambiguation",
                    "redirects": 1,
                    "format": "json",
                },
            )
            query = (payload or {}).get("query") or {}
            pages = query.get("pages") or {}

            # MediaWiki normalises and follows redirects; record the mapping so a
            # requested title still finds the article it was redirected to.
            redirect_targets = {
                str(item.get("from")): str(item.get("to"))
                for item in (query.get("redirects") or [])
            }
            normalised = {
                str(item.get("from")): str(item.get("to"))
                for item in (query.get("normalized") or [])
            }

            for page in pages.values():
                title = page.get("title")
                if not title:
                    continue
                props = page.get("pageprops") or {}
                if "disambiguation" in props:
                    # A disambiguation page is never the right target.
                    continue
                if "missing" in page:
                    continue
                text = _clean_extract(page.get("extract"))
                if not text:
                    continue
                url = page.get("fullurl") or (
                    f"https://zh.wikipedia.org/wiki/{str(title).replace(' ', '_')}"
                )
                resolved[str(title)] = (text, str(url))

            for original in chunk:
                if original in resolved:
                    continue
                target = redirect_targets.get(original) or normalised.get(original)
                if target and target in resolved:
                    resolved[original] = resolved[target]

        return resolved

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            status="healthy" if self.enabled else "disabled",
            latency_ms=0.0,
            detail="Wikipedia/Wikidata extracts (CC BY-SA 4.0)",
        )


class _Link:
    __slots__ = ("wikipedia", "wikidata")

    def __init__(self, wikipedia: str | None, wikidata: str | None) -> None:
        self.wikipedia = wikipedia
        self.wikidata = wikidata


def _linked_attractions(attractions: list[Attraction], limit: int) -> list[tuple]:
    """Find attractions that carry an explicit OSM wikipedia/wikidata link."""
    found: list[tuple] = []
    for attraction in attractions:
        wikipedia = wikidata = None
        for reference in attraction.source_references:
            context = reference.request_context
            if not isinstance(context, dict):
                continue
            wikipedia = context.get("wikipedia") or wikipedia
            wikidata = context.get("wikidata") or wikidata
        if wikipedia or wikidata:
            found.append((attraction, _Link(wikipedia, wikidata)))
        if len(found) >= limit:
            break
    return found


def _attach_link_context(references: list[SourceReference], link_context: dict):
    """Copy the followed link onto the attraction's newest reference.

    Kept so a reader can tell *why* a description exists, and — when it does not
    — that a link was present but produced no text.
    """
    if not references:
        return references
    head = references[0]
    context = dict(head.request_context) if isinstance(head.request_context, dict) else {}
    context.update(link_context)
    updated = head.model_copy(update={"request_context": context})
    return [updated, *references[1:]]


def _link_statistics(attractions: list[Attraction]) -> dict:
    linked = 0
    for attraction in attractions:
        for reference in attraction.source_references:
            context = reference.request_context
            if isinstance(context, dict) and (context.get("wikipedia") or context.get("wikidata")):
                linked += 1
                break
    return {"linked": linked, "total": len(attractions)}


def _clean_extract(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > EXTRACT_CHAR_LIMIT:
        text = text[: EXTRACT_CHAR_LIMIT - 1].rstrip() + "…"
    return text


def _chunks(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]
