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
PLANNING_JOBS_LIB = (
    Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "planning-jobs.ts"
)
INGESTED_PLACES_LIB = (
    Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "ingested-places.ts"
)


def _ts_union_members(source: str, type_name: str, *, path: Path) -> set[str]:
    """The string literals of a TypeScript union type, e.g. ``"a" | "b"``.

    Shared by the three union checks below, because each of them must read the union the same
    way — a scan that worked for one and silently returned nothing for another would pass.
    """
    match = re.search(rf"export type {type_name}\s*=(.*?);", source, re.DOTALL)
    assert match, f"could not find `{type_name}` in {path.name}"
    members = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert members, f"`{type_name}` parsed to nothing, so the check would be vacuous"
    return members


def test_the_web_knows_every_place_resolution_status_the_server_can_report():
    """``PlaceResolutionStatus`` in the web against ``PlaceResolutionStatus`` in the API.

    The same shape as the two checks around it, and the one with the most at stake: the four
    statuses are kept apart precisely so that "nobody was asked" is not presented as "asked and
    not found". A status the server can report and the web cannot name falls into the
    ``default:`` branch, which says 未知的定位结果 — honest, and useless to the reader who is
    trying to find out whether their note was ignored.
    """
    from travel_api.domain.models import PlaceResolutionStatus

    source = INGESTED_PLACES_LIB.read_text(encoding="utf-8")
    declared = _ts_union_members(source, "PlaceResolutionStatus", path=INGESTED_PLACES_LIB)
    expected = {member.value for member in PlaceResolutionStatus}

    assert declared == expected, (
        "the web and the API disagree about place resolution statuses. "
        f"web only: {sorted(declared - expected)}; api only: {sorted(expected - declared)}"
    )


def test_the_web_knows_every_job_status_the_server_can_report():
    """A status the web does not know is not merely unlabelled — it is treated as non-terminal.

    ``isTerminal`` returns false for anything outside its three end states, and the client polls
    until a job is terminal. So a status added to the server appears on the web as a job that
    runs forever: the progress line says 进行中 while the work finished some time ago, and the
    poll never stops. Silence, rather than a visible gap.
    """
    from travel_api.services.planning_jobs import JobStatus

    source = PLANNING_JOBS_LIB.read_text(encoding="utf-8")
    declared = _ts_union_members(source, "JobStatus", path=PLANNING_JOBS_LIB)
    expected = {member.value for member in JobStatus}

    assert declared == expected, (
        "the web and the API disagree about job statuses. "
        f"web only: {sorted(declared - expected)}; api only: {sorted(expected - declared)}"
    )


def test_the_web_knows_every_job_kind_the_server_can_report():
    """``kind`` decides what the client says is happening, so a third kind must not be silent.

    The web branches on ``kind`` to tell "a new trip is being planned" from "the trip on screen
    is being replaced". A kind it does not know falls to whichever branch is the ``else``, which
    would describe a refresh as a new trip.
    """
    from travel_api.services.planning_jobs import JobKind

    source = PLANNING_JOBS_LIB.read_text(encoding="utf-8")
    declared = _ts_union_members(source, "JobKind", path=PLANNING_JOBS_LIB)
    expected = {member.value for member in JobKind}

    assert declared == expected, (
        "the web and the API disagree about job kinds. "
        f"web only: {sorted(declared - expected)}; api only: {sorted(expected - declared)}"
    )


