"""The export endpoints, over real HTTP, because that is where the first bug lived.

Running the export service directly was not enough. ``Response(headers=...)`` encodes header
values as latin-1, and the download filename is built from the destination — so every Chinese
destination, which is to say essentially every trip this project is for, returned a 500. The
pure-function tests all passed, because the failure was in the transport layer between them and
the client. These tests exercise the wire.

The other subject here is owner isolation, which the export endpoints have to respect like
every other trip route: a plan is somebody's, and 404 rather than 403 avoids confirming that an
id exists.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app


def _settings(tmp_path, name: str = "export.db") -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / name}",
        auto_create_schema=True,
        auth_enabled=False,
        credential_key_file=str(tmp_path / "credential.key"),
        provider_retry_count=0,
        free_providers_enabled=False,
        mock_provider_enabled=True,
    )


def _create(client: TestClient, destination: str = "杭州") -> dict:
    created = client.post(
        "/api/v1/trips",
        json={
            "destination": destination,
            "start_date": "2026-10-03",
            "end_date": "2026-10-05",
            "travelers": 2,
            "total_budget": 5000,
        },
    )
    assert created.status_code == 200, created.text
    return created.json()


def test_a_chinese_destination_exports_instead_of_returning_a_server_error(tmp_path):
    """The bug that pure-function tests could not see.

    A latin-1 header cannot carry 杭州. Sanitising with ``str.isalnum()`` does not help, because
    a Chinese character *is* alphanumeric — so the destination reached the header intact and the
    response raised ``UnicodeEncodeError``. Both formats must survive a real response.
    """
    with TestClient(create_app(_settings(tmp_path))) as client:
        trip = _create(client, destination="杭州")

        markdown = client.get(f"/api/v1/trips/{trip['id']}/export.md")
        assert markdown.status_code == 200, markdown.text
        assert markdown.text.startswith("# 杭州")
        assert "text/markdown" in markdown.headers["content-type"]

        calendar = client.get(f"/api/v1/trips/{trip['id']}/export.ics")
        assert calendar.status_code == 200, calendar.text
        assert calendar.text.startswith("BEGIN:VCALENDAR")
        assert "text/calendar" in calendar.headers["content-type"]


def test_the_download_is_named_after_the_trip_and_never_cached(tmp_path):
    """A file called ``<uuid>.md`` is unopenable later, and a snapshot must not be cached."""
    with TestClient(create_app(_settings(tmp_path, "headers.db"))) as client:
        trip = _create(client, destination="杭州")
        response = client.get(f"/api/v1/trips/{trip['id']}/export.md")

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert ".md" in disposition
    # Multi-byte characters are percent-encoded per RFC 5987, and the fallback stays ASCII.
    assert "filename*=UTF-8''%E6%9D%AD%E5%B7%9E" in disposition
    assert response.headers["cache-control"] == "no-store"


def test_the_exported_document_states_its_own_snapshot_status(tmp_path):
    """The wire-level document must carry the disclosures the unit tests look for."""
    with TestClient(create_app(_settings(tmp_path, "contents.db"))) as client:
        trip = _create(client, destination="杭州")
        body = client.get(f"/api/v1/trips/{trip['id']}/export.md").text

    assert "不会自动更新" in body
    assert "抓取时间" in body
    # The fixture stamps no observation basis, so at least one reference is unverifiable, and
    # the document must say so rather than reading as fully verified.
    assert "无法确认数据年龄" in body
    assert "全部标明了数据自身的时间" not in body


def test_a_travellers_own_note_reaches_the_downloaded_file(tmp_path):
    """The appendix over the wire, because that is where the notes could be lost.

    ``ingested_notes`` is an untyped list of dicts on the preview and the export reads it by key,
    so only an end-to-end run proves the keys that reach the database are the keys the document
    looks for. A rename on either side produces an appendix that is silently empty — a document
    that looks complete while having lost the one part that cannot be fetched again.

    The note is saved *before* the trip is created, because ``create`` builds the preview, and a
    preview reads the notes that exist when it runs.
    """
    with TestClient(create_app(_settings(tmp_path, "notes.db"))) as client:
        saved = client.post(
            "/api/v1/ingestions",
            json={
                "text": "楼外楼人均 80 元左右，不过周一闭馆，建议早点去。",
                "url": "https://example.invalid/note/1",
                "title": "杭州三天两夜",
                "published_at": "2024-05-01",
            },
        )
        assert saved.status_code in (200, 201), saved.text

        trip = _create(client)
        body = client.get(f"/api/v1/trips/{trip['id']}/export.md").text

    # The failure being guarded against is an empty appendix, so assert on content rather than on
    # the heading alone: a heading with nothing under it is exactly what a renamed key produces.
    assert "你提供的内容" in body
    assert "楼外楼人均 80 元左右" in body, "the quoted span did not survive to the document"
    assert "2024-05-01" in body, "the page's own publication date is missing"
    assert "某个人说的" in body
    assert "https://example.invalid/note/1" in body


def test_the_preview_carries_every_key_the_export_reads(tmp_path):
    """The key contract, asserted on its own so a failure says which side broke.

    The document reading ``matched_entity_name`` while the preview sends ``matched_name`` is
    invisible in both directions: the preview is valid JSON and the document is valid Markdown.
    Naming each key turns that into one failing assertion that says which one.
    """
    with TestClient(create_app(_settings(tmp_path, "keys.db"))) as client:
        saved = client.post(
            "/api/v1/ingestions",
            json={
                "text": "楼外楼人均 80 元左右，不过周一闭馆，建议早点去。",
                "url": "https://example.invalid/note/1",
                "title": "杭州三天两夜",
                "published_at": "2024-05-01",
            },
        )
        assert saved.status_code in (200, 201), saved.text
        preview = client.post(
            "/api/v1/trips/preview",
            json={
                "destination": "杭州",
                "start_date": "2026-10-03",
                "end_date": "2026-10-05",
                "travelers": 2,
                "total_budget": 5000,
            },
        ).json()

    notes = preview["ingested_notes"]
    assert notes, "the preview reported no extracted facts at all"
    for key in (
        "ingestion_id",
        "platform",
        "url",
        "title",
        "author",
        "kind",
        "text",
        "evidence",
        "confidence",
        "reason",
        "published_at",
        "ingested_at",
        "matched",
        "matched_entity_name",
    ):
        assert key in notes[0], f"the export reads {key!r} and the preview does not send it"


def test_an_unknown_trip_is_not_found_in_both_formats(tmp_path):
    unknown = "00000000-0000-0000-0000-000000000000"
    with TestClient(create_app(_settings(tmp_path, "missing.db"))) as client:
        assert client.get(f"/api/v1/trips/{unknown}/export.md").status_code == 404
        assert client.get(f"/api/v1/trips/{unknown}/export.ics").status_code == 404


def test_another_owners_plan_cannot_be_exported(tmp_path):
    """404, not 403: the response must not confirm that the id exists.

    This is the export path, so the leak would be the entire plan — hotels, times, notes —
    rather than a status code.
    """
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'owners.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        credential_key_file=str(tmp_path / "credential.key"),
        provider_retry_count=0,
        free_providers_enabled=False,
        mock_provider_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        # Minted through the service rather than the HTTP route, following the established
        # pattern for multi-owner tests: the route's own shape is not what is under test here.
        alice = {"Authorization": f"Bearer {app.state.auth_service.mint('alice')[0]}"}
        bob = {"Authorization": f"Bearer {app.state.auth_service.mint('bob')[0]}"}

        created = client.post(
            "/api/v1/trips",
            headers=alice,
            json={
                "destination": "杭州",
                "start_date": "2026-10-03",
                "end_date": "2026-10-05",
                "travelers": 2,
            },
        )
        assert created.status_code == 200, created.text
        trip_id = created.json()["id"]

        # Alice can export her own plan, in both formats.
        assert client.get(f"/api/v1/trips/{trip_id}/export.md", headers=alice).status_code == 200
        assert client.get(f"/api/v1/trips/{trip_id}/export.ics", headers=alice).status_code == 200

        # Bob gets the same answer as for an id that was never issued.
        for suffix in ("export.md", "export.ics"):
            response = client.get(f"/api/v1/trips/{trip_id}/{suffix}", headers=bob)
            assert response.status_code == 404, response.text
            assert "杭州" not in response.text
