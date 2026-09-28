"""Authentication and tenant isolation.

These are security tests, so they assert the negative cases too: a key must not
read or write another owner's trip, and a revoked key must stop working
immediately.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app
from travel_api.services.auth import (
    KEY_PREFIX,
    AuthenticationError,
    AuthService,
    Principal,
    display_prefix,
    generate_api_key,
    hash_api_key,
)

# -- key material -----------------------------------------------------------


def test_generated_keys_are_unique_and_prefixed():
    keys = {generate_api_key() for _ in range(200)}
    assert len(keys) == 200, "key generation collided"
    for key in keys:
        assert key.startswith(KEY_PREFIX)
        assert len(key) > 40, "key is too short to resist guessing"


def test_hashing_is_deterministic_and_not_reversible():
    key = generate_api_key()
    assert hash_api_key(key) == hash_api_key(key)
    assert len(hash_api_key(key)) == 64
    # The digest must not contain the secret.
    assert key not in hash_api_key(key)


def test_display_prefix_is_short_enough_to_be_safe():
    key = generate_api_key()
    prefix = display_prefix(key)
    assert len(prefix) < len(key) / 2
    assert key.startswith(prefix)
    # It must not be usable as a credential by itself.
    assert prefix != key


# -- principal scopes ------------------------------------------------------


def test_principal_without_scopes_can_do_everything():
    principal = Principal(owner_id="alice")
    assert principal.can("trip:read")
    assert principal.can("trip:write")


def test_principal_scopes_are_enforced():
    read_only = Principal(owner_id="alice", scopes=("trip:read",))
    assert read_only.can("trip:read")
    assert not read_only.can("trip:write")


# -- app fixture -----------------------------------------------------------


def _auth_settings(tmp_path, **overrides):
    base = {
        "app_env": "test",
        "database_url": f"sqlite:///{tmp_path / 'auth.db'}",
        "auto_create_schema": True,
        "provider_retry_count": 0,
        "free_providers_enabled": False,
        "mock_provider_enabled": True,
        "auth_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)


def _trip_body():
    return {
        "destination": "成都",
        "start_date": "2026-10-03",
        "end_date": "2026-10-05",
        "travelers": 2,
        "total_budget": 6000,
    }


def _mint(app, owner: str, **kwargs) -> str:
    """Mint a key directly through the app's own AuthService."""
    return app.state.auth_service.mint(owner, **kwargs)[0]


# -- enforcement -----------------------------------------------------------


def test_trip_routes_reject_missing_credentials(tmp_path):
    with TestClient(create_app(_auth_settings(tmp_path))) as client:
        assert client.get("/api/v1/trips").status_code == 401
        assert client.post("/api/v1/trips", json=_trip_body()).status_code == 401


def test_401_carries_www_authenticate(tmp_path):
    with TestClient(create_app(_auth_settings(tmp_path))) as client:
        response = client.get("/api/v1/trips")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert "detail" in response.json()


def test_unknown_and_malformed_keys_are_rejected(tmp_path):
    with TestClient(create_app(_auth_settings(tmp_path))) as client:
        # Well-formed prefix but never issued.
        unknown = generate_api_key()
        response = client.get("/api/v1/trips", headers={"Authorization": f"Bearer {unknown}"})
        assert response.status_code == 401

        # Wrong shape entirely (e.g. a JWT pasted by mistake).
        response = client.get("/api/v1/trips", headers={"Authorization": "Bearer not-a-traver-key"})
        assert response.status_code == 401

        # A non-Bearer scheme must not be treated as a credential.
        response = client.get("/api/v1/trips", headers={"Authorization": f"Basic {unknown}"})
        assert response.status_code == 401


