"""Tests for the AMap client's error handling and parsing.

These are deliberately independent of the route/POI response shapes: they cover the
envelope and the value conversions, which are where a request silently succeeds
while producing nonsense.

Two of them guard specific mistakes that AMap's wire format invites:

* ``status`` is the **string** ``"1"``. A truthiness check accepts ``"0"``, so
  every failure would read as a success.
* coordinates arrive as ``"lng,lat"`` but every internal model is
  latitude-first. A swap is numerically valid and puts the trip in the wrong
  hemisphere, so it is asserted against a real Beijing coordinate.
"""

from __future__ import annotations

import pytest
from travel_api.providers.amap.client import (
    AMAP_STATUS_OK,
    AmapClient,
    AmapError,
    AmapKeyError,
    AmapQuotaError,
    is_success,
    parse_float,
    parse_int,
    parse_location,
    redact_key,
    require_success,
)


class _StubHttp:
    """Captures requests and replays one canned payload."""

    def __init__(self, payload: object = None) -> None:
        self.payload = payload if payload is not None else {"status": "1"}
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get_json(self, url, *, params=None):
        self.calls.append(("GET", url, params))
        return self.payload

    async def post_json(self, url, *, data=None):
        self.calls.append(("POST", url, data))
        return self.payload


# -- the success rule ------------------------------------------------------


def test_status_is_compared_as_a_string():
    """``status`` is ``"1"``, not ``1`` and not ``True``.

    The failure this guards is specific: ``if payload.get("status")`` is truthy for
    the string ``"0"``, so a truthiness check reports every AMap error as a
    success and the trip is planned from empty data.
    """
    assert is_success({"status": "1"}) is True
    assert is_success({"status": "0"}) is False
    assert is_success({"status": 1}) is True
    assert is_success({}) is False
    # The exact bug: a falsy-looking failure that a naive check would accept.
    assert bool("0") is True
    assert is_success({"status": "0"}) is False


def test_a_failure_names_the_infocode_rather_than_the_url():
    """The URL carries the key, so it must not appear in the error."""
    with pytest.raises(AmapError) as caught:
        require_success(
            {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"},
            operation="/v3/direction/transit/integrated",
        )
    message = str(caught.value)
    assert "infocode=10001" in message
    assert "INVALID_USER_KEY" in message
    assert "key=" not in message


def test_an_invalid_key_is_reported_as_a_key_problem():
    with pytest.raises(AmapKeyError):
        require_success(
            {"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY"}, operation="x"
        )


def test_exceeding_the_daily_quota_is_not_reported_as_a_bad_key():
    """Different remedy: the key is fine and telling the user to re-enter it wastes
    their time and hides the real cause."""
    with pytest.raises(AmapQuotaError):
        require_success(
            {"status": "0", "infocode": "10044", "info": "DAILY_QUERY_OVER_LIMIT"}, operation="x"
        )


def test_an_unknown_failure_is_still_permanent():
    with pytest.raises(AmapError):
        require_success({"status": "0", "infocode": "20000", "info": "UNKNOWN"}, operation="x")


def test_a_non_object_payload_is_an_error_not_a_crash():
    with pytest.raises(AmapError):
        require_success(["not", "an", "object"], operation="x")


# -- value parsing ---------------------------------------------------------


def test_coordinates_are_swapped_into_latitude_first_order():
    """AMap sends ``lng,lat``; every model here is latitude-first.

    Uses Tiananmen Square. If the pair were passed through unswapped the result
    would still be numerically valid latitude/longitude, so nothing downstream
    could detect it — the trip would simply be planned in the wrong place.
    """
    assert parse_location("116.397428,39.90923") == (39.90923, 116.397428)


def test_a_malformed_location_is_none_rather_than_a_bad_guess():
    for value in (None, "", "[]", "116.397428", "not,a,place", "1e999,2"):
        if value == "1e999,2":
            # Infinity is rejected by the range check rather than becoming a
            # coordinate.
            assert parse_location(value) is None
        else:
            assert parse_location(value) is None, value


def test_amap_absent_values_are_none_not_zero():
    """``""`` and ``"[]"`` both mean "no value" in AMap responses.

    A bare ``float("")`` raises; defaulting to 0 instead would place a hotel at
    latitude 0 and put the trip in the Atlantic.
    """
    assert parse_float("") is None
    assert parse_float("[]") is None
    assert parse_float(None) is None
    assert parse_float("abc") is None
    assert parse_float("12.5") == 12.5
    assert parse_int("120") == 120
    assert parse_int("") is None


# -- the client ------------------------------------------------------------


def test_a_client_without_a_key_is_refused():
    """A keyless client would send "" and get INVALID_USER_KEY on every call,
    which reads as a broken key rather than a missing one."""
    with pytest.raises(AmapKeyError):
        AmapClient(http=_StubHttp(), key="")
    with pytest.raises(AmapKeyError):
        AmapClient(http=_StubHttp(), key="   ")


@pytest.mark.asyncio
async def test_the_key_is_injected_and_the_path_is_joined():
    http = _StubHttp({"status": AMAP_STATUS_OK})
    client = AmapClient(http=http, key="secret-key-value", base_url="https://example.test/")

    await client.get("/v5/direction/transit/integrated", params={"city": "北京"})

    _, url, params = http.calls[0]
    assert url == "https://example.test/v5/direction/transit/integrated"
    assert params["key"] == "secret-key-value"
    assert params["city"] == "北京"


@pytest.mark.asyncio
async def test_post_is_used_for_large_parameter_sets():
    http = _StubHttp({"status": AMAP_STATUS_OK})
    client = AmapClient(http=http, key="k")

    await client.post("/v3/distance", data={"origins": "1,1|2,2"})

    method, _, data = http.calls[0]
    assert method == "POST"
    assert data["key"] == "k"


@pytest.mark.asyncio
async def test_a_failed_call_raises_without_leaking_the_key():
    http = _StubHttp({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
    client = AmapClient(http=http, key="sk-must-not-appear")

    with pytest.raises(AmapKeyError) as caught:
        await client.get("/v3/geocode/geo", params={"address": "北京"})

    assert "sk-must-not-appear" not in str(caught.value)
    assert "sk-must-not-appear" not in repr(caught.value.__dict__)


def test_the_public_base_url_never_contains_the_key():
    client = AmapClient(http=_StubHttp(), key="sk-secret", base_url="https://restapi.amap.com")
    assert "sk-secret" not in client.base_url_public


def test_redacting_a_url_removes_only_the_key():
    """The debug-log helper. Kept tested because it is the one function whose whole
    job is to prevent a leak, and a silent failure in it is invisible."""
    assert redact_key("/v3/geocode/geo?key=abc&address=x") == "/v3/geocode/geo?key=***&address=x"
    assert redact_key("/path?key=abc") == "/path?key=***"
    assert redact_key("/path?address=x") == "/path?address=x"
    assert redact_key(None) == ""
    assert redact_key("") == ""


def test_redacting_leaves_no_fragment_of_a_long_key():
    secret = "a" * 32
    redacted = redact_key(f"/p?key={secret}&x=1")
    assert secret not in redacted
    assert secret[:8] not in redacted
