"""Offline tests for the RollingGo MCP client and hotel provider.

A fake transport serves recorded-shape payloads, so the mapping from the MCP envelope to
domain models is verified without the network — the same approach the free providers use.

**What these tests cannot prove.** The response envelope is not published by the service, so
the payloads here are built from the MCP convention (``result.content[*].text`` holding the
tool's JSON) and from the one ``getHotelDetail`` example in the project's README. A test
written against an assumed shape passes whether or not the assumption holds, which is why
``scripts/probe_rollinggo_shape.py`` exists and why the parser records the key it matched
instead of asserting one.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from travel_api.domain.models import (
    Coordinates,
    DataDomain,
    Hotel,
    Location,
    ObservationBasis,
    PriceType,
    SourceReference,
    SourceType,
    TripProfile,
)
from travel_api.providers.base import CAPABILITY_BY_METHOD
from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.manifest import (
    capability_gaps,
    manifest_for,
)
from travel_api.providers.mock import stable_id
from travel_api.providers.registry import ProviderRegistry
from travel_api.providers.rollinggo import (
    ROLLINGGO_HOTEL_PROVIDER_NAME,
    RollingGoHotelProvider,
    RollingGoKeyError,
    RollingGoMcpClient,
    build_rollinggo_providers,
    parse_jsonrpc_payload,
    unwrap_result,
)

NO_RETRY = {"host_intervals": {"mcp.rollinggo.cn": 0.0}, "retry_count": 0}


def make_profile() -> TripProfile:
    return TripProfile(
        destination="杭州",
        start_date=date(2026, 10, 20),
        end_date=date(2026, 10, 23),
        travelers=2,
        total_budget=Decimal("6000"),
    )


def mcp_envelope(payload: object, *, request_id: int = 1) -> dict:
    """A JSON-RPC response whose tool text carries ``payload``."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": False,
        },
    }


def sse_body(envelope: dict) -> str:
    """The same response framed as ``text/event-stream``, with a notification first."""
    notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
    return f"event: message\ndata: {notification}\n\ndata: {json.dumps(envelope)}\n\n"


def recording_client(responder, *, key: str = "test-key") -> tuple[RollingGoMcpClient, list]:
    """A client whose transport records each request and answers via ``responder``."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(handle))
    return RollingGoMcpClient(http=http, key=key, base_url="https://mcp.rollinggo.cn/mcp"), seen


def recording_provider(responder) -> RollingGoHotelProvider:
    """A provider backed by a recording client, for tests that only need the provider."""
    client, _ = recording_client(responder)
    return RollingGoHotelProvider(client)


def json_responder(payload: object):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mcp_envelope(payload))

    return handle


def hotel_row(hotel_id: str = "1090005", **overrides) -> dict:
    row = {
        "hotelId": hotel_id,
        "name": "杭州西子湖四季酒店",
        "address": "杭州市西湖区北山街 5 号",
        "city": "杭州",
        "geo": {"latitude": 30.2531, "longitude": 120.1461},
    }
    row.update(overrides)
    return row


# -- transport ---------------------------------------------------------------


async def test_the_client_sends_a_json_rpc_tools_call_with_the_required_headers():
    """The envelope and the Accept pair are the documented contract.

    ``Accept`` is not decoration: the README states the server returns HTTP 400 without both
    content types, and the shared HTTP client defaults to ``application/json`` alone.
    """
    client, seen = recording_client(json_responder({"hotels": []}))

    await client.call_tool("searchHotels", {"place": "杭州"})

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://mcp.rollinggo.cn/mcp"
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["accept"] == "application/json, text/event-stream"
    body = json.loads(request.content)
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "searchHotels"
    assert body["params"]["arguments"] == {"place": "杭州"}
    assert isinstance(body["id"], int)


async def test_the_key_is_never_placed_in_the_url():
    """Unlike 高德's query-parameter key, this one travels in a header only."""
    client, seen = recording_client(json_responder({"hotels": []}))

    await client.call_tool("searchHotels", {})

    assert "test-key" not in str(seen[0].url)


def test_an_sse_framed_response_is_parsed():
    """``text/event-stream`` carries the same JSON behind ``data:`` lines.

    A stream may deliver notifications before the result, so the last frame that parses as
    an object is the one returned.
    """
    payload = mcp_envelope({"hotels": [hotel_row()]})

    parsed = parse_jsonrpc_payload(sse_body(payload))

    assert parsed == payload


def test_a_plain_json_body_is_parsed():
    payload = mcp_envelope({"hotels": []})
    assert parse_jsonrpc_payload(json.dumps(payload)) == payload


def test_a_json_rpc_error_is_reported_with_its_code_and_message():
    """The code is surfaced rather than mapped onto an invented category.

    The service publishes no error-code table, so classifying one would be manufacturing a
    taxonomy; the HTTP status is what distinguishes a rejected key.
    """
    from travel_api.providers.rollinggo import RollingGoError

    with pytest.raises(RollingGoError) as excinfo:
        unwrap_result(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad args"}},
            operation="searchHotels",
        )

    assert "-32602" in str(excinfo.value)
    assert "bad args" in str(excinfo.value)