def test_every_place_resolution_status_has_its_own_sentence():
    """Handling a status is not enough — the four must not say the same thing.

    A status with no ``case`` falls through to ``default:``, which reports 未知的定位结果. That
    is at least visibly odd. The quieter failure is a status that *has* a case whose body is a
    copy of another's: ``not_attempted`` returning the ``not_found`` sentence passes any check
    that only counts cases, and it re-collapses "nobody was asked" into "asked and found
    nothing" — the exact distinction the four-way split exists to preserve, and the one this
    file's sibling test on the backend asserts in prose.

    So the bodies are compared for uniqueness, not just for presence. This was written after a
    mutation that duplicated ``not_found``'s body into ``not_attempted`` passed the presence-only
    version of this test.
    """
    source = INGESTED_PLACES_LIB.read_text(encoding="utf-8")
    body = source.split("export function resolvedPlaceWording", 1)[1]

    # Split the switch into (status, body) pairs. Anchored on `case "..."` and running to the
    # next `case`/`default`, which is enough for this function's shape.
    cases = re.findall(
        r'case "([a-z_]+)":(.*?)(?=\n\s*case "|\n\s*default:)',
        body,
        re.DOTALL,
    )
    assert cases, "no cases parsed, so this check would be vacuous"

    from travel_api.domain.models import PlaceResolutionStatus

    expected = {member.value for member in PlaceResolutionStatus}
    handled = {name for name, _ in cases}
    assert handled == expected, (
        f"unhandled statuses fall through to a generic sentence: {sorted(expected - handled)}"
    )

    # Normalise whitespace so a copy that was reindented still counts as a copy.
    normalised = {name: " ".join(text.split()) for name, text in cases}
    seen: dict[str, str] = {}
    duplicates = []
    for name in sorted(normalised):
        text = normalised[name]
        if text in seen:
            duplicates.append((name, seen[text]))
        else:
            seen[text] = name

    assert not duplicates, (
        "these statuses render the identical sentence, so the outcome they distinguish is lost: "
        + "; ".join(f"{a} == {b}" for a, b in duplicates)
    )


FRESHNESS_LIB = Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "freshness.ts"
TRIP_STORE_LIB = Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "trip-store.ts"
TRIP_PAGE = (
    Path(__file__).resolve().parents[2] / "apps" / "web" / "app" / "trips" / "[id]" / "page.tsx"
)


def test_the_web_knows_every_freshness_basis_the_server_can_report():
    """``ObservationBasis`` in the API against the four constants in ``freshness.ts``.

    This is the 时效性 hard requirement's own vocabulary, and the one where a rename is
    quietest. The web writes the bases as named constants and branches on them; anything it
    does not recognise falls to the ``default:`` branch, which says 数据时间未知. That is the
    honest answer for a datum whose age really is unknown — so if ``provider_reported`` were
    renamed, every source that *does* publish a timestamp would start reporting 数据时间未知,
    the panel would look careful and conservative, and the feature would be gone.

    The values are compared, not the constant names: the names are the web's business, the
    strings on the wire are the contract.
    """
    from travel_api.domain.models import ObservationBasis

    source = FRESHNESS_LIB.read_text(encoding="utf-8")
    web_values = set(
        re.findall(r'^export const BASIS_[A-Z_]+ = "([a-z_]+)";', source, re.MULTILINE)
    )
    assert web_values, f"could not find the BASIS_ constants in {FRESHNESS_LIB.name}"

    expected = {member.value for member in ObservationBasis}
    assert web_values == expected, (
        "the web and the API disagree about freshness bases. "
        f"web only: {sorted(web_values - expected)}; api only: {sorted(expected - web_values)}"
    )


def test_the_web_and_the_api_agree_which_basis_makes_the_data_age_known():
    """The rule itself, not just the vocabulary, has to be the same on both sides.

    The 时效性 rule is: only a source that *stamped* its own datum lets you speak about the
    age of the data; a live computation and a silent source both tell you only that we asked
    recently. The API encodes it once (``ObservationBasis.age_is_about_the_data``) and the web
    encodes it again (``dataAgeIsKnown``), because one is Python and one is TypeScript.

    If the two ever disagree, the disagreement is invisible on both sides: each is internally
    consistent, and the web's panel would simply be reassuring about a datum the server knows
    is old. So the web's rule is read and compared to the API's.
    """
    from travel_api.domain.models import ObservationBasis

    source = FRESHNESS_LIB.read_text(encoding="utf-8")
    body = source.split("export function dataAgeIsKnown", 1)[1].split("}", 1)[0]
    referenced = set(re.findall(r"BASIS_[A-Z_]+", body))
    assert referenced, "could not read `dataAgeIsKnown`; the check would be vacuous"

    # Map the web's constant names back to their string values.
    constants = dict(
        re.findall(r'^export const (BASIS_[A-Z_]+) = "([a-z_]+)";', source, re.MULTILINE)
    )
    web_known = {constants[name] for name in referenced if name in constants}

    api_known = {member.value for member in ObservationBasis if member.age_is_about_the_data}
    assert web_known == api_known, (
        "the web and the API disagree about which freshness basis makes the data's age known. "
        f"web treats these as known: {sorted(web_known)}; the API: {sorted(api_known)}"
    )


