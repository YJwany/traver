"""Mandatory attribution for the open-data sources.

These are licence obligations, not documentation niceties:

* OpenStreetMap data (via Overpass, Nominatim and OSRM) is **ODbL 1.0** —
  attribution to OpenStreetMap contributors is required.
* Open-Meteo is **CC BY 4.0** and requires a visible link to open-meteo.com
  wherever its data is displayed.
* The OSRM demo server additionally asks for the FOSSGIS/OSM "fix the map" link.

The Web client renders these; keeping them in one module means a new provider
cannot ship without its credit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Attribution:
    provider: str
    text: str
    url: str
    licence: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    def as_credit(self) -> str:
        """One-line credit for ``SourceReference.request_context``.

        Must be a plain string, never the dataclass: ``request_context`` is
        persisted as a JSON column, so storing the object here fails at insert
        time with "Object of type Attribution is not JSON serializable". That
        only showed up in the persistence test, not in aggregation.
        """
        return f"{self.text} ({self.licence})"


OSM_ATTRIBUTION = Attribution(
    provider="openstreetmap",
    text="© OpenStreetMap contributors",
    url="https://www.openstreetmap.org/copyright",
    licence="ODbL 1.0",
)

OPEN_METEO_ATTRIBUTION = Attribution(
    provider="open-meteo",
    text="Weather data by Open-Meteo.com",
    url="https://open-meteo.com/",
    licence="CC BY 4.0",
)

OSRM_ATTRIBUTION = Attribution(
    provider="osrm",
    text="Routing: © OpenStreetMap contributors, OSRM",
    url="https://www.openstreetmap.org/fixthemap",
    licence="ODbL 1.0 / CC BY-SA",
)

#: MIT does not require attribution, but crediting the holiday database is both
#: good practice and useful to a reader checking where a date came from.
HOLIDAYS_ATTRIBUTION = Attribution(
    provider="holidays",
    text="Public holidays: vacanza/holidays",
    url="https://github.com/vacanza/holidays",
    licence="MIT",
)

#: Wikipedia/Wikivoyage text is CC BY-SA 4.0, which is stricter than ODbL: it
#: requires attribution *and* share-alike on derived text.
WIKIPEDIA_ATTRIBUTION = Attribution(
    provider="wikipedia",
    text="景点简介来自维基百科（CC BY-SA 4.0）",
    url="https://zh.wikipedia.org/",
    licence="CC BY-SA 4.0",
)

#: AMap is not open data — it is an official commercial API with its own terms.
#: It still requires a visible credit ("© 高德地图") wherever its data appears, so
#: it is served through the same endpoint. Listing it here rather than in the AMap
#: package keeps the rule that a new source cannot ship without its credit.
AMAP_ATTRIBUTION = Attribution(
    provider="amap",
    text="© 高德地图（AMap）",
    url="https://lbs.amap.com/",
    licence="高德开放平台服务条款",
)

#: Everything that must be displayed when free-provider data is shown.
OPEN_DATA_ATTRIBUTIONS: tuple[Attribution, ...] = (
    OSM_ATTRIBUTION,
    OPEN_METEO_ATTRIBUTION,
    OSRM_ATTRIBUTION,
    HOLIDAYS_ATTRIBUTION,
    WIKIPEDIA_ATTRIBUTION,
    AMAP_ATTRIBUTION,
)


def attribution_for(provider_name: str) -> Attribution | None:
    normalized = provider_name.strip().lower()
    for item in OPEN_DATA_ATTRIBUTIONS:
        if item.provider == normalized:
            return item
    # Checked before the fuzzy rules below on purpose: an AMap provider name such
    # as "amap-driving-routes" contains "route" and would otherwise be credited to
    # OSRM, crediting the wrong project and dropping a required AMap notice.
    if "amap" in normalized or "高德" in normalized:
        return AMAP_ATTRIBUTION
    # Both the POI and geocoding providers publish OSM-derived data.
    if any(token in normalized for token in ("openstreetmap", "overpass", "nominatim")):
        return OSM_ATTRIBUTION
    if "meteo" in normalized or "weather" in normalized or "air" in normalized:
        return OPEN_METEO_ATTRIBUTION
    if "osrm" in normalized or "route" in normalized:
        return OSRM_ATTRIBUTION
    if "holiday" in normalized:
        return HOLIDAYS_ATTRIBUTION
    if "wiki" in normalized:
        return WIKIPEDIA_ATTRIBUTION
    return None


def attributions_as_dicts() -> list[dict[str, str]]:
    return [item.as_dict() for item in OPEN_DATA_ATTRIBUTIONS]
