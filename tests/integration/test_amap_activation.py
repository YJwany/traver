"""End-to-end tests for per-owner AMap activation.

This is the test that proves the three requirements actually meet, through HTTP and
the real application rather than through the pieces separately:

1. **时效性 / honesty** — a zero-credential deployment reports 公交/地铁 as a *gap*
   rather than pretending the capability exists.
2. **路线要好** — once one owner supplies a key, that owner's catalogue reports the
   transit aspect as closed.
3. **Not boasting** — the other owner's catalogue does not, because the key is theirs
   alone and the registry is scoped per request.

The registry is built once at startup while a key belongs to one person, so this is
the seam where an isolation bug would appear: the same process must answer
differently for two owners in the same moment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app
from travel_api.providers.amap import (
    AMAP_KEY_CREDENTIAL,
    AMAP_PROVIDER_ID,
    AMAP_PROVIDER_NAMES,
)

SECRET_KEY = "amap-web-service-key-must-never-be-echoed"


def _settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'amap.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        provider_retry_count=0,
        # The free stack is off so that the only possible source of route coverage is
        # AMap itself. With it on, a passing test could be explained by OSRM instead.
        free_providers_enabled=False,
        mock_provider_enabled=False,
        credential_key_file=str(tmp_path / "credential.key"),
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as test_client:
        yield test_client


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def owners(client):
    a, _ = client.app.state.auth_service.mint("owner-a", label="a")
    b, _ = client.app.state.auth_service.mint("owner-b", label="b")
    return {"a": a, "b": b}


def _catalog(client, key: str) -> dict:
    response = client.get("/api/v1/providers/catalog", headers=_auth(key))
    assert response.status_code == 200
    return response.json()


def _row(catalog: dict, capability: str) -> dict:
    return next(row for row in catalog["capabilities"] if row["capability"] == capability)


def _aspect(catalog: dict, capability: str, aspect: str) -> dict:
    """One aspect row. ``aspects`` is a list of rows, not a mapping."""
    return next(row for row in _row(catalog, capability)["aspects"] if row["aspect"] == aspect)


def _entry(catalog: dict, provider_id: str) -> dict:
    return next(item for item in catalog["providers"] if item["id"] == provider_id)


def _field(catalog: dict, provider_id: str, credential_key: str) -> dict:
    return next(
        field
        for field in _entry(catalog, provider_id)["credentials"]
        if field["key"] == credential_key
    )


def _is_registered(catalog: dict) -> bool:
    """Whether 高德's providers are actually live in this owner's registry.

    Checked against the per-provider names, not the ``"amap"`` source id: the
    registry keys providers by name, so all three being present is the thing that
    matters. Asserting only the source id is what let a bug through in which two of
    the three providers were silently dropped while the catalogue still looked right.
    """
    return set(AMAP_PROVIDER_NAMES) <= set(catalog["registered"])


# -- with no key: the gap must be visible ---------------------------------


def test_without_a_key_transit_is_reported_as_an_uncovered_aspect(client, owners):
    """The honest zero-configuration answer.

    The public OSRM demo has no transit profile, so a deployment with no 高德 key
    genuinely cannot plan a subway journey. Saying so is the whole point of tracking
    aspects separately from capabilities.
    """
    catalog = _catalog(client, owners["a"])

    assert not _is_registered(catalog)
    routing = _row(catalog, "calculate_routes")
    # The capability is uncovered outright here, because nothing at all is registered.
    assert routing["covered"] is False
    assert "amap" not in routing["providers"]
    # Every routing aspect is a gap, transit included.
    assert "transit" in routing["missing_aspects"]

    # And the 高德 entry must still be listed, as something the owner can turn on.
    assert _entry(catalog, "amap")["status"] == "needs_credentials"


def test_a_key_is_never_needed_to_produce_a_catalogue(client, owners):
    """渐进增强: nothing about the catalogue may require a credential."""
    catalog = _catalog(client, owners["a"])
    assert catalog["providers"], "the catalogue must be populated with no credentials at all"


# -- with a key: transit closes for that owner only -----------------------


def test_saving_a_key_activates_amap_for_that_owner(client, owners):
    """A key saved through the settings page must take effect immediately.

    No restart, and no reliance on a startup-time registry: the provider is built per
    request from the vault. A cached provider instance would keep the key it was first
    built with, which is the "I saved my key and it still doesn't work" bug.
    """
    before = _catalog(client, owners["a"])
    assert not _is_registered(before)

    stored = client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": SECRET_KEY},
        headers=_auth(owners["a"]),
    )
    assert stored.status_code == 200
    assert SECRET_KEY not in stored.text

    after = _catalog(client, owners["a"])
    assert _is_registered(after), "the key must activate the source on the next request"
    routing = _row(after, "calculate_routes")
    assert routing["covered"] is True
    assert "amap" in routing["providers"]
    # 公交/地铁 is the reason 高德 is here, so the aspect must now be closed.
    assert _aspect(after, "calculate_routes", "transit")["covered"] is True
    assert "transit" not in routing["missing_aspects"]


def test_activating_amap_does_not_claim_cycling(client, owners):
    """The matrix must not overstate what the key unlocked.

    ``TravelMode`` has no cycling member, so nothing can request a cycling route and
    高德's cycling aspect was deliberately left unclaimed. Once a key is present the
    capability reads as covered, and this asserts the partial coverage is still
    reported as partial rather than rounded up to "everything works".
    """
    client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": SECRET_KEY},
        headers=_auth(owners["a"]),
    )
    catalog = _catalog(client, owners["a"])
    routing = _row(catalog, "calculate_routes")

    assert routing["covered"] is True
    assert routing["complete"] is False, "cycling is not claimed, so coverage is not complete"
    assert routing["missing_aspects"] == ["cycling"]
    assert _aspect(catalog, "calculate_routes", "cycling")["covered"] is False
    # And the gap is reported at the top level too, for the UI's unlock panel.
    assert any(
        item["aspect"] == "cycling"
        for item in catalog["uncovered_aspects"]
        if item["capability"] == "calculate_routes"
    )


def test_one_owners_key_does_not_become_anothers_capability(client, owners):
    """The isolation invariant, checked in the same instant for both owners.

    Two different keys are presented to the same process against the same registry
    object. If the registry were scoped once at startup, or if the ambient owner were
    left bound after a request, one of these two assertions would fail.
    """
    client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": SECRET_KEY},
        headers=_auth(owners["a"]),
    )

    catalog_a = _catalog(client, owners["a"])
    catalog_b = _catalog(client, owners["b"])

    assert _is_registered(catalog_a)
    assert not _is_registered(catalog_b), (
        "owner-b has supplied nothing and must not inherit owner-a's source"
    )
    assert _row(catalog_b, "calculate_routes")["covered"] is False

    # And the ordering must not matter: re-read owner-b first, then owner-a.
    assert not _is_registered(_catalog(client, owners["b"]))
    assert _is_registered(_catalog(client, owners["a"]))


def test_removing_the_key_withdraws_the_capability_again(client, owners):
    """A removed key must stop being used. With a cached provider it would not, and the
    catalogue would keep advertising a capability the owner can no longer exercise."""
    path = f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}"
    client.put(path, json={"value": SECRET_KEY}, headers=_auth(owners["a"]))
    assert _is_registered(_catalog(client, owners["a"]))

    removed = client.delete(path, headers=_auth(owners["a"]))
    assert removed.status_code == 200

    after = _catalog(client, owners["a"])
    assert not _is_registered(after)
    assert _row(after, "calculate_routes")["covered"] is False


def test_the_key_is_never_echoed_by_the_catalogue(client, owners):
    """The catalogue is rendered in a browser and may be cached or logged, so a key
    leaking into it would be a real exposure rather than a cosmetic one."""
    client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": SECRET_KEY},
        headers=_auth(owners["a"]),
    )

    response = client.get("/api/v1/providers/catalog", headers=_auth(owners["a"]))
    assert SECRET_KEY not in response.text
    # Also check the fragment an over-eager implementation might include.
    assert SECRET_KEY[:8] not in response.text


def test_the_catalogue_reports_the_vault_state_honestly(client, owners):
    """The 高德 caveats include a licence obligation not to cache its data, so the
    vault and caching disclosures must be present rather than assumed."""
    catalog = _catalog(client, owners["a"])
    vault = catalog["vault"]
    # Reported, not assumed: the vault may be disabled, and on Windows the key file
    # cannot be made owner-only, so the payload says so rather than implying more
    # protection than exists.
    assert isinstance(vault["enabled"], bool)
    assert vault["key_source"] in {"env", "file", "unavailable"}
    assert isinstance(vault["owner_only_permissions"], bool)
    if not vault["enabled"]:
        assert vault["locked_reason"], "a disabled vault must say why"

    amap_entry = _entry(catalog, "amap")
    assert amap_entry["caveats"], "the UI needs the caveats to explain the trade-offs"
    assert any("缓存" in caveat for caveat in amap_entry["caveats"]), (
        "the no-caching obligation must be stated to the user"
    )
    assert amap_entry["credentials"], "the settings page renders the fields from here"
    assert _field(catalog, "amap", AMAP_KEY_CREDENTIAL)["required"] is True


def test_a_stored_key_is_reported_as_stored_not_as_verified_working(client, owners):
    """A stored value is not a working value.

    The settings page must be able to distinguish "nothing entered" from "entered but
    not yet proven", because the remedy differs: one is finding where to get a key,
    the other is re-entering it. Here the key is invalid as far as 高德 is concerned,
    and the catalogue still only claims it is stored.
    """
    before = _catalog(client, owners["a"])
    assert _field(before, "amap", AMAP_KEY_CREDENTIAL)["configured"] is False
    assert AMAP_KEY_CREDENTIAL in _entry(before, "amap")["missing_credentials"]

    client.put(
        f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
        json={"value": "obviously-not-a-valid-key"},
        headers=_auth(owners["a"]),
    )
    after = _catalog(client, owners["a"])

    assert _field(after, "amap", AMAP_KEY_CREDENTIAL)["configured"] is True
    assert AMAP_KEY_CREDENTIAL not in _entry(after, "amap")["missing_credentials"]
    # Nothing in the payload may assert the key is valid — only that a value exists.
    assert "obviously-not-a-valid-key" not in str(after)


def test_disable_amap_is_respected(tmp_path):
    """An operator must be able to withdraw the source without editing code."""
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'disabled.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        free_providers_enabled=False,
        mock_provider_enabled=False,
        amap_enabled=False,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    with TestClient(create_app(settings)) as local:
        key, _ = local.app.state.auth_service.mint("owner-a", label="a")
        local.put(
            f"/api/v1/settings/credentials/{AMAP_PROVIDER_ID}/{AMAP_KEY_CREDENTIAL}",
            json={"value": SECRET_KEY},
            headers=_auth(key),
        )
        catalog = _catalog(local, key)

    assert not _is_registered(catalog), (
        "AMAP_ENABLED=false must prevent the provider being built even with a stored key"
    )
    assert _row(catalog, "calculate_routes")["covered"] is False