def test_the_web_knows_every_itinerary_item_type_the_server_can_produce():
    """A type the web cannot name is printed as its identifier on every itinerary line.

    ``typeNames[item.item_type] ?? item.item_type`` is the fallback, so an unknown type shows
    ``transfer`` in the middle of a Chinese itinerary rather than nothing — visible, and easy
    to mistake for a deliberate technical term.
    """
    from travel_api.domain.models import ItineraryItemType

    source = TRIP_PAGE.read_text(encoding="utf-8")
    match = re.search(r"const typeNames: Record<string, string> = \{(.*?)\};", source, re.DOTALL)
    assert match, f"could not find `typeNames` in {TRIP_PAGE.name}"
    declared = set(re.findall(r"([a-z_]+):", match.group(1)))
    assert declared, "an empty table means the scan is wrong and this check is vacuous"

    expected = {member.value for member in ItineraryItemType}
    assert declared == expected, (
        "the itinerary shows item types the API cannot produce, or cannot name one it can. "
        f"web only: {sorted(declared - expected)}; api only: {sorted(expected - declared)}"
    )


def test_the_web_knows_every_conversation_role_the_server_can_send():
    """A role the web cannot name makes the chat history **empty**, not merely mislabelled.

    The history is filtered to ``role === "user" || role === "assistant"`` before rendering, so
    a server-side rename of either role does not produce a wrong label — it produces a panel
    with nothing in it, and the traveller concludes their conversation was lost. That is a
    strictly worse failure than an unlabelled chip, which is why this one is checked too.
    """
    from travel_api.domain.models import ConversationRole

    source = TRIP_STORE_LIB.read_text(encoding="utf-8")
    # `Message` is an object type written on a single line, so its `role` member is found by
    # scoping the search to the `Message` declaration and then reading the union inside it —
    # anchoring on line starts would silently find nothing.
    message_match = re.search(r"^type Message = \{(.*?)\};", source, re.MULTILINE | re.DOTALL)
    assert message_match, f"could not find `Message` in {TRIP_STORE_LIB.name}"
    role_match = re.search(r"\brole:\s*([^;]+);", message_match.group(1))
    assert role_match, f"could not find `role` inside `Message` in {TRIP_STORE_LIB.name}"
    roles = set(re.findall(r'"([a-z_]+)"', role_match.group(1)))
    assert roles, "reading the role union produced nothing, so the check would be vacuous"

    expected = {member.value for member in ConversationRole}
    assert roles == expected, (
        "the web and the API disagree about conversation roles. "
        f"web only: {sorted(roles - expected)}; api only: {sorted(expected - roles)}"
    )


def test_the_web_knows_every_extraction_confidence_the_server_can_report():
    """``confidenceLabel``'s ``default:`` returns the raw value, so a new level is a raw word.

    Confidence wording is deliberately about the reading rather than the truth (识别明确 /
    识别一般 / 仅为线索), which means it cannot be derived from the value on the client — the
    mapping has to exist on both sides.
    """
    from travel_api.domain.ingestion import ExtractionConfidence

    source = INGESTIONS_LIB.read_text(encoding="utf-8")
    body = source.split("export function confidenceLabel", 1)[1]
    handled = set(re.findall(r'case "([a-z_]+)":', body.split("\n}", 1)[0]))
    assert handled, "could not read `confidenceLabel`'s cases; the check would be vacuous"

    expected = {member.value for member in ExtractionConfidence}
    assert handled == expected, (
        "the web cannot word every confidence the API can report. "
        f"unhandled: {sorted(expected - handled)}; handled but not in the API: "
        f"{sorted(handled - expected)}"
    )


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


