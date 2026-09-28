from fastapi.testclient import TestClient
from sqlalchemy import func, select
from travel_api.config import Settings
from travel_api.db.models import ProviderRequestRecord, ProviderResponseRecord
from travel_api.main import create_app


def test_create_reload_revision_and_change_history_are_persistent(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'phase2.db'}",
        auto_create_schema=True,
        provider_retry_count=0,
        # Keep this test hermetic: no outbound network calls.
        free_providers_enabled=False,
        mock_provider_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        body = {
            "destination": "成都",
            "start_date": "2026-10-03",
            "end_date": "2026-10-07",
            "travelers": 2,
            "total_budget": 5000,
            "hotel_budget": 1800,
            "pace": "relaxed",
        }
        created = client.post("/api/v1/trips", json=body)
        assert created.status_code == 200
        trip_id = created.json()["id"]
        with client.app.state.database.session_factory() as session:
            request_count = session.scalar(select(func.count()).select_from(ProviderRequestRecord))
            response_count = session.scalar(
                select(func.count()).select_from(ProviderResponseRecord)
            )
        assert request_count > 0
        assert response_count == request_count

        loaded = client.get(f"/api/v1/trips/{trip_id}")
        assert loaded.status_code == 200
        assert loaded.json()["current_version"] == 1

        changed = client.post(
            f"/api/v1/trips/{trip_id}/revision",
            json={
                "action": "change_budget",
                "constraints": {"total_budget": 5000, "hotel_budget": 1300},
            },
        )
        assert changed.status_code == 200
        assert changed.json()["trip"]["current_version"] == 2
        assert changed.json()["trip"]["budget"]["strategy"] == "strict"
        assert changed.json()["trip"]["selected_hotel_offer_id"] is not None

        refreshed = client.post(f"/api/v1/trips/{trip_id}/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["current_version"] == 3

        persisted = client.get(f"/api/v1/trips/{trip_id}").json()
        assert persisted["current_version"] == 3
        assert len(persisted["change_history"]) == 1
        snapshots = client.get(f"/api/v1/trips/{trip_id}/snapshots").json()
        assert snapshots
        assert any(len(item["history"]) >= 2 and item["diff"] for item in snapshots)
        assert len(client.get(f"/api/v1/trips/{trip_id}/changes").json()) == 1
