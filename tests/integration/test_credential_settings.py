"""End-to-end tests for the credential settings endpoints.

These go through HTTP rather than calling the vault directly, because the
promises being checked are about the API: that a value can be written but never
read back, that one owner cannot see or disturb another's credentials, and that
a saved credential actually changes the model in use without a restart.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app

OWNER_A_KEY = "trv" + "_" + "owner_a_key_0000000000000000"
SECRET = "sk-live-abcdef-should-never-be-echoed"


def _settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'settings.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        provider_retry_count=0,
        free_providers_enabled=False,
        mock_provider_enabled=True,
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
    """Mint a real key per owner, since only the plaintext can authenticate."""
    a, _ = client.app.state.auth_service.mint("owner-a", label="owner-a")
    b, _ = client.app.state.auth_service.mint("owner-b", label="owner-b")
    return {"a": a, "b": b}


# -- health reports the vault honestly ------------------------------------


def test_health_reports_the_vault_as_ready(client):
    with TestClient(client.app) as test_client:
        payload = test_client.get("/api/v1/health").json()
    assert payload["credential_vault"] == "ready"
    assert payload["credential_key_source"] in {"file", "env"}


# -- writing, reading, deleting -------------------------------------------


def test_a_credential_can_be_stored_and_is_never_read_back(client, owners):
    response = client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        json={"value": SECRET},
        headers=_auth(owners["a"]),
    )
    assert response.status_code == 200
    # The response must acknowledge without echoing the secret.
    assert SECRET not in response.text
    assert response.json()["stored"] is True

    listing = client.get("/api/v1/settings/credentials", headers=_auth(owners["a"]))
    assert listing.status_code == 200
    assert SECRET not in listing.text
    assert listing.json()["credentials"]["openai-compatible"]["ai_api_key"] == "stored"


def test_an_unknown_provider_is_rejected(client, owners):
    response = client.put(
        "/api/v1/settings/credentials/not-a-provider/ai_api_key",
        json={"value": SECRET},
        headers=_auth(owners["a"]),
    )
    assert response.status_code == 404


def test_an_undeclared_credential_name_is_rejected(client, owners):
    """Otherwise the vault becomes general-purpose storage and typos save silently."""
    response = client.put(
        "/api/v1/settings/credentials/openai-compatible/not_a_real_key",
        json={"value": SECRET},
        headers=_auth(owners["a"]),
    )
    assert response.status_code == 404
    assert "does not declare" in response.json()["detail"]


def test_a_declared_credential_uses_its_manifest_name(client, owners):
    """The manifest is the single source of truth for what can be configured."""
    from travel_api.providers.manifest import manifest_for

    for credential in manifest_for("openai-compatible").credentials:
        response = client.put(
            f"/api/v1/settings/credentials/openai-compatible/{credential.key}",
            json={"value": "placeholder-value"},
            headers=_auth(owners["a"]),
        )
        assert response.status_code == 200, credential.key


def test_an_empty_value_is_rejected(client, owners):
    response = client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        json={"value": ""},
        headers=_auth(owners["a"]),
    )
    assert response.status_code == 422


def test_a_credential_can_be_deleted(client, owners):
    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        json={"value": SECRET},
        headers=_auth(owners["a"]),
    )
    removed = client.delete(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        headers=_auth(owners["a"]),
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] is True

    listing = client.get("/api/v1/settings/credentials", headers=_auth(owners["a"]))
    assert "openai-compatible" not in listing.json()["credentials"]

    again = client.delete(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        headers=_auth(owners["a"]),
    )
    assert again.json()["removed"] is False


# -- authentication is required -------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/settings/credentials"),
        ("put", "/api/v1/settings/credentials/openai-compatible/ai_api_key"),
        ("delete", "/api/v1/settings/credentials/openai-compatible/ai_api_key"),
        ("post", "/api/v1/settings/model/test"),
    ],
)
def test_credential_routes_require_authentication(client, method, path):
    # Only the write verbs take a body; TestClient.get() has no json= parameter.
    kwargs = {"json": {"value": SECRET}} if method in {"put", "post"} else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


def test_a_read_only_key_cannot_write_credentials(client):
    read_only, _ = client.app.state.auth_service.mint("reader", scopes=("trip:read",))
    response = client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        json={"value": SECRET},
        headers=_auth(read_only),
    )
    assert response.status_code == 403


# -- tenant isolation ------------------------------------------------------


def test_one_owner_cannot_see_another_owners_credentials(client, owners):
    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        json={"value": SECRET},
        headers=_auth(owners["a"]),
    )

    listing = client.get("/api/v1/settings/credentials", headers=_auth(owners["b"]))
    assert listing.status_code == 200
    assert listing.json()["credentials"] == {}


def test_deleting_as_another_owner_does_not_remove_the_credential(client, owners):
    """A cross-tenant delete must be a no-op, not a deletion."""
    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        json={"value": SECRET},
        headers=_auth(owners["a"]),
    )

    intruder = client.delete(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        headers=_auth(owners["b"]),
    )
    assert intruder.json()["removed"] is False

    still_there = client.get("/api/v1/settings/credentials", headers=_auth(owners["a"]))
    assert still_there.json()["credentials"]["openai-compatible"]["ai_api_key"] == "stored"


# -- the catalogue reflects stored credentials ----------------------------


def test_storing_a_base_url_marks_the_model_active_in_the_catalogue(client, owners):
    """Proves a saved credential actually changes what the deployment can do."""
    before = client.get("/api/v1/providers/catalog", headers=_auth(owners["a"])).json()
    entry = next(i for i in before["providers"] if i["id"] == "openai-compatible")
    assert entry["active"] is False

    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_base_url",
        json={"value": "http://127.0.0.1:11434/v1"},
        headers=_auth(owners["a"]),
    )

    after = client.get("/api/v1/providers/catalog", headers=_auth(owners["a"])).json()
    entry = next(i for i in after["providers"] if i["id"] == "openai-compatible")
    assert entry["active"] is True
    assert entry["missing_credentials"] == []


def test_the_catalogue_never_carries_a_stored_secret(client, owners):
    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_base_url",
        json={"value": "http://127.0.0.1:11434/v1"},
        headers=_auth(owners["a"]),
    )
    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_api_key",
        json={"value": SECRET},
        headers=_auth(owners["a"]),
    )

    response = client.get("/api/v1/providers/catalog", headers=_auth(owners["a"]))
    assert response.status_code == 200
    assert SECRET not in response.text


def test_the_model_takes_effect_without_a_restart(client, owners):
    """The whole point of resolving per request: save, then use it immediately."""
    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_base_url",
        json={"value": "http://127.0.0.1:11434/v1"},
        headers=_auth(owners["a"]),
    )
    # Read the runtime the running app is actually using, without rebuilding it.
    model_runtime = client.app.state.model_runtime
    resolved = model_runtime.resolve("owner-a")
    assert resolved.adapter is not None
    assert resolved.adapter.base_url == "http://127.0.0.1:11434/v1"
    assert resolved.sources["ai_base_url"] == "stored"


def test_two_owners_can_use_different_models_in_one_deployment(client, owners):
    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_base_url",
        json={"value": "https://a.invalid/v1"},
        headers=_auth(owners["a"]),
    )
    client.put(
        "/api/v1/settings/credentials/openai-compatible/ai_base_url",
        json={"value": "https://b.invalid/v1"},
        headers=_auth(owners["b"]),
    )

    model_runtime = client.app.state.model_runtime
    assert model_runtime.resolve("owner-a").adapter.base_url == "https://a.invalid/v1"
    assert model_runtime.resolve("owner-b").adapter.base_url == "https://b.invalid/v1"


# -- the model test endpoint ----------------------------------------------


def test_testing_an_unconfigured_model_explains_the_planner_still_works(client, owners):
    response = client.post("/api/v1/settings/model/test", headers=_auth(owners["a"]))
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "no model configured" in payload["error"]


# -- the vault being locked is reported, not hidden ----------------------


def test_a_locked_vault_is_reported_and_blocks_storing(tmp_path):
    """Writing must fail loudly when there is no usable key.

    Silently storing plaintext, or accepting a write that cannot be read back,
    would both be worse than a clear 503.
    """
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'locked.db'}",
        auto_create_schema=True,
        auth_enabled=False,
        provider_retry_count=0,
        free_providers_enabled=False,
        mock_provider_enabled=True,
        # Insufficient: an AES-256 key is 32 bytes.
        credential_encryption_key="dG9vLXNob3J0",
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/v1/health").json()
        assert health["credential_vault"] == "locked"
        assert health["credential_key_source"] == "unavailable"

        response = client.put(
            "/api/v1/settings/credentials/openai-compatible/ai_api_key",
            json={"value": SECRET},
        )
        assert response.status_code == 503
        assert SECRET not in response.text