def test_valid_key_grants_access(tmp_path):
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app) as client:
        key = _mint(app, "alice")
        response = client.get("/api/v1/trips", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert response.json() == []


# -- tenant isolation ------------------------------------------------------


def test_trips_are_invisible_across_owners(tmp_path):
    """The core isolation guarantee: one owner cannot see another's trip."""
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app) as client:
        alice = _mint(app, "alice")
        bob = _mint(app, "bob")

        created = client.post(
            "/api/v1/trips", json=_trip_body(), headers={"Authorization": f"Bearer {alice}"}
        )
        assert created.status_code == 200, created.text
        trip_id = created.json()["id"]

        # Alice sees it.
        assert (
            client.get(
                f"/api/v1/trips/{trip_id}", headers={"Authorization": f"Bearer {alice}"}
            ).status_code
            == 200
        )
        # Bob does not, and is told 404 rather than 403 so ids are not confirmed.
        bob_read = client.get(
            f"/api/v1/trips/{trip_id}", headers={"Authorization": f"Bearer {bob}"}
        )
        assert bob_read.status_code == 404, bob_read.text

        # Bob's list does not include it.
        bob_list = client.get("/api/v1/trips", headers={"Authorization": f"Bearer {bob}"})
        assert bob_list.json() == []


def test_cross_owner_writes_are_refused(tmp_path):
    """Isolation must hold for every mutating verb, not just reads."""
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app) as client:
        alice = _mint(app, "alice")
        bob = _mint(app, "bob")
        alice_headers = {"Authorization": f"Bearer {alice}"}
        bob_headers = {"Authorization": f"Bearer {bob}"}

        created = client.post("/api/v1/trips", json=_trip_body(), headers=alice_headers)
        trip_id = created.json()["id"]

        assert (
            client.patch(
                f"/api/v1/trips/{trip_id}", json={"travelers": 5}, headers=bob_headers
            ).status_code
            == 404
        )
        assert (
            client.post(f"/api/v1/trips/{trip_id}/refresh", headers=bob_headers).status_code == 404
        )
        assert (
            client.post(
                f"/api/v1/trips/{trip_id}/revision",
                json={"action": "change_pace", "constraints": {"pace": "relaxed"}},
                headers=bob_headers,
            ).status_code
            == 404
        )
        for suffix in ("budget", "itinerary", "sources", "snapshots", "changes", "conversation"):
            assert (
                client.get(f"/api/v1/trips/{trip_id}/{suffix}", headers=bob_headers).status_code
                == 404
            ), f"{suffix} leaked across owners"

        # Alice's trip is unchanged after all those refused writes.
        still = client.get(f"/api/v1/trips/{trip_id}", headers=alice_headers).json()
        assert still["profile"]["travelers"] == 2


def test_ai_chat_cannot_touch_another_owners_trip(tmp_path):
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app) as client:
        alice = _mint(app, "alice")
        bob = _mint(app, "bob")
        created = client.post(
            "/api/v1/trips",
            json=_trip_body(),
            headers={"Authorization": f"Bearer {alice}"},
        )
        trip_id = created.json()["id"]
        response = client.post(
            "/api/v1/ai/chat",
            json={"trip_id": trip_id, "message": "换一个酒店"},
            headers={"Authorization": f"Bearer {bob}"},
        )
    assert response.status_code in (403, 404), response.text


# -- revocation and scopes -------------------------------------------------


def test_revoked_key_stops_working_immediately(tmp_path):
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app) as client:
        key, key_id = app.state.auth_service.mint("alice")
        headers = {"Authorization": f"Bearer {key}"}
        assert client.get("/api/v1/trips", headers=headers).status_code == 200

        assert app.state.auth_service.revoke(key_id) is True
        assert client.get("/api/v1/trips", headers=headers).status_code == 401

        # Revoking twice reports that nothing changed.
        assert app.state.auth_service.revoke(key_id) is False


def test_read_only_key_cannot_create_or_modify(tmp_path):
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app) as client:
        admin = _mint(app, "alice")
        read_only = _mint(app, "alice", scopes=("trip:read",))
        admin_headers = {"Authorization": f"Bearer {admin}"}
        ro_headers = {"Authorization": f"Bearer {read_only}"}

        created = client.post("/api/v1/trips", json=_trip_body(), headers=admin_headers)
        trip_id = created.json()["id"]

        # Reads are allowed.
        assert client.get("/api/v1/trips", headers=ro_headers).status_code == 200
        assert client.get(f"/api/v1/trips/{trip_id}", headers=ro_headers).status_code == 200

        # Writes are not.
        assert (
            client.post("/api/v1/trips", json=_trip_body(), headers=ro_headers).status_code == 403
        )
        assert (
            client.patch(
                f"/api/v1/trips/{trip_id}", json={"travelers": 3}, headers=ro_headers
            ).status_code
            == 403
        )


