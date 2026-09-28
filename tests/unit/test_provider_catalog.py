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
    PROVIDER_CATALOG,
    STATUS_AVAILABLE,
    STATUS_NEEDS_CREDENTIALS,
    STATUS_PLANNED,
    capability_matrix,
    catalog_as_dicts,
    manifest_for,
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
    """
    registered = _free_stack_names()
    declared = {
        name
        for manifest in PROVIDER_CATALOG
        for name in manifest.provider_names
        if manifest.status != STATUS_PLANNED
    }
    assert declared <= registered, f"declared but not registered: {sorted(declared - registered)}"


def test_planned_sources_contribute_no_capabilities():
    """高德 is declared but not written yet, so it must not appear as coverage.

    The failure this guards against is subtle and would be self-flattering: the
    matrix would show every capability covered because the catalogue happens to
    mention a source that does it, while in reality nothing does.
    """
    amap = manifest_for("amap")
    assert amap is not None
    assert amap.status == STATUS_PLANNED
    assert not amap.is_usable

    matrix = capability_matrix(registered_names=_free_stack_names())
    for row in matrix:
        assert "amap" not in row["providers"], "a planned source must not count as coverage"


def test_planned_source_is_reported_with_its_blockers():
    """The UI needs to explain what is missing, not just that something is."""
    amap = manifest_for("amap")
    assert amap is not None
    assert amap.credentials, "a planned source must still declare what it will need"
    assert any("尚未实现" in caveat for caveat in amap.caveats), (
        "the caveats must say plainly that it is not implemented"
    )


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


def test_a_model_reports_active_only_when_its_activation_keys_are_present():
    """A model has no registry name, so registry presence cannot decide this.

    Reporting a configured model as inactive would be a falsehood in the UI, and
    the activation condition has to mirror ``main.py``: the adapter is built only
    when the base URL *and* the key are both set.
    """

    def active(configured: set[str]) -> bool:
        catalog = catalog_as_dicts(
            registered_names=(),
            configured_credentials={"openai-compatible": configured},
        )
        entry = next(item for item in catalog if item["id"] == "openai-compatible")
        return entry["active"]

    assert active({"ai_base_url", "ai_api_key"}) is True
    assert active({"ai_base_url", "ai_api_key", "ai_model"}) is True
    # Base URL alone is not enough: main.py still leaves the adapter unbuilt.
    assert active({"ai_base_url"}) is False
    assert active(set()) is False


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
    assert payload["uncovered_capabilities"] == [], (
        "the default deployment should leave no capability uncovered"
    )
    assert "openstreetmap" in payload["registered"]
    ids = {item["id"] for item in payload["providers"]}
    assert {"openstreetmap", "amap", "openai-compatible"} <= ids


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
