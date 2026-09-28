"""A place named in the traveller's own note, located in the plan — over real HTTP.

The unit tests prove the decision. This one answers the question the product actually asks:
**when a note names somewhere our data sources do not have, does the plan end up with a
coordinate for it — and does it stay distinguishable from a place a source measured?**

Both halves matter, and the second is the one that would be quietly lost. A located name
carries a coordinate but no rating, no ticket price and no opening hours, because nothing
measured them; it also is not scheduled into a day, because a name in somebody's post is
evidence that they liked a place, not this traveller's decision to go there. If it arrived as
an ``Attraction`` it would silently acquire all of those by being the same type as the ones
that do have them.

A local HTTP server stands in for 高德, varying its answer by the ``address`` it is asked for,
so the ambiguous-namesake case is exercised rather than described.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app
from travel_api.providers.amap import AMAP_KEY_CREDENTIAL, AMAP_PROVIDER_ID

STUB_KEY = "amap-stub-key"

#: 杭州's centre, and the point the trip's destination resolves to.
HANGZHOU_CENTRE = "120.155100,30.274100"
#: 灵隐寺, about 6 km west of it — the landmark a note names.
LINGYIN_TEMPLE = "120.101000,30.240800"
#: 灵隐街道, the administrative area containing it and nearer the city centre. This is what
#: makes the test about precision rather than about distance.
LINGYIN_SUBDISTRICT = "120.120000,30.250000"

#: 故宫博物院 — a POI the stub reports, so the plan has one measured attraction to compare
#: the located name against.
PALACE_MUSEUM = "120.160000,30.260000"


def _first(query: dict, name: str) -> str:
    """``parse_qs`` yields lists, so every read needs unwrapping. Done once, here."""
    values = query.get(name)
    if not values:
        return ""
    return values[0] if isinstance(values, list) else str(values)


def _geocode_payload(query: dict) -> dict:
    """Vary the answer by what was asked for, which is what makes the stub useful.

    A stub returning one canned result for every query could not exercise an ambiguous name at
    all: both the destination and the namesake would resolve to the same point and the
    distance check would trivially pass.
    """
    address = _first(query, "address")
    if address == "杭州":
        return _geocodes([("浙江省杭州市", "杭州市", "", "330100", HANGZHOU_CENTRE, "省")])
    if address == "灵隐寺":
        # 高德's own ordering puts the district first, as it does for destination queries. The
        # resolver must not inherit that preference — see the unit test of the same name.
        return _geocodes(
            [
                ("灵隐街道", "杭州市", "西湖区", "330106", LINGYIN_SUBDISTRICT, "区县"),
                ("灵隐寺", "杭州市", "西湖区", "330106", LINGYIN_TEMPLE, "兴趣点"),
            ]
        )
    if address == "西湖":
        # The namesake case: a real 西湖, 950 km away in 惠州. Nothing in the trip's city
        # matches, so this must be reported as 在别处 rather than scheduled.
        return _geocodes(
            [("惠州西湖", "惠州市", "惠城区", "441302", "114.416200,23.111500", "兴趣点")]
        )
    return {"status": "1", "info": "OK", "infocode": "10000", "count": "0", "geocodes": []}


def _geocodes(entries: list[tuple[str, str, str, str, str, str]]) -> dict:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": str(len(entries)),
        "geocodes": [
            {
                "formatted_address": address,
                "province": city,
                "city": city,
                "district": district,
                "adcode": adcode,
                "location": location,
                "level": level,
            }
            for address, city, district, adcode, location, level in entries
        ],
    }


def _places(keywords: str) -> dict:
    specs = {
        "景点": [("B0AAA", "故宫博物院", "风景名胜;风景名胜;世界遗产", PALACE_MUSEUM)],
        "美食": [("B0BBB", "楼外楼", "餐饮服务;中餐厅", HANGZHOU_CENTRE)],
        "酒店": [("B0CCC", "杭州饭店", "住宿服务;宾馆酒店", HANGZHOU_CENTRE)],
    }
    pois = specs.get(keywords, [])
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": str(len(pois)),
        "pois": [
            {
                "id": poi_id,
                "name": name,
                "type": poi_type,
                "location": location,
                "address": name,
                "cityname": "杭州市",
                "adname": "西湖区",
                "pname": "浙江省",
            }
            for poi_id, name, poi_type, location in pois
        ],
    }


class _StubHandler(BaseHTTPRequestHandler):
    received: list[tuple[str, dict]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's required name
        parsed = urlparse(self.path)
        query = dict(parse_qs(parsed.query))
        type(self).received.append((parsed.path, query))

        if parsed.path == "/v3/geocode/geo":
            payload = _geocode_payload(query)
        elif parsed.path == "/v3/place/text":
            payload = _places(_first(query, "keywords"))
        else:
            payload = {"status": "0", "info": "UNKNOWN_PATH", "infocode": "20000"}

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ARG002 - silence the stub
        return


@pytest.fixture
def stub():
    _StubHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _StubHandler.received
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def client(tmp_path, stub):
    base_url, received = stub
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'places.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        # The free stack off, so nothing in the result can have come from it and a located
        # place can only have come from the 高德 stub.
        free_providers_enabled=False,
        mock_provider_enabled=False,
        amap_base_url=base_url,
        amap_ttl_seconds=300,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    with TestClient(create_app(settings)) as test_client:
        test_client.received = received  # type: ignore[attr-defined]
        yield test_client


def _key(client, owner: str = "owner-a") -> str:
    key, _ = client.app.state.auth_service.mint(owner, label=owner)
    return key


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _profile() -> dict:
    return {
        "destination": "杭州",
        "start_date": "2026-10-03",
        "end_date": "2026-10-05",
        "travelers": 2,
        "total_budget": 5000,
    }


def _with_key(client, key: str) -> None:
    stored = client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": STUB_KEY},
        headers=_auth(key),
    )
    assert stored.status_code == 200


def _preview(client, key: str, note: str | None = None):
    if note is not None:
        saved = client.post("/api/v1/ingestions", json={"text": note}, headers=_auth(key))
        assert saved.status_code in (200, 201), saved.text
    response = client.post("/api/v1/trips/preview", json=_profile(), headers=_auth(key))
    assert response.status_code == 200, response.text
    return response.json()


def test_a_place_only_the_note_knows_gets_a_coordinate(client):
    """The feature: a name our sources do not have becomes usable."""
    key = _key(client)
    _with_key(client, key)

    preview = _preview(client, key, "推荐去灵隐寺看看，很值得。")

    located = preview["ingested_places"]
    assert len(located) == 1, located
    entry = located[0]
    assert entry["name"] == "灵隐寺"
    assert entry["status"] == "located"
    assert entry["coordinates"] is not None
    assert entry["coordinates"]["latitude"] == pytest.approx(30.2408, abs=1e-4)
    assert entry["coordinates"]["longitude"] == pytest.approx(120.1010, abs=1e-4)
    assert entry["display_name"] == "灵隐寺"
    assert entry["straight_line_from_centre_km"] is not None
    # The reason says *how* it was chosen, so a reader knows a namesake could have been picked.
    assert "最近" in entry["reason"]


def test_a_located_place_is_not_a_measured_attraction(client):
    """The boundary the whole type exists to hold.

    ``ResolvedPlace`` is a different type from ``Attraction`` precisely so that no code
    consuming attractions can receive one of these. If it arrived there it would silently
    acquire a rating, a ticket price and opening hours that nothing measured.
    """
    key = _key(client)
    _with_key(client, key)

    preview = _preview(client, key, "推荐去灵隐寺看看，很值得。")

    assert preview["ingested_places"], "the name should have been located"
    attraction_names = {item["canonical_name"] for item in preview["attractions"]}
    assert "灵隐寺" not in attraction_names, "a mentioned place must not become an attraction"
    # And it must not be scheduled: a day plan is the traveller's decision, not a post's.
    itinerary_names = {
        item.get("title") or item.get("name")
        for day in preview["itinerary"]["days"]
        for item in day["items"]
    }
    assert "灵隐寺" not in itinerary_names


def test_the_lookup_is_cited_in_the_plans_sources(client):
    """A lookup is a data source, so its result is only checkable if it is cited."""
    key = _key(client)
    _with_key(client, key)

    preview = _preview(client, key, "推荐去灵隐寺看看，很值得。")

    geocode_refs = [
        ref for ref in preview["source_references"] if ref["source"] == AMAP_PROVIDER_ID
    ]
    assert geocode_refs, "the place lookup must be cited"
    contexts = [ref["request_context"] for ref in geocode_refs]
    assert any(ctx.get("query") == "灵隐寺" for ctx in contexts), contexts
    looked_up = next(ctx for ctx in contexts if ctx.get("query") == "灵隐寺")
    assert looked_up["level"] == "兴趣点"
    assert looked_up["distance_from_destination_km"] is not None
    # The gazetteer publishes no rebuild time, so the only known time is when we asked.
    assert all(ref["observed_at_basis"] == "source_silent" for ref in geocode_refs), (
        "a geocoder does not publish its index's age"
    )


def test_a_namesake_far_away_is_reported_as_elsewhere_and_not_scheduled(client):
    """Found, and not here — which is a fact about the note, not a failure."""
    key = _key(client)
    _with_key(client, key)

    preview = _preview(client, key, "推荐去西湖看看，很值得。")

    entries = [item for item in preview["ingested_places"] if item["name"] == "西湖"]
    assert entries, preview["ingested_places"]
    entry = entries[0]
    assert entry["status"] == "elsewhere"
    assert entry["coordinates"] is None
    assert entry["straight_line_from_centre_km"] > 900
    assert "公里" in entry["reason"]
    # The rejection is still cited, so 「在别处」 is checkable rather than an assertion.
    assert entry.get("source_reference") is not None or any(
        ref["request_context"].get("query") == "西湖"
        for ref in preview["source_references"]
        if ref["source"] == AMAP_PROVIDER_ID
    )


def test_a_mention_our_sources_already_have_is_not_looked_up(client):
    """Matching runs first, and only what is left over is geocoded.

    Two reasons this matters: the measured POI is a better answer than a geocoded name, and a
    lookup for something already in the plan spends the owner's 高德 quota on a redundant
    question.
    """
    key = _key(client)
    _with_key(client, key)

    # 故宫博物院 is what the stub reports as an attraction, so the mention matches it.
    preview = _preview(client, key, "推荐去故宫博物院，很值得。")

    assert any(
        note["matched"] and note["matched_entity_name"] == "故宫博物院"
        for note in preview["ingested_notes"]
    ), preview["ingested_notes"]
    assert [item["name"] for item in preview["ingested_places"]] == [], (
        "a matched mention must not be geocoded"
    )
    # And no lookup was made for it, which the stub's request log proves.
    addresses = [
        _first(params, "address")
        for path, params in client.received  # type: ignore[attr-defined]
        if path == "/v3/geocode/geo"
    ]
    assert "故宫博物院" not in addresses, addresses


def test_without_a_source_the_mention_is_reported_as_not_located(client, tmp_path):
    """渐进增强 in the direction that is easy to get wrong.

    With no key there is nothing that can resolve a place, so the mention stays a mention. What
    must *not* happen is a claim about the place: 「没有找到灵隐寺」 would blame the place for
    the absence of a source, and nobody looked.
    """
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'nokey.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        free_providers_enabled=False,
        mock_provider_enabled=True,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    with TestClient(create_app(settings)) as bare:
        key = _key(bare)
        saved = bare.post(
            "/api/v1/ingestions", json={"text": "推荐去灵隐寺看看。"}, headers=_auth(key)
        )
        assert saved.status_code in (200, 201)
        response = bare.post("/api/v1/trips/preview", json=_profile(), headers=_auth(key))
        assert response.status_code == 200, response.text
        preview = response.json()

    # The mention is still reported as a mention, with the coordinate-less reason that already
    # existed for it.
    mentions = [n for n in preview["ingested_notes"] if n["kind"] == "place_mention"]
    assert [n["text"] for n in mentions] == ["灵隐寺"]
    # And nothing claims to have looked for it: no place list, and no lookup citation.
    assert preview["ingested_places"] == []
    assert not [
        ref
        for ref in preview["source_references"]
        if ref["request_context"].get("query") == "灵隐寺"
    ]


def test_another_owners_note_never_reaches_this_plans_places(client):
    """Owner isolation, extended to the new field.

    The located places derive from ingested notes, so they are exactly as private as the notes
    — and a leak here would be a leak of the traveller's own material presented as a coordinate
    on somebody else's map.
    """
    alice = _key(client, "alice")
    bob = _key(client, "bob")
    _with_key(client, alice)
    _with_key(client, bob)

    _preview(client, alice, "推荐去灵隐寺看看，很值得。")
    bobs = _preview(client, bob)

    assert bobs["ingested_notes"] == []
    assert bobs["ingested_places"] == []
    assert "灵隐寺" not in json.dumps(bobs, ensure_ascii=False)