def test_unknown_scope_is_rejected_at_mint_time(tmp_path):
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app):
        with pytest.raises(ValueError, match="unknown scope"):
            app.state.auth_service.mint("alice", scopes=("trip:delete",))


def test_mint_requires_an_owner(tmp_path):
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app):
        with pytest.raises(ValueError, match="owner_id is required"):
            app.state.auth_service.mint("   ")


# -- public endpoints stay public ------------------------------------------


def test_health_and_attributions_need_no_key(tmp_path):
    """Probes and the Web shell must reach these without a credential."""
    with TestClient(create_app(_auth_settings(tmp_path))) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/providers/attributions").status_code == 200


def test_health_reports_auth_mode(tmp_path):
    with TestClient(create_app(_auth_settings(tmp_path))) as client:
        payload = client.get("/api/v1/health").json()
    assert payload["authentication"] == "api-key"
    assert payload["ready"] is True

    # And the unsecured mode is advertised rather than hidden.
    with TestClient(create_app(_auth_settings(tmp_path, auth_enabled=False))) as client:
        assert client.get("/api/v1/health").json()["authentication"] == "disabled"


# -- disabled mode ---------------------------------------------------------


def test_auth_disabled_keeps_single_user_mode_working(tmp_path):
    """Local development must not require key management."""
    settings = _auth_settings(
        tmp_path, auth_enabled=False, database_url=f"sqlite:///{tmp_path / 'open.db'}"
    )
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/v1/trips", json=_trip_body())
        assert created.status_code == 200, created.text
        assert client.get("/api/v1/trips").status_code == 200


def test_keys_from_a_disabled_app_are_not_accepted_when_enabled(tmp_path):
    """Turning auth on must not leave previously-open data world-readable."""
    open_settings = _auth_settings(
        tmp_path, auth_enabled=False, database_url=f"sqlite:///{tmp_path / 'shared.db'}"
    )
    with TestClient(create_app(open_settings)) as client:
        created = client.post("/api/v1/trips", json=_trip_body())
        assert created.status_code == 200

    # Same database, auth now on: the anonymous-mode trip is not visible without
    # a credential.
    closed_settings = _auth_settings(
        tmp_path, auth_enabled=True, database_url=f"sqlite:///{tmp_path / 'shared.db'}"
    )
    with TestClient(create_app(closed_settings)) as client:
        assert client.get("/api/v1/trips").status_code == 401


# -- bootstrap -------------------------------------------------------------


def test_bootstrap_key_is_created_and_usable(tmp_path):
    key = "trv_" + "b" * 40
    settings = _auth_settings(tmp_path, auth_bootstrap_key=key, auth_bootstrap_owner="admin")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/trips", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200


def test_bootstrap_key_without_prefix_is_ignored_not_trusted(tmp_path):
    """A malformed bootstrap value must not become a working credential."""
    settings = _auth_settings(tmp_path, auth_bootstrap_key="not-a-traver-key")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/trips").status_code == 401
        # And nothing was stored for it.
        assert app_key_count(client) == 0


def app_key_count(client) -> int:
    return client.app.state.auth_service.count_active()


def test_bootstrap_is_idempotent_across_restarts(tmp_path):
    key = "trv_" + "c" * 40
    settings = _auth_settings(tmp_path, auth_bootstrap_key=key)
    for _ in range(3):
        with TestClient(create_app(settings)) as client:
            assert (
                client.get("/api/v1/trips", headers={"Authorization": f"Bearer {key}"}).status_code
                == 200
            )
            assert app_key_count(client) == 1, "bootstrap duplicated the key"


# -- service-level guard ---------------------------------------------------


def test_authenticate_raises_rather_than_returning_none():
    """A failed lookup must not be representable as 'no restriction'."""

    class _Uow:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    service = AuthService(lambda: _Uow(), enabled=True)
    with pytest.raises(AuthenticationError):
        service.authenticate(None)
    with pytest.raises(AuthenticationError):
        service.authenticate("   ")
    with pytest.raises(AuthenticationError):
        service.authenticate("wrong-prefix")