async def test_a_rejected_key_is_a_key_error_and_not_a_generic_failure():
    """The one failure the user can fix gets its own type, so the UI can say so."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client, _ = recording_client(handle)

    with pytest.raises(RollingGoKeyError):
        await client.call_tool("searchHotels", {})


def test_a_client_cannot_be_built_without_a_key():
    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(json_responder({})))
    with pytest.raises(RollingGoKeyError):
        RollingGoMcpClient(http=http, key="   ")


# -- hotel mapping -----------------------------------------------------------


async def test_hotels_are_mapped_with_their_own_id_on_the_source_reference():
    """The supplier's id belongs on the reference, never in ``Hotel.id``.

    ``Hotel.id`` is minted here (a stable UUID derived from the source id) and the supplier's
    id travels on the reference — which is exactly what ``search_room_offers`` reads back.
    """
    provider = recording_provider(json_responder({"hotels": [hotel_row()]}))

    result = await provider.search_hotels(make_profile())

    assert len(result.data) == 1
    hotel = result.data[0]
    assert hotel.canonical_name == "杭州西子湖四季酒店"
    assert hotel.location.coordinates.latitude == 30.2531
    assert str(hotel.id) == str(stable_id("rollinggo:hotel:1090005"))
    assert str(hotel.id) != "1090005"
    reference = hotel.source_references[0]
    assert reference.source_id == "1090005"
    assert reference.source == "rollinggo"
    assert reference.source_type is SourceType.PARTNER_API


async def test_the_matched_payload_key_is_recorded_on_the_reference():
    """A real run reports the layout it found instead of the assumption it was written under."""
    provider = recording_provider(json_responder({"data": [hotel_row()]}))

    result = await provider.search_hotels(make_profile())

    assert result.data[0].source_references[0].request_context["payload_key"] == "data"


async def test_a_hotel_row_without_coordinates_is_dropped():
    """No coordinate means no itinerary point, and inventing one places a real hotel wrongly."""
    row = hotel_row()
    del row["geo"]
    provider = recording_provider(json_responder({"hotels": [row]}))

    result = await provider.search_hotels(make_profile())

    assert result.data == []


async def test_a_search_that_matches_no_known_key_yields_no_hotels():
    """The honest failure: no rows, rather than a guessed layout that produces nonsense."""
    provider = recording_provider(json_responder({"somethingElse": [hotel_row()]}))

    result = await provider.search_hotels(make_profile())

    assert result.data == []


# -- room offers -------------------------------------------------------------


def offer_row(**overrides) -> dict:
    row = {
        "roomTypeId": 22724870,
        "roomName": "标准双床房",
        "ratePlanId": "rate-1",
        "ratePlanName": "标准双床房",
        "currency": "CNY",
        "totalPrice": 1056,
        "averagePrice": 352,
        "availability": 3,
        "isOnRequest": False,
        "cancellationPolicies": [{"fromDate": "2026-10-19T00:00:00+08:00", "amount": 352}],
    }
    row.update(overrides)
    return row


def offering_provider(rows: list[dict]) -> RollingGoHotelProvider:
    """A provider whose ``getHotelDetail`` answers with ``rows``."""

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tool = body["params"]["name"]
        payload = {"hotels": [hotel_row()]} if tool == "searchHotels" else {"roomRatePlans": rows}
        return httpx.Response(200, json=mcp_envelope(payload))

    client, _ = recording_client(responder)
    return RollingGoHotelProvider(client)


def priced_hotel(source: str = "rollinggo", source_id: str = "1090005") -> Hotel:
    now = datetime.now(timezone.utc)
    return Hotel(
        id=stable_id(f"{source}:hotel:{source_id}"),
        canonical_name="某酒店",
        location=Location(
            address="某路 1 号",
            city="杭州",
            coordinates=Coordinates(latitude=30.25, longitude=120.14),
        ),
        source_references=[
            SourceReference(
                source=source,
                source_type=SourceType.PARTNER_API,
                source_id=source_id,
                observed_at=now,
                fetched_at=now,
                domain=DataDomain.COMMERCIAL_FACT,
            )
        ],
    )


async def test_a_room_offer_is_priced_from_the_hotels_own_supplier_id():
    """The whole reason ``search_room_offers`` takes entities: the supplier id is read back."""
    seen: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=mcp_envelope({"roomRatePlans": [offer_row()]}))

    client, _ = recording_client(responder)
    provider = RollingGoHotelProvider(client)

    result = await provider.search_room_offers([priced_hotel()], make_profile())

    assert len(result.data) == 1
    offer = result.data[0]
    assert offer.price.value == Decimal("1056")
    assert offer.price.price_type is PriceType.PER_STAY
    assert offer.check_in == date(2026, 10, 20)
    assert offer.check_out == date(2026, 10, 23)
    assert offer.guests == 2
    # And the request actually carried the supplier's own id, not our UUID.
    arguments = json.loads(seen[0].content)["params"]["arguments"]
    assert arguments["hotelId"] == "1090005"
    assert str(priced_hotel().id) not in json.dumps(arguments)


async def test_a_hotel_from_another_source_is_not_priced():
    """A rate attached to the wrong hotel is indistinguishable from a correct one."""
    seen: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=mcp_envelope({"roomRatePlans": [offer_row()]}))

    client, _ = recording_client(responder)
    provider = RollingGoHotelProvider(client)

    result = await provider.search_room_offers(
        [priced_hotel(source="openstreetmap")], make_profile()
    )

    assert result.data == []
    assert seen == [], "no request should be made for a hotel this source did not issue"


async def test_total_price_is_per_stay_and_average_price_is_per_night():
    """Two different quantities; the price type follows whichever the source sent."""
    provider = offering_provider([offer_row(totalPrice=None)])

    result = await provider.search_room_offers([priced_hotel()], make_profile())

    offer = result.data[0]
    assert offer.price.value == Decimal("352")
    assert offer.price.price_type is PriceType.PER_NIGHT


async def test_an_offer_with_no_price_is_dropped():
    """No amount means no offer; a zero would enter the budget as a commercial fact."""
    provider = offering_provider([offer_row(totalPrice=None, averagePrice=None)])

    result = await provider.search_room_offers([priced_hotel()], make_profile())

    assert result.data == []


async def test_a_room_on_request_is_not_reported_as_available():
    """「需申请」不是「有房」: no confirmed inventory must not read as availability > 0."""
    provider = offering_provider([offer_row(isOnRequest=True, availability=5)])

    result = await provider.search_room_offers([priced_hotel()], make_profile())

    assert result.data[0].availability is None


async def test_offers_are_stamped_source_silent_with_the_fetch_time():
    """The source states nothing about when the price was produced.

    ``LIVE_COMPUTED`` would claim the age describes the data; it describes our request, and
    whether the upstream is itself cached is not something this program knows. So the basis
    is ``SOURCE_SILENT`` and ``age_is_about_the_data`` is False — an unclassified or silent
    reference must never be counted as evidence of freshness.
    """
    provider = offering_provider([offer_row()])

    result = await provider.search_room_offers([priced_hotel()], make_profile())

    reference = result.data[0].source_reference
    assert reference.observed_at_basis is ObservationBasis.SOURCE_SILENT
    assert reference.observed_at_basis.age_is_about_the_data is False
    assert reference.observed_at == reference.fetched_at
    assert reference.expires_at is not None


async def test_the_cancellation_policy_is_rendered_from_the_sources_own_fields():
    provider = offering_provider([offer_row()])

    result = await provider.search_room_offers([priced_hotel()], make_profile())

    policy = result.data[0].cancellation_policy
    assert policy is not None
    assert "2026-10-19T00:00:00+08:00" in policy
    assert "352" in policy


# -- capability wiring -------------------------------------------------------


def test_the_manifest_declares_exactly_what_the_provider_serves():
    """The anti-drift check: a declaration the registry cannot route is a false promise.

    This is the shape that let 高德 advertise ``search_hotels`` while its capability was
    unreachable — declared, implemented and routed were each correct on their own.
    """
    manifest = manifest_for("rollinggo")
    assert manifest is not None
    assert set(manifest.capabilities) <= set(CAPABILITY_BY_METHOD)

    http = FreeHttpClient(**NO_RETRY, transport=httpx.MockTransport(json_responder({})))
    registry = ProviderRegistry(build_rollinggo_providers(key="k", http=http))

    for capability in manifest.capabilities:
        routed = {getattr(p, "name", "") for p in registry.serving_providers_for(capability)}
        assert routed & set(manifest.provider_names), (
            f"rollinggo declares {capability} but none of its providers serve it"
        )


def test_the_source_id_and_the_registry_name_are_different_things():
    """The registry is keyed by name, so the source id must not double as one."""
    manifest = manifest_for("rollinggo")
    assert manifest is not None
    assert manifest.id == "rollinggo"
    assert ROLLINGGO_HOTEL_PROVIDER_NAME in manifest.provider_names
    assert "rollinggo" not in manifest.provider_names


def test_room_offers_are_now_unlockable_rather_than_an_unclosable_gap():
    """Adding this source has to move the capability between two lists, not duplicate it.

    Before RollingGo, 房型与报价 was reported as a gap no source could close. It is still
    uncovered in a keyless deployment — but it is now something a credential unlocks, and
    the two answers have opposite remedies, so both lists must not contain it.
    """
    gaps = {gap["capability"] for gap in capability_gaps(["openstreetmap", "osrm"], None)}
    assert "search_room_offers" not in gaps
    assert "search_reviews" in gaps

    manifest = manifest_for("rollinggo")
    assert manifest is not None
    assert "search_room_offers" in manifest.capabilities
    assert manifest.status == "needs_credentials"