def test_the_web_knows_every_planning_step_the_server_can_emit():
    """``STEP_LABELS`` against the step names actually emitted, read from the server's source.

    This one was found by looking rather than by a failure report: the ``resolve_places`` step
    was added to ``aggregation.py`` in the same change that added the feature behind it, and the
    web's label table was not updated. Nothing broke. The progress line rendered a raw
    ``resolve_places`` between two Chinese labels, which a reader has no way to distinguish from
    a deliberate technical term — and ``planning-jobs.ts`` has a fallback that *intends* to show
    unknown names, so the omission looked like the fallback working.

    The step names are not an enum; they are string literals passed to ``reporter.step``, so the
    server's own sources are scanned for them. That is crude, and it is the only thing that can
    see a literal nobody registered anywhere. A new ``.step("...")`` call is covered the moment
    it is written, which is the property that was missing.
    """
    source_root = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "travel_api"
    emitted: set[str] = set()
    # Both call shapes, because the two exist for a reason (one is inside a service that may
    # have no reporter, the other is a direct call) and a scan that knew only one would report
    # a false clean bill of health.
    patterns = (
        re.compile(r'\.step\(\s*\n?\s*"([a-z_]+)"'),
        re.compile(r'_maybe_step\(\s*progress,\s*"([a-z_]+)"'),
    )
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            emitted.update(pattern.findall(text))

    assert len(emitted) >= 8, f"the scan found only {len(emitted)} step names: {sorted(emitted)}"
    assert "resolve_places" in emitted, "the scan must see the step this test was written for"

    web = PLANNING_JOBS_LIB.read_text(encoding="utf-8")
    match = re.search(r"export const STEP_LABELS[^{]*\{(.*?)\n\};", web, re.DOTALL)
    assert match, f"could not find `STEP_LABELS` in {PLANNING_JOBS_LIB.name}"
    labelled = set(re.findall(r"^\s*([a-z_]+):", match.group(1), re.MULTILINE))
    assert labelled, "an empty table means the scan is wrong and this check is vacuous"

    assert labelled == emitted, (
        "the web's step labels and the steps the server emits disagree. "
        f"unlabelled (would render as a raw identifier): {sorted(emitted - labelled)}; "
        f"stale (labelled but never emitted): {sorted(labelled - emitted)}"
    )


def test_every_step_label_is_chinese_prose_rather_than_the_identifier():
    """The label must say something a user can read, not echo the key.

    ``stepLabel`` falls back to the raw name, so a label that *is* the name is
    indistinguishable from the fallback — the entry would look present and do nothing.
    """
    web = PLANNING_JOBS_LIB.read_text(encoding="utf-8")
    match = re.search(r"export const STEP_LABELS[^{]*\{(.*?)\n\};", web, re.DOTALL)
    assert match
    pairs = re.findall(r'^\s*([a-z_]+):\s*"([^"]*)"', match.group(1), re.MULTILINE)
    assert pairs, "no label pairs parsed"

    for name, label in pairs:
        assert label != name, f"{name} is labelled with its own identifier"
        assert any("\u4e00" <= character <= "\u9fff" for character in label), (
            f"{name} has no Chinese in its label ({label!r}), so it reads as an identifier"
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


CATALOG_TEST = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "web"
    / "app"
    / "components"
    / "provider-catalog.test.tsx"
)


def _ts_string_array(source: str, field: str) -> list[str]:
    """The string literals of one array field in the first object that has it."""
    match = re.search(rf"{field}:\s*\[([^\]]*)\]", source, re.DOTALL)
    assert match, f"could not find {field}: [...] in the fixture"
    return re.findall(r'"([^"]*)"', match.group(1))


