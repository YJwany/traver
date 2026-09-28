from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app


def _hermetic_app(tmp_path):
    """App wired to mock providers only, so tests never touch the network."""
    return create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite:///{tmp_path / 'api.db'}",
            auto_create_schema=True,
            provider_retry_count=0,
            free_providers_enabled=False,
            mock_provider_enabled=True,
        )
    )


def test_health_endpoint(tmp_path):
    with TestClient(_hermetic_app(tmp_path)) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_trip_preview_endpoint_runs_without_real_provider_keys(tmp_path):
    body = {
        "destination": "成都",
        "start_date": "2026-10-03",
        "end_date": "2026-10-07",
        "travelers": 2,
        "total_budget": 6000,
        "hotel_budget": 1800,
        "preferences": ["美食", "景点"],
        "pace": "relaxed",
        "hotel_preferences": ["地铁附近", "评分高"],
    }
    with TestClient(_hermetic_app(tmp_path)) as client:
        response = client.post("/api/v1/trips/preview", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["hotels"]) == 2
    assert payload["hotel_decisions"]
    assert payload["source_references"]
