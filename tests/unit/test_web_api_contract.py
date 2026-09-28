"""The web form's vocabularies must be the API's vocabularies.

`plan-form.tsx` offered three paces — relaxed / balanced / **packed** — while the API's
`Pace` enum is relaxed / balanced / **full**. Choosing 紧凑 returned HTTP 422 and no trip:
the option existed, looked like every other option, and could not work.

Nothing could see it. A web test asserts against the form's own list and passes; a Python
test asserts against the enum and passes; the defect lives exactly in the gap between
them, and the languages cannot share a constant without a build step. So the contract is
checked the same way `.env.example` is: parse the TypeScript for the values it will send,
and compare them to the enum that receives them.

This is deliberately a text scan rather than a compiler check. It is crude, and it is the
only thing that would have caught the actual bug.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from travel_api.domain.models import Pace, TravelMode

PLAN_FORM = (
    Path(__file__).resolve().parents[2] / "apps" / "web" / "app" / "components" / "plan-form.tsx"
)

#: The empty string is a real choice in the form ("derive it from the pace"), not a mode,
#: so it is allowed to be absent from the enum.
_AUTO = ""


def _literal_values(source: str, const_name: str) -> list[str]:
    """Every ``value: "..."`` inside the named array literal.

    Scoped to the array so a value defined for another control cannot be mistaken for one
    this control sends.
    """
    match = re.search(
        rf"export const {const_name}\s*=\s*\[(.*?)\n\];",
        source,
        re.DOTALL,
    )
    assert match, f"could not find `{const_name}` in {PLAN_FORM.name}"
    return re.findall(r'value:\s*"([^"]*)"', match.group(1))


@pytest.fixture(scope="module")
def source() -> str:
    return PLAN_FORM.read_text(encoding="utf-8")


def test_the_form_exists_where_this_test_thinks_it_does(source):
    assert source, "empty source means the path is wrong and every check below is vacuous"


def test_every_pace_the_form_offers_is_one_the_api_accepts(source):
    """The exact defect: a selectable option that the server rejects."""
    offered = _literal_values(source, "PACES")
    assert offered, "the form must offer at least one pace"

    accepted = {member.value for member in Pace}
    unsupported = sorted(value for value in offered if value not in accepted)
    assert not unsupported, (
        f"{PLAN_FORM.name} offers pace values the API rejects: {unsupported}. "
        f"The API accepts {sorted(accepted)}."
    )


def test_every_pace_the_api_accepts_is_offered(source):
    """The other direction: a pace nobody can select is a capability with no entry point.

    This is how 公交/地铁 stayed unreachable on the API-side for so long, so it is worth
    asserting on the web side too.
    """
    offered = set(_literal_values(source, "PACES"))
    missing = sorted(member.value for member in Pace if member.value not in offered)
    assert not missing, f"the API accepts paces the form cannot send: {missing}"


def test_every_travel_mode_the_form_offers_is_one_the_api_accepts(source):
    offered = _literal_values(source, "TRAVEL_MODES")
    assert offered, "the form must offer at least one travel mode"

    accepted = {member.value for member in TravelMode} | {_AUTO}
    unsupported = sorted(value for value in offered if value not in accepted)
    assert not unsupported, (
        f"{PLAN_FORM.name} offers travel_mode values the API rejects: {unsupported}. "
        f"The API accepts {sorted(accepted)}."
    )


def test_the_chosen_travel_mode_is_actually_sent(source):
    """A control whose value never reaches the request is decoration.

    Matched on the field name, because the failure would be a new picker rendered next to
    a submit handler that was never updated to read it.
    """
    assert "profile.travel_mode = values.travel_mode" in source, (
        "the travel-mode picker exists but its value is not added to the submitted profile"
    )


def test_an_unset_travel_mode_is_omitted_rather_than_sent_as_null(source):
    """``null`` and absent differ: the API treats absent as "derive from the pace".

    Sending ``travel_mode: null`` explicitly would still validate, but it makes the field
    look required and would break the moment the API started distinguishing the two.
    """
    assignment = source.split("profile.travel_mode", 1)[1]
    guarded = assignment.split("}", 1)[0]
    assert "if (values.travel_mode)" in source.split("profile.travel_mode")[0][-80:], (
        "the assignment must sit inside a truthiness guard so an empty picker sends nothing"
    )
    assert guarded is not None


def test_the_transit_mode_is_offered_under_the_name_the_api_uses(source):
    """公交/地铁 needs a 高德 key, so a user has to be able to ask for it by name.

    If the wire value drifted from ``TravelMode.TRANSIT``, the one capability the 高德
    integration exists to add would again be unreachable from the interface.
    """
    assert "transit" in _literal_values(source, "TRAVEL_MODES")


# -- and the endpoint actually accepts them -----------------------------------------
#
# The scans above compare vocabularies, which is what would have caught the bug. This
# closes the loop by sending each of the form's own values to the real endpoint: a
# vocabulary can match the enum and still be rejected at the request layer, by a wrapper
# model or a stricter field. `packed` failed at the enum, but the next such mismatch
# might not.


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient
    from travel_api.config import Settings
    from travel_api.main import create_app

    tmp = tmp_path_factory.mktemp("pace-contract")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp / 'contract.db'}",
        auto_create_schema=True,
        provider_retry_count=0,
        free_providers_enabled=False,
        mock_provider_enabled=True,
        auth_enabled=False,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _create(client, **profile):
    payload = {
        "destination": "成都",
        "start_date": "2026-10-03",
        "end_date": "2026-10-05",
        "travelers": 2,
        "total_budget": 5000,
    }
    payload.update(profile)
    return client.post("/api/v1/trips", json=payload)


def test_every_pace_the_form_offers_is_accepted_by_the_endpoint(source, client):
    """The failure the user saw: an option that returns 422 instead of a trip."""
    for pace in _literal_values(source, "PACES"):
        response = _create(client, pace=pace)
        assert response.status_code < 400, (
            f"pace={pace!r} (offered by the form) was rejected with "
            f"{response.status_code}: {response.text[:200]}"
        )
        # And it must survive as the value that was asked for, not be silently
        # normalised to a different one.
        assert response.json()["profile"]["pace"] == pace


def test_every_travel_mode_the_form_offers_is_accepted_and_stored(source, client):
    for mode in _literal_values(source, "TRAVEL_MODES"):
        profile = {"pace": "balanced"}
        if mode:
            profile["travel_mode"] = mode
        response = _create(client, **profile)
        assert response.status_code < 400, (
            f"travel_mode={mode!r} was rejected with {response.status_code}: {response.text[:200]}"
        )
        stored = response.json()["profile"].get("travel_mode")
        # An empty picker means "derive it from the pace", which is stored as null rather
        # than as an empty string or a default mode.
        assert stored == (mode or None), f"travel_mode={mode!r} stored as {stored!r}"


def test_an_explicit_transit_request_survives_to_the_stored_trip(client):
    """The end of the chain that started with an unreachable capability.

    A transit request that validated but was dropped before storage would leave every
    downstream layer routing on the pace, with nothing to explain the difference.
    """
    response = _create(client, pace="balanced", travel_mode="transit")
    assert response.status_code < 400, response.text
    trip = response.json()

    assert trip["profile"]["travel_mode"] == "transit"
    # Re-read, because a value that only exists in the create response has not been stored.
    reread = client.get(f"/api/v1/trips/{trip['id']}")
    assert reread.status_code == 200
    assert reread.json()["profile"]["travel_mode"] == "transit"


def test_a_trip_that_never_mentions_a_mode_stores_none(client):
    """渐进增强: the field is optional, and absent must stay absent rather than become a
    default mode that later reads as a user choice."""
    response = _create(client, pace="balanced")
    assert response.status_code < 400, response.text
    assert response.json()["profile"].get("travel_mode") is None


INGESTIONS_LIB = Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "ingestions.ts"


def test_the_web_knows_exactly_the_fact_kinds_the_server_can_produce():
    """``FACT_KINDS`` in the web client against ``FactKind`` in the API.

    The ``packed``-pace shape, one layer out. The extractor and the note list are described by
    the same vocabulary, and the web keeps its own copy of it because each entry also carries a
    display tone. A kind the server can emit and the web cannot name renders as a raw identifier
    in a Chinese chip; a kind the web names and the server never emits is a legend entry for a
    fact that does not exist.

    Only the **keys** are compared, not the labels. The two sides legitimately word things
    differently — the web has a chip-sized label and the export has a table-cell one — and
    asserting on the strings would make this test a copy of them.
    """
    from travel_api.domain.ingestion import FactKind

    source = INGESTIONS_LIB.read_text(encoding="utf-8")
    match = re.search(r"export const FACT_KINDS[^{]*\{(.*?)\n\};", source, re.DOTALL)
    assert match, f"could not find `FACT_KINDS` in {INGESTIONS_LIB.name}"
    declared = set(re.findall(r"^\s*([a-z_]+):\s*\{", match.group(1), re.MULTILINE))
    assert declared, "an empty table means the scan is wrong and this check is vacuous"

    expected = {member.value for member in FactKind}
    assert declared == expected, (
        "the web and the API disagree about fact kinds. "
        f"web only: {sorted(declared - expected)}; api only: {sorted(expected - declared)}"
    )


def test_the_resolver_agrees_with_what_the_api_stored(client):
    """The stored mode is what the routing provider will act on, so they must match."""
    from travel_api.domain.models import TravelMode, TripProfile
    from travel_api.providers.base import resolve_travel_mode

    def stored_profile(response) -> TripProfile:
        # `nights` is a `@computed_field`, so it is serialised but cannot be validated
        # back in — `TripProfile` forbids extras. Dropping it is not papering over
        # anything: it is derived from the dates, which are kept.
        payload = {k: v for k, v in response.json()["profile"].items() if k != "nights"}
        return TripProfile.model_validate(payload)

    response = _create(client, pace="relaxed", travel_mode="transit")
    assert response.status_code < 400, response.text
    assert resolve_travel_mode(stored_profile(response)) is TravelMode.TRANSIT

    # And with no mode, the pace still decides.
    response = _create(client, pace="relaxed")
    assert resolve_travel_mode(stored_profile(response)) is TravelMode.WALK