def test_the_settings_page_fixture_matches_the_source_it_is_supposed_to_describe(tmp_path):
    """The web's 高德 fixture must be the shape the server actually sends.

    This is the fixture-drift defect, and it is the same family as the 成都 constant: a test
    that describes a state the world has left behind cannot fail, so the behaviour it was
    meant to protect goes unchecked while the suite stays green.

    Concretely, every rendering test in ``provider-catalog.test.tsx`` described 高德 as
    ``planned`` — a source listed and declared unimplemented — long after the AMap provider
    was implemented. Among other consequences, ``unlockHints`` therefore never populated its
    ``configurable`` list (the only other inactive entry is the AI model, which has no
    capabilities), so the sentence

        高德地图（AMap）：填入凭据后可提供路线规划：公交/地铁

    never appeared in any test. That sentence is the most actionable thing the settings page
    says to a traveller planning in China, and the entire reason 高德 is integrated.

    So the comparison is made against the real catalogue rather than against a second
    hand-written copy: the deployed fixture is read out of the TSX and checked against
    ``/api/v1/providers/catalog``. If the AMap manifest changes again, this fails and names
    the difference, instead of the settings page quietly going stale.
    """
    from fastapi.testclient import TestClient
    from travel_api.config import Settings
    from travel_api.main import create_app

    source = CATALOG_TEST.read_text(encoding="utf-8")
    # The deployed fixture, not `CATALOG`. `AMAP_AS_DEPLOYED` is a standalone object literal,
    # so it is isolated by slicing from its declaration to the end of the literal.
    start = source.index("const AMAP_AS_DEPLOYED")
    end = source.index("\n};", start)
    deployed = source[start:end]

    assert 'status: "needs_credentials"' in deployed, (
        "the deployed 高德 fixture must carry the status the server reports; `planned` is "
        "the pre-implementation state and claiming it makes the screen describe code that "
        "does not exist"
    )

    # A fresh app with the real free stack, because the shape being checked is what the
    # keyless deployment reports — the module-wide `client` fixture runs on mocks, which
    # would describe 高德 differently and defeat the comparison. The database is under
    # `tmp_path` so running the suite leaves nothing in the checkout.
    with TestClient(
        create_app(
            Settings(
                app_env="test",
                database_url=f"sqlite:///{tmp_path / 'catalog-contract.db'}",
                auto_create_schema=True,
                auth_enabled=False,
                free_providers_enabled=True,
                mock_provider_enabled=False,
                provider_fixture_dir=None,
            )
        )
    ) as client:
        response = client.get("/api/v1/providers/catalog")
    assert response.status_code == 200, response.text
    catalog = response.json()

    real = next(item for item in catalog["providers"] if item["id"] == "amap")
    assert real["status"] == "needs_credentials", (
        "this test assumes an unconfigured deployment; if that is no longer how 高德 reports "
        "itself, update the fixture and this expectation together"
    )

    assert _ts_string_array(deployed, "capabilities") == real["capabilities"], (
        "the fixture and the server disagree about which capabilities 高德 provides"
    )

    # The aspect list is the one that matters most: it is what decides whether the settings
    # page promises 公交/地铁 (which a key really does buy) and whether it wrongly offers
    # 骑行 (which no key can buy, because TravelMode has no cycling member).
    fixture_modes = re.findall(r"calculate_routes:\s*\[([^\]]*)\]", deployed)
    assert fixture_modes, "the deployed fixture declares no routing aspects"
    fixture_aspects = re.findall(r'"([a-z]+)"', fixture_modes[0])
    real_aspects = real["aspects"]["calculate_routes"]
    assert fixture_aspects == real_aspects, (
        f"the fixture claims aspects {fixture_aspects} but the server declares {real_aspects}"
    )
    assert "transit" in real_aspects, "高德 must declare 公交/地铁; it is the reason it exists"
    assert "cycling" not in real_aspects, (
        "高德 must not declare cycling: TravelMode cannot request it, so a key would not "
        "unlock it and the settings page would promise something unreachable"
    )


