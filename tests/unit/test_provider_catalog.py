"""Tests for the provider catalogue and the capability matrix.

The catalogue is what the settings page renders and what answers "what can this
deployment actually do". A catalogue that lists capabilities nothing implements,
or provider names nothing registers, is worse than no catalogue: it is a
confident lie. So these tests check the catalogue against the real code rather
than against itself.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app
from travel_api.providers.base import CAPABILITY_BY_METHOD
from travel_api.providers.free import build_free_providers
from travel_api.providers.manifest import (
    CAPABILITY_ASPECTS,
    PROVIDER_CATALOG,
    STATUS_AVAILABLE,
    STATUS_NEEDS_CREDENTIALS,
    STATUS_PLANNED,
    capability_matrix,
    catalog_as_dicts,
    manifest_for,
    uncovered_aspects,
    unknown_capabilities,
)


def _free_stack_names() -> set[str]:
    """Provider names the real free stack registers, built the way main.py builds it."""
    providers, _http = build_free_providers()
    return {getattr(provider, "name", "") for provider in providers}


# -- the catalogue cannot drift from the code ------------------------------


def test_no_manifest_declares_a_capability_the_registry_cannot_route():
    """A capability nothing routes would make the matrix a wish list.

    Asserts against ``CAPABILITY_BY_METHOD`` directly as well as through the
    helper, so the check does not rest solely on the code it is checking.
    """
    declared = {capability for manifest in PROVIDER_CATALOG for capability in manifest.capabilities}
    routable = set(CAPABILITY_BY_METHOD)
    assert declared <= routable, f"declared but not routable: {sorted(declared - routable)}"
    assert unknown_capabilities() == []


def test_every_declared_provider_name_is_actually_registered():
    """Catches a typo'd registry name, which would silently mark a live source absent.

    Checks against the stack ``build_free_providers`` really returns, so renaming
    a provider without updating its manifest fails here instead of quietly
    reporting the capability as unavailable.

    A credential-gated source is checked against the objects its own factory builds.
    That is the *stronger* check of the two: it proves the name is real, and it also
    proves the factory still runs at all, which a name comparison against a constant
    would not.
    """
    registered = _free_stack_names()
    keyless = {
        name
        for manifest in PROVIDER_CATALOG
        for name in manifest.provider_names
        if manifest.status == STATUS_AVAILABLE
    }
    assert keyless <= registered, f"declared but not registered: {sorted(keyless - registered)}"

    # Credential-gated names must come from the real factory, not just be spelled
    # the same way somewhere.
    credential_gated = {
        name
        for manifest in PROVIDER_CATALOG
        for name in manifest.provider_names
        if manifest.status == STATUS_NEEDS_CREDENTIALS
    }
    if credential_gated:
        assert credential_gated <= _credentialed_stack_names(), (
            f"declared but not built: {sorted(credential_gated - _credentialed_stack_names())}"
        )


def _credentialed_stack_names() -> set[str]:
    """Names the credential-gated factory really produces, for the AMap source.

    Uses a placeholder key: this asserts construction and naming, never a real call.
    """
    from travel_api.providers.amap import build_amap_providers

    providers = build_amap_providers(key="test-key-not-used-for-requests", http=object())
    return {getattr(provider, "name", "") for provider in providers}


def _amap_stack() -> list:
    from travel_api.providers.amap import build_amap_providers

    return build_amap_providers(key="test-key-not-used-for-requests", http=object())


def test_every_declared_capability_reaches_a_live_provider_that_implements_it():
    """The end-to-end anti-drift check: declaration must imply reachability.

    Every earlier test here compares names and protocols. This one asks the registry
    the question the planner asks — "give me the providers for this capability" — and
    requires a non-empty answer for each capability an implemented source declares.

    This is the check that would have caught 高德's hotel POIs being unreachable:
    ``search_hotels`` was declared, and the provider implemented the method, but
    ``CAPABILITY_BY_METHOD`` pointed at a protocol that also required room rates, so
    ``providers_for("search_hotels")`` returned nothing. Declaration, implementation
    and routing all looked correct in isolation; only asking the registry exposed it.
    """
    from travel_api.providers.registry import ProviderRegistry

    providers, _http = build_free_providers()
    registry = ProviderRegistry([*providers, *_amap_stack()])

    unreachable: list[str] = []
    for manifest in PROVIDER_CATALOG:
        if not manifest.is_usable or not manifest.provider_names:
            continue
        for capability in manifest.capabilities:
            served = registry.providers_for(capability)
            if not any(
                getattr(provider, "name", "") in manifest.provider_names for provider in served
            ):
                unreachable.append(f"{manifest.id}:{capability}")

    assert not unreachable, (
        "declared capabilities that no live provider is routed for, so the matrix "
        f"would advertise them while nothing can execute them: {sorted(unreachable)}"
    )


def test_a_source_that_lists_hotels_without_rates_is_still_routed_hotels():
    """Finding hotels and pricing them are separate abilities, and were not.

    ``CAPABILITY_BY_METHOD["search_hotels"]`` used to point at a protocol that also
    demanded ``search_room_offers``. A source that could list hotels but had no rates
    — which is exactly 高德, whose hotel POIs come with no prices — therefore failed
    the ``isinstance`` check and was never routed for hotels at all. Its manifest
    advertised ``search_hotels`` while the call could never reach it: a capability
    that existed only on paper.

    Both directions are asserted, because the fix must not go the other way either:
    a source with no rates must not be handed a request for rates.
    """
    from travel_api.providers.base import CAPABILITY_BY_METHOD, HotelProvider, RoomOfferProvider
    from travel_api.providers.registry import ProviderRegistry

    hotel_only = _amap_stack()[1]  # the POI provider: hotels, restaurants, attractions
    assert isinstance(hotel_only, HotelProvider)
    assert not isinstance(hotel_only, RoomOfferProvider), (
        "高德 returns hotel POIs and no room rates; claiming otherwise would let the "
        "budget layer present an invented price as a real one"
    )

    registry = ProviderRegistry([hotel_only])
    assert registry.providers_for("search_hotels") == [hotel_only]
    assert registry.providers_for("search_room_offers") == []

    # And the mapping itself must keep them apart; pointing both at one protocol is
    # the specific mistake this test exists to catch.
    assert CAPABILITY_BY_METHOD["search_hotels"] is not CAPABILITY_BY_METHOD["search_room_offers"]


def test_a_sources_providers_do_not_share_one_registry_name():
    """The registry is keyed by name, so a shared name silently drops providers.

    高德's routing, POI and weather providers were all named ``"amap"``. Registering
    them therefore kept only the last one and discarded the other two *without any
    error* — and because the capability matrix was driven by the manifest's
    declaration rather than by what actually registered, the catalogue went on
    reporting 公交/地铁 as covered while no routing provider existed to serve it.

    Nothing failed. A keyed deployment simply planned with less data than it
    advertised, which is the exact failure this project's honesty rules exist to
    prevent. Distinct names are what the registry assumes of every other source, so
    this asserts the invariant directly rather than waiting for a plan to be slow or
    a matrix to be wrong.
    """
    names = [getattr(provider, "name", None) for provider in _amap_stack()]

    assert all(names), f"every provider needs a name, got {names}"
    assert len(set(names)) == len(names), (
        f"providers share a registry name, so some would be dropped: {names}"
    )


def _amap_stack() -> list:
    from travel_api.providers.amap import build_amap_providers

    return build_amap_providers(key="test-key-not-used-for-requests", http=object())


def test_the_matrix_does_not_credit_a_capability_with_no_live_provider():
    """A source must not be credited with capabilities it cannot currently serve.

    The check is tightened by passing which providers are genuinely live per
    capability, as the catalogue route now does from the registry itself. Here the
    weather provider alone is live: 高德's weather is covered, but its routing and
    POI capabilities must be reported as gaps rather than inherited from the same
    declaration.
    """
    from travel_api.providers.amap import AMAP_WEATHER_PROVIDER_NAME

    live = {"search_weather": {AMAP_WEATHER_PROVIDER_NAME}}
    matrix = {
        row["capability"]: row for row in capability_matrix([AMAP_WEATHER_PROVIDER_NAME], live)
    }

    assert matrix["search_weather"]["covered"] is True
    # The other 高德 capabilities are declared by the same manifest, but nothing live
    # serves them, so they must not be reported as covered.
    assert matrix["search_hotels"]["covered"] is False
    assert matrix["search_hotels"]["providers"] == []
    assert matrix["calculate_routes"]["covered"] is False
    assert "transit" in matrix["calculate_routes"]["missing_aspects"]


def test_a_source_needing_credentials_contributes_no_capabilities_until_configured():
    """高德 is implemented but inert without a key, so it must not appear as coverage.

    The failure this guards against is subtle and self-flattering: the matrix would
    show 公交/地铁 covered because the catalogue happens to mention 高德, while in
    reality nothing in a zero-credential deployment can plan a subway journey. That
    is precisely the overstatement this matrix exists to prevent.

    Note ``is_usable`` means *implemented*, not *active*: 高德 is usable in that sense
    while still being absent from a keyless deployment's coverage.
    """
    amap = manifest_for("amap")
    assert amap is not None
    assert amap.status == STATUS_NEEDS_CREDENTIALS
    assert amap.is_usable, "needs_credentials means implemented, so it is usable once keyed"
    assert amap.credentials, "it must declare the credential that activates it"

    # The factory really does build it, so the name is not decorative.
    assert set(amap.provider_names) <= _credentialed_stack_names()

    # But with only the keyless stack registered it is not coverage.
    matrix = capability_matrix(registered_names=_free_stack_names())
    for row in matrix:
        assert "amap" not in row["providers"], (
            "a source with no credential must not count as coverage"
        )

    # Routing is the case that matters: 公交/地铁 must still be reported as a gap.
    aspects = uncovered_aspects(_free_stack_names())
    assert any(item["aspect"] == "transit" for item in aspects), (
        "transit must stay an uncovered aspect until a key is configured"
    )


def test_every_source_that_needs_credentials_declares_the_field_to_enter():
    """The settings page can only ask for what the manifest declares.

    A source in this state with no credential field would be impossible to activate
    from the UI, so it would read as permanently unavailable.
    """
    needing = [m for m in PROVIDER_CATALOG if m.status == STATUS_NEEDS_CREDENTIALS]
    assert needing, "this test is vacuous unless some source needs credentials"
    for manifest in needing:
        assert manifest.credentials, f"{manifest.id} needs credentials but declares none"
        assert any(field.required for field in manifest.credentials), (
            f"{manifest.id} declares only optional credentials, so nothing would activate it"
        )
        assert manifest.caveats, f"{manifest.id} must explain what is missing"


def test_the_amap_manifest_no_longer_claims_to_be_unimplemented():
    """The old text said 尚未实现. Leaving it would understate a working source, which
    is the mirror image of the overstatement the matrix is meant to prevent."""
    amap = manifest_for("amap")
    assert amap is not None
    assert not any("尚未实现" in caveat for caveat in amap.caveats)
    assert any("Key" in caveat for caveat in amap.caveats), (
        "the caveats must say plainly that a key of your own is required"
    )
    # The no-caching rule is a licence obligation, so it is stated rather than implied.
    assert any("缓存" in caveat for caveat in amap.caveats)


# -- the matrix reflects what is registered --------------------------------


def test_a_capability_is_uncovered_when_nothing_is_registered():
    matrix = {row["capability"]: row for row in capability_matrix(registered_names=())}
    assert not matrix["search_hotels"]["covered"]
    assert matrix["search_hotels"]["providers"] == []


def test_a_capability_is_covered_when_its_source_is_registered():
    matrix = {
        row["capability"]: row for row in capability_matrix(registered_names={"openstreetmap"})
    }
    assert matrix["search_hotels"]["covered"] is True
    assert matrix["search_hotels"]["providers"] == ["openstreetmap"]
    # A capability whose source is absent must stay uncovered.
    assert matrix["search_weather"]["covered"] is False


def test_the_real_free_stack_covers_the_core_capabilities():
    """Zero-config must be genuinely usable, which is the point of the defaults."""
    matrix = {row["capability"]: row["covered"] for row in capability_matrix(_free_stack_names())}
    for capability in (
        "search_hotels",
        "search_restaurants",
        "search_attractions",
        "calculate_routes",
        "search_weather",
        "search_holidays",
        "enrich_attractions",
    ):
        assert matrix[capability] is True, f"{capability} should work with no credentials"


# -- partial coverage must not be reported as full -------------------------
#
# Routing is why this exists. OSRM answers ``calculate_routes``, so a flat model
# reports routing as covered — while the public demo has no transit at all. The
# matrix would then say everything is covered and a source that adds 公交/地铁 would
# look pointless, which is the opposite of true for a domestic itinerary.


def test_osrm_is_not_credited_with_transit_routing():
    """No OSRM deployment can plan 公交/地铁, so it must not be recorded as doing so.

    Asserted against the provider's own declared aspects and the resulting matrix, because the
    tempting mistake — listing every aspect the capability name implies — is exactly what would
    make the matrix self-flattering. Driving and walking are served; transit and cycling are
    not, and the last two are the ones a reader could otherwise be misled about.
    """
    osrm = manifest_for("osrm")
    assert osrm is not None
    served = dict(osrm.aspects).get("calculate_routes", ())
    # Walking is served on every deployment: from a real foot profile where the server has one,
    # and otherwise from the car network's *distance* with the duration derived and flagged.
    # Either way a usable, honestly-labelled walking leg is produced, so it counts as covered.
    assert set(served) == {"driving", "walking"}, (
        f"OSRM declares {served}, which does not match what it can produce"
    )
    assert "transit" not in served, "no OSRM server has timetable data"
    assert "cycling" not in served, "TravelMode cannot request a bike leg"

    matrix = {row["capability"]: row for row in capability_matrix(_free_stack_names())}
    aspect_rows = {row["aspect"]: row for row in matrix["calculate_routes"]["aspects"]}
    assert aspect_rows["driving"]["covered"] is True
    assert aspect_rows["walking"]["covered"] is True
    assert aspect_rows["transit"]["covered"] is False
    assert aspect_rows["transit"]["providers"] == []
    assert aspect_rows["cycling"]["covered"] is False


def test_a_capability_with_a_missing_aspect_is_covered_but_not_complete():
    """Two different facts, reported separately so neither hides the other.

    ``covered`` is what the deployment can do today; ``complete`` is whether it can
    do all of it. Collapsing them either hides the transit gap or denies that
    driving works.
    """
    row = next(
        item
        for item in capability_matrix(_free_stack_names())
        if item["capability"] == "calculate_routes"
    )
    assert row["covered"] is True, "driving works, so routing is not absent"
    assert row["complete"] is False, "transit is missing, so routing is not complete"
    # Only the two nothing can serve. Walking used to be listed here, back when the only
    # deployment available was car-only and its walking times had to be derived; it is now
    # measured on the default deployment and no longer a gap.
    assert set(row["missing_aspects"]) == {"transit", "cycling"}


def test_uncovered_aspects_names_the_gap_and_what_it_belongs_to():
    gaps = uncovered_aspects(_free_stack_names())
    by_aspect = {gap["aspect"]: gap for gap in gaps}
    assert "transit" in by_aspect
    assert by_aspect["transit"]["label"] == "公交/地铁"
    assert by_aspect["transit"]["capability_label"] == "路线规划"
    # Driving is served, so it must not be reported as a gap.
    assert "driving" not in by_aspect


def test_an_atomic_capability_reports_no_aspects():
    """Capabilities that do not decompose keep working exactly as before."""
    row = next(
        item
        for item in capability_matrix(_free_stack_names())
        if item["capability"] == "search_hotels"
    )
    assert row["aspects"] == []
    assert row["missing_aspects"] == []
    assert row["complete"] is True


def test_every_declared_aspect_belongs_to_a_declared_capability():
    """An aspect under a capability nobody declares is dead configuration."""
    for manifest in PROVIDER_CATALOG:
        for capability, served in manifest.aspects:
            assert capability in manifest.capabilities, (
                f"{manifest.id} declares aspects for {capability}, which it does not serve"
            )
            known = CAPABILITY_ASPECTS.get(capability)
            assert known is not None, (
                f"{manifest.id} declares aspects for {capability}, which has no aspect list"
            )
            unknown = set(served) - set(known)
            assert not unknown, f"{manifest.id} declares unknown aspects of {capability}: {unknown}"


def test_a_manifest_that_decomposes_declares_at_least_one_aspect():
    """Omitting ``aspects`` means "serves none", which would be a silent retraction.

    A source that lists a decomposable capability but no aspects would be counted
    as covering it while contributing nothing to any part of it — so it must
    either name what it serves or be caught here.
    """
    for manifest in PROVIDER_CATALOG:
        if not manifest.is_usable:
            continue
        declared = dict(manifest.aspects)
        for capability in manifest.capabilities:
            if capability in CAPABILITY_ASPECTS:
                assert declared.get(capability), (
                    f"{manifest.id} serves {capability}, which decomposes, but names no aspects"
                )


# -- serialisation ---------------------------------------------------------


def test_catalog_never_contains_a_credential_value():
    """The endpoint must be safe to render: names and flags, never secrets."""
    secret = "sk-do-not-leak-this-value"
    catalog = catalog_as_dicts(
        registered_names=(),
        configured_credentials={"openai-compatible": {"ai_api_key", "ai_model"}},
    )
    serialised = json.dumps(catalog, ensure_ascii=False)
    assert secret not in serialised
    assert "configured" in serialised

    entry = next(item for item in catalog if item["id"] == "openai-compatible")
    by_key = {item["key"]: item for item in entry["credentials"]}
    assert by_key["ai_api_key"]["configured"] is True
    # The key name is exposed; a value never is.
    assert "value" not in by_key["ai_api_key"]
    assert by_key["ai_base_url"]["configured"] is False


def test_missing_credentials_are_reported_for_required_fields_only():
    catalog = catalog_as_dicts(
        registered_names=(),
        configured_credentials={"openai-compatible": {"ai_base_url", "ai_model"}},
    )
    entry = next(item for item in catalog if item["id"] == "openai-compatible")
    # ai_api_key is optional, so its absence is not a blocker.
    assert entry["missing_credentials"] == []


def test_a_model_reports_active_once_its_base_url_is_set():
    """A model has no registry name, so registry presence cannot decide this.

    Gated on the base URL, mirroring ``main.py``: a keyless local server (Ollama,
    LM Studio) is a valid configuration, so requiring a key would report a working
    local model as inactive.
    """

    def active(configured: set[str]) -> bool:
        catalog = catalog_as_dicts(
            registered_names=(),
            configured_credentials={"openai-compatible": configured},
        )
        entry = next(item for item in catalog if item["id"] == "openai-compatible")
        return entry["active"]

    assert active({"ai_base_url"}) is True
    assert active({"ai_base_url", "ai_api_key", "ai_model"}) is True
    # A key with nowhere to send it is not a usable model.
    assert active({"ai_api_key"}) is False
    assert active(set()) is False
    # The model name has a default and cannot activate a model on its own.
    assert active({"ai_model"}) is False


def test_available_sources_declare_their_freshness():
    """Timeliness decides whether a datum is usable for a future trip.

    Every usable source has to state how current its data is; a source that
    cannot answer that has no business presenting itself as usable.
    """
    for manifest in PROVIDER_CATALOG:
        if manifest.status == STATUS_AVAILABLE:
            assert manifest.freshness_note, f"{manifest.id} has no freshness note"


def test_catalogued_statuses_are_from_the_known_set():
    known = {STATUS_AVAILABLE, STATUS_NEEDS_CREDENTIALS, STATUS_PLANNED}
    for manifest in PROVIDER_CATALOG:
        assert manifest.status in known, f"{manifest.id} has status {manifest.status!r}"


def test_free_sources_need_no_credentials():
    """Zero-config is a promise, so a keyless source must not demand a key."""
    for manifest in PROVIDER_CATALOG:
        if manifest.status == STATUS_AVAILABLE:
            assert manifest.credentials == ()


# -- HTTP layer ------------------------------------------------------------


def _client(tmp_path) -> TestClient:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        auto_create_schema=True,
        auth_enabled=False,
        free_providers_enabled=True,
        mock_provider_enabled=False,
        provider_fixture_dir=None,
    )
    return TestClient(create_app(settings))


def test_catalog_endpoint_reports_a_usable_deployment(tmp_path):
    with _client(tmp_path) as client:
        response = client.get("/api/v1/providers/catalog")

    assert response.status_code == 200
    payload = response.json()
    # 房型与报价 is genuinely uncovered, and this assertion used to claim otherwise.
    #
    # The old expectation was `uncovered_capabilities == []`, which held only because
    # Overpass implements `search_room_offers` as a method that always returns nothing and
    # was counted as coverage. So the catalogue asserted that a deployment which cannot
    # obtain a room rate was fully covered — the test was pinning the bug.
    #
    # One gap is the honest answer for this deployment: rates require a commercial hotel
    # channel, and none is integrated. Everything else is served by keyless sources.
    assert payload["uncovered_capabilities"] == ["search_room_offers"]
    # And the gap is named with a reason, not just counted, so a reader learns what to plug
    # in rather than having to diff the matrix against their expectations.
    gap = next(
        item for item in payload["capability_gaps"] if item["capability"] == "search_room_offers"
    )
    assert gap["label"]
    assert "估算" in gap["note"], "the note must say what happens instead of a real rate"
    assert "openstreetmap" in payload["registered"]
    ids = {item["id"] for item in payload["providers"]}
    assert {"openstreetmap", "amap", "openai-compatible"} <= ids


def test_a_provider_that_always_returns_nothing_is_not_credited_with_coverage(tmp_path):
    """The rule behind the assertion above, stated directly.

    Implementing a protocol method is not the same as serving it. OpenStreetMap carries no
    rates, so its `search_room_offers` returns an empty success — correct behaviour, because
    inventing a price would be worse — but an empty success must not be reported as a
    capability.
    """
    with _client(tmp_path) as client:
        payload = client.get("/api/v1/providers/catalog").json()

    row = next(
        item for item in payload["capabilities"] if item["capability"] == "search_room_offers"
    )
    assert row["covered"] is False
    assert row["complete"] is False
    assert row["providers"] == []

    # The same source *is* credited where it genuinely delivers, so the fix does not simply
    # silence the provider.
    for capability in ("search_hotels", "search_restaurants", "search_attractions"):
        served = next(item for item in payload["capabilities"] if item["capability"] == capability)
        assert served["covered"] is True, capability
        assert served["providers"] == ["openstreetmap"]

    # And the provider itself still declares the hotel capability it does serve.
    entry = next(item for item in payload["providers"] if item["id"] == "openstreetmap")
    assert "search_hotels" in entry["capabilities"]
    assert "search_room_offers" not in entry["capabilities"]


def test_catalog_endpoint_does_not_leak_a_configured_secret(tmp_path):
    secret = "sk-live-should-never-be-returned"
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'leak.db'}",
        auto_create_schema=True,
        auth_enabled=False,
        free_providers_enabled=True,
        mock_provider_enabled=False,
        ai_base_url="https://example.invalid/v1",
        ai_api_key=secret,
        ai_model="test-model",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/providers/catalog")

    assert response.status_code == 200
    assert secret not in response.text
    entry = next(item for item in response.json()["providers"] if item["id"] == "openai-compatible")
    assert entry["missing_credentials"] == []
