from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app


def _settings(tmp_path):
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'phase3-ai.db'}",
        auto_create_schema=True,
        provider_retry_count=0,
        # Keep this test hermetic: no outbound network calls.
        free_providers_enabled=False,
        mock_provider_enabled=True,
    )


def test_ai_chat_creates_persists_and_revises_trip(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post(
            "/api/v1/ai/chat",
            json={
                "message": (
                    "去成都玩四天，两个人，预算5000，酒店预算1800，喜欢美食，"
                    "不想太赶，2026年10月3日到2026年10月7日"
                )
            },
        )
        assert created.status_code == 200, created.text
        first = created.json()
        trip_id = first["trip_id"]
        assert first["trip_updated"] is True
        assert first["version"] == 1
        assert {call["name"] for call in first["tool_calls"]} >= {
            "search_hotels",
            "search_restaurants",
            "search_attractions",
        }

        conversation = client.get(f"/api/v1/trips/{trip_id}/conversation")
        assert conversation.status_code == 200
        assert [item["role"] for item in conversation.json()] == [
            "user",
            "tool",
            "tool",
            "tool",
            "tool",
            "tool",
            "assistant",
        ]

        revised = client.post(
            "/api/v1/ai/chat",
            json={"trip_id": trip_id, "message": "酒店太贵了，换一个便宜但离地铁近的"},
        )
        assert revised.status_code == 200, revised.text
        second = revised.json()
        assert second["trip_updated"] is True
        assert second["version"] == 2
        assert second["change"]["action"] == "replace_hotel"

        loaded = client.get(f"/api/v1/trips/{trip_id}").json()
        assert loaded["current_version"] == 2
        assert len(loaded["change_history"]) == 1


def test_ai_chat_requests_missing_date_without_creating_trip(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/api/v1/ai/chat",
            json={"message": "去成都玩四天，两个人，预算5000，喜欢美食"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["trip_id"] is None
    assert payload["trip_updated"] is False
    assert payload["unresolved_questions"]
    assert "日期" in payload["message"]


def test_trip_list_endpoint_returns_persisted_trips(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/api/v1/trips",
            json={
                "destination": "成都",
                "start_date": "2026-10-03",
                "end_date": "2026-10-07",
                "travelers": 2,
                "total_budget": 5000,
            },
        )
        assert response.status_code == 200

        trips = client.get("/api/v1/trips")

    assert trips.status_code == 200
    assert len(trips.json()) == 1