def test_the_matrix_fixture_lists_every_capability_the_server_sends(tmp_path):
    """The other half of the same drift, and the one that hid the most important row.

    ``CATALOG.capabilities`` used to hold three entries, all ``covered: true``, while
    ``CATALOG.uncovered_capabilities`` and ``CATALOG.capability_gaps`` named a fourth —
    ``search_room_offers`` — that had no row at all. The server sends nine.

    Two things followed, and neither was visible:

      * the ``无来源`` branch of the matrix never rendered in any test, and that branch is the
        label on the one capability nothing can serve — the row a reader goes to the matrix to
        find;
      * a fixture that is internally inconsistent (a gap naming a row that does not exist) is a
        fixture whose failures cannot be interpreted: a wrong count could be the component's
        fault or the fixture's, and nothing distinguished them.

    So the comparison is against the live response rather than against the fixture's own idea
    of itself. Capability ids and labels both, in order, because the matrix is a list the user
    reads top to bottom.
    """
    from fastapi.testclient import TestClient
    from travel_api.config import Settings
    from travel_api.main import create_app

    source = CATALOG_TEST.read_text(encoding="utf-8")
    # The `capabilities: [...]` block of `CATALOG` (the shared fixture), which ends where
    # `registered:` begins.
    start = source.index("  capabilities: [")
    end = source.index("  registered: [", start)
    block = source[start:end]

    # Each row is `capability: "<id>"` followed by `label: "<label>"`, in that order.
    fixture_rows = re.findall(
        r'capability:\s*"([a-z_]+)",\s*\n\s*label:\s*"([^"]+)"',
        block,
    )
    assert fixture_rows, "could not parse any capability rows out of the fixture"

    with TestClient(
        create_app(
            Settings(
                app_env="test",
                database_url=f"sqlite:///{tmp_path / 'matrix-contract.db'}",
                auto_create_schema=True,
                auth_enabled=False,
                free_providers_enabled=True,
                mock_provider_enabled=False,
                provider_fixture_dir=None,
            )
        )
    ) as client:
        response = client.get("/api/v1/providers/catalog")
    assert response.status_code == 200, response.text
    real_rows = [(row["capability"], row["label"]) for row in response.json()["capabilities"]]

    assert fixture_rows == real_rows, (
        "the settings-page fixture's capability matrix has drifted from what the server "
        "sends. A row missing here is a capability the reading tests never render.\n"
        f"  fixture only: {[r for r in fixture_rows if r not in real_rows]}\n"
        f"  server only:  {[r for r in real_rows if r not in fixture_rows]}"
    )

    # And the fixture's own gap report must name rows that exist in it. This is the internal
    # inconsistency that made the drift invisible rather than merely stale.
    fixture_ids = {capability for capability, _label in fixture_rows}
    uncovered = _ts_string_array(block + source[end:], "uncovered_capabilities")
    assert uncovered, "the fixture reports no uncovered capability, so this checks nothing"
    missing_rows = [item for item in uncovered if item not in fixture_ids]
    assert not missing_rows, (
        f"the fixture names {missing_rows} as uncovered but gives them no matrix row, so the "
        "panel and the matrix disagree about what exists"
    )


def _ts_gap_capabilities(source: str) -> list[str]:
    """The ``capability`` ids inside the fixture's ``capability_gaps`` array.

    Not ``_ts_string_array``: in this array ``capability`` is a scalar field of each entry, not
    an array, so the array-shaped helper finds nothing.
    """
    start = source.index("capability_gaps: [")
    end = source.index("\n  ],", start)
    return re.findall(r'capability:\s*"([a-z_]+)"', source[start:end])


