"""The write gate must hold at the HTTP layer, not just in the registry.

This file exists because of a real bypass: ``POST /api/v1/tools/{name}`` passed
``allow_write=True`` unconditionally, so the registry's ``PermissionError`` guard
could never fire over HTTP. The only pre-existing test
(``test_ai_tools.py::test_write_tool_requires_explicit_permission``) exercised the
registry directly, which is exactly the layer that was *not* broken — so the
bypass survived. These tests drive the endpoint itself.

A second bypass lived on the same route and is pinned at the bottom of the file: the route
had **no authentication dependency at all**. ``confirm_write`` is a confirmation, not a
permission, so an anonymous caller could pass ``?confirm_write=true`` and the registry's
write guard would fire on ``allow_write=True`` — every gate satisfied and no credential
required. Worse, the tool handlers take a trip id and no owner (their signatures are the
published OpenAI tool contract), so they looked the trip up with owner ``None``, which in
the repository means "no filter": any owner's trip could be read by id, and mutated.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app
from travel_api.services.auth import SCOPE_READ
from travel_api.services.tool_registry import ToolDefinition, ToolRegistry


def _hermetic_app(tmp_path, *, auth_enabled: bool = False):
    return create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite:///{tmp_path / 'tools.db'}",
            auto_create_schema=True,
            auth_enabled=auth_enabled,
            provider_retry_count=0,
            free_providers_enabled=False,
            mock_provider_enabled=True,
        )
    )


@pytest.fixture
def trip_body():
    return {
        "destination": "成都",
        "start_date": "2026-10-03",
        "end_date": "2026-10-07",
        "travelers": 2,
        "total_budget": 6000,
        "pace": "relaxed",
    }


# -- definition consistency -------------------------------------------------


def test_write_permission_and_kind_must_agree():
    """A mismatch silently disables the gate, so registration must reject it.

    ``refresh_prices`` declared ``permission="trip:write"`` while leaving ``kind``
    at its ``"read"`` default. Only ``kind`` was ever checked, so the tool was
    callable without confirmation by anyone.
    """

    def handler(payload):  # pragma: no cover - never invoked
        return {}

    class _Input:
        @staticmethod
        def model_validate(_payload):
            return None

    with pytest.raises(ValueError, match="inconsistent"):
        ToolDefinition(
            name="sneaky",
            description="write permission but read kind",
            input_model=_Input,
            output_model=None,
            permission="trip:write",
            handler=handler,
            kind="read",
        )


def test_default_registry_has_no_inconsistent_tool():
    """Every shipped write tool must actually be classified as a write."""
    from travel_api.services.tool_registry import build_default_tool_registry

    class _Service:
        pass

    built = build_default_tool_registry(_Service())
    definitions = built.definitions()
    assert any(item["permission"].endswith(":write") for item in definitions), (
        "expected at least one write tool"
    )
    for item in definitions:
        expected_kind = "write" if item["permission"].endswith(":write") else "read"
        assert built.get(item["name"]).kind == expected_kind, (
            f"{item['name']} is {item['permission']} but classified as "
            f"{built.get(item['name']).kind}"
        )


# -- HTTP-layer enforcement -------------------------------------------------


def test_write_tool_over_http_requires_explicit_confirmation(tmp_path, trip_body):
    """The exact bypass: a write tool must not run on a bare POST."""
    with TestClient(_hermetic_app(tmp_path)) as client:
        created = client.post("/api/v1/trips", json=trip_body)
        assert created.status_code == 200, created.text
        trip_id = created.json()["id"]

        # No confirmation -> refused, and the trip must be untouched.
        refused = client.post(
            "/api/v1/tools/revise_trip",
            json={"trip_id": trip_id, "action": "replace_hotel", "constraints": {}},
        )
        assert refused.status_code == 403, refused.text
        assert "confirm_write" in refused.json()["detail"]

        # Explicit confirmation -> allowed through to the handler.
        confirmed = client.post(
            "/api/v1/tools/revise_trip?confirm_write=true",
            json={"trip_id": trip_id, "action": "replace_hotel", "constraints": {}},
        )
        assert confirmed.status_code in (200, 422), confirmed.text
        assert confirmed.status_code != 403


def test_replace_hotel_over_http_requires_explicit_confirmation(tmp_path, trip_body):
    with TestClient(_hermetic_app(tmp_path)) as client:
        created = client.post("/api/v1/trips", json=trip_body)
        trip_id = created.json()["id"]

        refused = client.post(
            "/api/v1/tools/replace_hotel",
            json={"trip_id": trip_id, "constraints": {}},
        )
        assert refused.status_code == 403, refused.text


def test_refresh_prices_is_gated_too(tmp_path, trip_body):
    """This tool was the one missing ``kind="write"``, so it was ungated."""
    with TestClient(_hermetic_app(tmp_path)) as client:
        created = client.post("/api/v1/trips", json=trip_body)
        trip_id = created.json()["id"]

        refused = client.post("/api/v1/tools/refresh_prices", json={"trip_id": trip_id})
        assert refused.status_code == 403, (
            "refresh_prices declared trip:write but was classified as read, so it "
            f"used to be callable with no confirmation: {refused.text}"
        )


def test_read_tools_need_no_confirmation(tmp_path, trip_body):
    with TestClient(_hermetic_app(tmp_path)) as client:
        response = client.post(
            "/api/v1/tools/search_hotels",
            json=trip_body,
        )
    assert response.status_code == 200, response.text


def test_unknown_tool_is_404_not_422(tmp_path):
    with TestClient(_hermetic_app(tmp_path)) as client:
        response = client.post("/api/v1/tools/does_not_exist", json={})
    assert response.status_code == 404


def test_tool_listing_exposes_permissions(tmp_path):
    with TestClient(_hermetic_app(tmp_path)) as client:
        response = client.get("/api/v1/tools")
    assert response.status_code == 200
    tools = {item["name"]: item for item in response.json()}
    assert tools["revise_trip"]["permission"] == "trip:write"
    assert tools["search_hotels"]["permission"] == "trip:read"


# -- the route is authenticated, and scoped to the caller --------------------


def _auth(app, owner: str, *, scopes: tuple[str, ...] = ()) -> dict[str, str]:
    key, _ = app.state.auth_service.mint(owner, label=owner, scopes=scopes)
    return {"Authorization": f"Bearer {key}"}


def test_an_anonymous_caller_cannot_read_another_owners_trip_through_a_tool(tmp_path, trip_body):
    """The route had no auth dependency, so the tools' owner-less lookups ran unfiltered.

    ``GET /trips/{id}`` correctly returned 401 for the same request, which is what made the
    hole easy to miss: the protected door was locked and this one was not.
    """
    app = _hermetic_app(tmp_path, auth_enabled=True)
    with TestClient(app) as client:
        alice = _auth(app, "alice")
        created = client.post("/api/v1/trips", json=trip_body, headers=alice)
        assert created.status_code == 200, created.text
        trip_id = created.json()["id"]

        # Alice can read her own budget through the tool: authentication must not cost the
        # owner the feature.
        hers = client.post("/api/v1/tools/get_budget", json={"trip_id": trip_id}, headers=alice)
        assert hers.status_code == 200, hers.text

        # A stranger with no credential gets the same answer as the protected route.
        anonymous = client.post("/api/v1/tools/get_budget", json={"trip_id": trip_id})
        assert anonymous.status_code == 401, anonymous.text
        assert trip_id not in anonymous.text


def test_an_anonymous_caller_cannot_mutate_another_owners_trip_through_a_tool(tmp_path, trip_body):
    """``confirm_write`` is a confirmation, not a permission — the exact confusion here."""
    app = _hermetic_app(tmp_path, auth_enabled=True)
    with TestClient(app) as client:
        alice = _auth(app, "alice")
        trip_id = client.post("/api/v1/trips", json=trip_body, headers=alice).json()["id"]
        before = client.get(f"/api/v1/trips/{trip_id}", headers=alice).json()

        anonymous = client.post(
            "/api/v1/tools/revise_trip?confirm_write=true",
            json={"trip_id": trip_id, "action": "replace_hotel", "constraints": {}},
        )
        assert anonymous.status_code == 401, anonymous.text

        after = client.get(f"/api/v1/trips/{trip_id}", headers=alice).json()
        assert after["current_version"] == before["current_version"], (
            "an unauthenticated request changed a stored trip"
        )


def test_another_owners_trip_is_a_404_through_a_tool_not_a_500(tmp_path, trip_body):
    """The handler raises a ``LookupError``, which the route did not map.

    Left unmapped it becomes a 500 — and a 500 is a worse answer than a 404: it says an id
    exists and something went wrong with it, which is the existence disclosure the 404/403
    rule exists to avoid.
    """
    app = _hermetic_app(tmp_path, auth_enabled=True)
    with TestClient(app) as client:
        alice = _auth(app, "alice")
        bob = _auth(app, "bob")
        trip_id = client.post("/api/v1/trips", json=trip_body, headers=alice).json()["id"]

        for suffix in ("", "?confirm_write=true"):
            response = client.post(
                f"/api/v1/tools/get_budget{suffix}",
                json={"trip_id": trip_id},
                headers=bob,
            )
            assert response.status_code == 404, response.text
            assert "成都" not in response.text

        # An id that was never issued answers identically.
        unknown = client.post(
            "/api/v1/tools/get_budget",
            json={"trip_id": "00000000-0000-0000-0000-000000000000"},
            headers=bob,
        )
        assert unknown.status_code == 404, unknown.text


def test_a_read_only_key_cannot_reach_a_write_tool_by_confirming_it(tmp_path, trip_body):
    """The scope comes from the tool's own ``kind``, not from a fixed route dependency.

    A single fixed ``require_read`` on the route would let a read-only key write; a fixed
    ``require_write`` would stop read-only keys from reading. The registry already knows
    which is which, so the route asks it.
    """
    app = _hermetic_app(tmp_path, auth_enabled=True)
    with TestClient(app) as client:
        owner = _auth(app, "alice")
        reader = _auth(app, "alice", scopes=(SCOPE_READ,))
        trip_id = client.post("/api/v1/trips", json=trip_body, headers=owner).json()["id"]

        # Reads are allowed with the read scope.
        assert (
            client.post(
                "/api/v1/tools/get_budget", json={"trip_id": trip_id}, headers=reader
            ).status_code
            == 200
        )

        refused = client.post(
            "/api/v1/tools/revise_trip?confirm_write=true",
            json={"trip_id": trip_id, "action": "replace_hotel", "constraints": {}},
            headers=reader,
        )
        assert refused.status_code == 403, refused.text
        assert "trip:write" in refused.json()["detail"]

        # And the refusal happened before the handler ran.
        assert client.get(f"/api/v1/trips/{trip_id}", headers=owner).json()["current_version"] == 1


# -- registry-level guard still holds ---------------------------------------


def test_registry_still_refuses_write_without_allow_write():
    from pydantic import BaseModel

    class Input(BaseModel):
        value: str

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="mutate",
            description="mutate state",
            input_model=Input,
            output_model=None,
            permission="trip:write",
            handler=lambda payload: {"value": payload.value},
            kind="write",
        )
    )

    import asyncio

    with pytest.raises(PermissionError):
        asyncio.run(registry.call("mutate", {"value": "x"}))