def test_the_room_rate_fixture_matches_the_source_that_unlocks_it(tmp_path):
    """RollingGo's fixture must be the shape the server sends, for the reason 高德's must.

    The defect is the same one documented above: with no RollingGo entry in the settings-page
    fixture, ``unlockHints`` had no inactive source declaring ``search_room_offers``, so the
    screen's only answer to "how do I stop guessing the accommodation cost" was never rendered
    by any test. A fixture that omits the source the advice is about cannot fail.
    """
    from fastapi.testclient import TestClient
    from travel_api.config import Settings
    from travel_api.main import create_app

    source = CATALOG_TEST.read_text(encoding="utf-8")
    start = source.index("const ROLLINGGO_AS_DEPLOYED")
    end = source.index("\n};", start)
    deployed = source[start:end]

    assert 'status: "needs_credentials"' in deployed, (
        "the deployed RollingGo fixture must carry the status the server reports; `planned` is "
        "the pre-implementation state and claiming it makes the screen describe code that does "
        "not exist. Asserted on the fixture itself, not only on the server's value below — a "
        "comparison that only reads the server cannot see the copy drift."
    )

    with TestClient(
        create_app(
            Settings(
                app_env="test",
                database_url=f"sqlite:///{tmp_path / 'rollinggo-contract.db'}",
                auto_create_schema=True,
                auth_enabled=False,
                free_providers_enabled=True,
                mock_provider_enabled=False,
                provider_fixture_dir=None,
            )
        )
    ) as client:
        response = client.get("/api/v1/providers/catalog")
    assert response.status_code == 200, response.text
    catalog = response.json()

    real = next(item for item in catalog["providers"] if item["id"] == "rollinggo")
    assert real["status"] == "needs_credentials", (
        "this test assumes an unconfigured deployment; if RollingGo reports itself differently "
        "now, update the fixture and this expectation together"
    )
    assert _ts_string_array(deployed, "capabilities") == real["capabilities"], (
        "the fixture and the server disagree about which capabilities RollingGo provides"
    )
    assert _ts_string_array(deployed, "missing_credentials") == real["missing_credentials"]


def test_the_fixture_gap_list_names_exactly_the_gaps_the_server_reports(tmp_path):
    """The drift this actually hit, and the check that would have caught it.

    The fixture's ``capability_gaps`` listed room rates as a dead end. The server stopped saying
    that the moment RollingGo was added — the capability became *unlockable*, which is a
    different list with a different remedy — and nothing noticed, because the contract test above
    compared the matrix *rows* and never this list.

    The ids are compared, not the notes: the fixture legitimately shortens note text, so
    comparing prose would fail for a reason that is not a drift. Which capabilities are gaps is
    the part that must agree, because that is what decides what the screen tells the reader to do.
    """
    from fastapi.testclient import TestClient
    from travel_api.config import Settings
    from travel_api.main import create_app

    source = CATALOG_TEST.read_text(encoding="utf-8")
    fixture_gaps = _ts_gap_capabilities(source)
    assert fixture_gaps, "could not parse any capability_gaps out of the fixture"

    with TestClient(
        create_app(
            Settings(
                app_env="test",
                database_url=f"sqlite:///{tmp_path / 'gap-contract.db'}",
                auto_create_schema=True,
                auth_enabled=False,
                free_providers_enabled=True,
                mock_provider_enabled=False,
                provider_fixture_dir=None,
            )
        )
    ) as client:
        response = client.get("/api/v1/providers/catalog")
    assert response.status_code == 200, response.text

    server_gaps = [gap["capability"] for gap in response.json()["capability_gaps"]]
    assert fixture_gaps == server_gaps, (
        "the settings-page fixture's gap list has drifted from the server's. A gap that no "
        "longer exists is advice the reader cannot act on; a missing one is a dead end they "
        "will not be told about.\n"
        f"  fixture only: {[g for g in fixture_gaps if g not in server_gaps]}\n"
        f"  server only:  {[g for g in server_gaps if g not in fixture_gaps]}"
    )
