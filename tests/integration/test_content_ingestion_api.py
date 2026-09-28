"""The ingestion API end to end: storage, owner isolation, and honest labelling.

Two properties carry the weight here.

**Owner isolation.** A saved note is a person's own material — where they ate, when they
were away, which page they were reading. On a deployment serving several people, one
must not be able to read another's, and the 404 that hides another owner's note must be
indistinguishable from the 404 for a note that does not exist.

**Honest labelling.** The platforms people paste from usually do not state a publication
date, so an ingested note normally has a known *fetch* time and an unknown *data* age.
Every assertion below that sounds pedantic about that distinction is guarding the one
failure this feature could ship with: presenting a 2019 note as current advice.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.domain.models import ObservationBasis
from travel_api.main import create_app

NOTE = (
    "杭州三日游攻略\n"
    "推荐去灵隐寺，建议游玩 2 小时，香火很旺。\n"
    "门票 ¥75，学生半价。\n"
    "周一闭馆，注意别白跑。\n"
    "楼外楼人均 150 元，有点贵但值得。\n"
)


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'ingest.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        # Nothing here should reach the network: ingestion is local text handling.
        free_providers_enabled=False,
        mock_provider_enabled=False,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _keys(app) -> tuple[str, str]:
    """Two owners' bearer keys, so isolation can be tested rather than assumed."""
    first, _ = app.state.auth_service.mint("owner-a", label="a")
    second, _ = app.state.auth_service.mint("owner-b", label="b")
    return first, second


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _ingest(client, key: str, **overrides):
    payload = {"text": NOTE, "url": "https://www.xiaohongshu.com/explore/xyz"}
    payload.update(overrides)
    return client.post("/api/v1/ingestions", json=payload, headers=_auth(key))


# -- extraction reaches the API -------------------------------------------


def test_a_pasted_note_is_read_and_stored(client, app):
    key, _ = _keys(app)
    response = _ingest(client, key)

    assert response.status_code == 201
    body = response.json()
    assert body["platform"] == "xiaohongshu"
    assert body["platform_label"] == "小红书"
    kinds = {fact["kind"] for fact in body["facts"]}
    assert {"price", "duration", "opening_hours", "place_mention"} <= kinds

    # Every fact must quote the text, because that is what makes it checkable rather
    # than merely asserted.
    for fact in body["facts"]:
        assert fact["evidence"] in NOTE
        assert fact["reason"], "a reading without a stated reason cannot be judged"


def test_the_extraction_is_returned_because_that_is_the_question(client, app):
    """A bare id would make the traveller guess whether it read the note correctly."""
    key, _ = _keys(app)
    body = _ingest(client, key).json()

    assert body["facts"]
    assert body["input_chars"] == len(NOTE)


def test_an_absent_publication_date_is_reported_rather_than_assumed(client, app):
    """The failure mode this feature would otherwise ship with.

    Most pages do not state a date. Defaulting to "now" would relabel a note from years
    ago as current advice about prices and opening hours.
    """
    key, _ = _keys(app)
    body = _ingest(client, key).json()

    assert body["published_at"] is None
    assert any("发布时间" in note for note in body["notes"])
    assert body["ingested_at"] is not None


def test_a_stated_publication_date_is_carried_through(client, app):
    key, _ = _keys(app)
    body = _ingest(client, key, published_at="2024-05-01T08:00:00").json()

    assert body["published_at"].startswith("2024-05-01T08:00:00")
    # With a date, the note is no longer reported as undated.
    assert not any("发布时间" in note for note in body["notes"])


def test_a_chinese_date_format_is_understood_rather_than_rejected(client, app):
    """Pages write dates in several ways; refusing one would block saving the note."""
    key, _ = _keys(app)
    response = _ingest(client, key, published_at="2024年05月01日")

    assert response.status_code == 201
    assert response.json()["published_at"].startswith("2024-05-01")


def test_an_unparseable_date_becomes_absent_not_now(client, app):
    """The direction of the fallback is the whole point.

    A failed parse must yield "unknown", never "just published" — otherwise a garbled
    date field would silently promote old advice to current.
    """
    key, _ = _keys(app)
    body = _ingest(client, key, published_at="昨天").json()

    assert body["published_at"] is None
    assert any("发布时间" in note for note in body["notes"])


def test_the_stored_reference_admits_the_data_age_is_unknown(client, app):
    """The reference is what the freshness layer reads, so it must not overstate."""
    key, _ = _keys(app)
    ingestion_id = _ingest(client, key).json()["id"]

    with app.state.database.session_factory() as session:
        from uuid import UUID

        from travel_api.db.uow import UnitOfWork

        with UnitOfWork(lambda: session) as uow:
            stored = uow.ingestions.get(UUID(ingestion_id), owner_id="owner-a")

    reference = stored.source_reference()
    assert reference.observed_at_basis is ObservationBasis.SOURCE_SILENT
    assert reference.observed_at_basis.age_is_about_the_data is False
    # No timer: re-reading the same paste would not make the advice newer.
    assert reference.expires_at is None
    assert reference.domain.value == "content_reference"
    assert reference.source_type.value == "user_provided"


def test_an_empty_body_is_rejected(client, app):
    """``text`` is the one required field: there is nothing to ingest without it."""
    key, _ = _keys(app)
    response = client.post("/api/v1/ingestions", json={"url": "https://x.com"}, headers=_auth(key))

    assert response.status_code == 422


# -- owner isolation -------------------------------------------------------


def test_a_note_is_readable_only_by_its_owner(client, app):
    alice, bob = _keys(app)
    ingestion_id = _ingest(client, alice).json()["id"]

    assert client.get(f"/api/v1/ingestions/{ingestion_id}", headers=_auth(alice)).status_code == 200
    # Bob must not see Alice's saved note. 404 rather than 403: confirming the id exists
    # under another owner would itself leak that fact.
    assert client.get(f"/api/v1/ingestions/{ingestion_id}", headers=_auth(bob)).status_code == 404


def test_a_listing_shows_only_the_callers_own_notes(client, app):
    alice, bob = _keys(app)
    _ingest(client, alice, url="https://www.xiaohongshu.com/explore/a")

    assert len(client.get("/api/v1/ingestions", headers=_auth(alice)).json()) == 1
    assert client.get("/api/v1/ingestions", headers=_auth(bob)).json() == []


def test_one_owner_cannot_delete_anothers_note(client, app):
    alice, bob = _keys(app)
    ingestion_id = _ingest(client, alice).json()["id"]

    assert (
        client.delete(f"/api/v1/ingestions/{ingestion_id}", headers=_auth(bob)).status_code == 404
    )
    # And it is genuinely still there for its owner, so the refusal was not a silent delete.
    assert client.get(f"/api/v1/ingestions/{ingestion_id}", headers=_auth(alice)).status_code == 200


def test_an_owner_can_delete_their_own_note(client, app):
    key, _ = _keys(app)
    ingestion_id = _ingest(client, key).json()["id"]

    assert (
        client.delete(f"/api/v1/ingestions/{ingestion_id}", headers=_auth(key)).status_code == 200
    )
    assert client.get(f"/api/v1/ingestions/{ingestion_id}", headers=_auth(key)).status_code == 404


def test_reading_requires_a_key(client, app):
    """Notes name where a person ate and when they were away, so they are not public."""
    _keys(app)
    assert client.get("/api/v1/ingestions").status_code == 401
    assert client.get("/api/v1/content/bookmarklet").status_code == 401


# -- persistence -----------------------------------------------------------


def test_a_stored_note_survives_a_reload_with_its_text(client, app):
    """The raw text is kept, so the extracted facts stay checkable after the fact.

    Without it a reader could see "人均 150 元" but not verify it, which is the
    difference between a citation and an assertion.
    """
    key, _ = _keys(app)
    ingestion_id = _ingest(client, key).json()["id"]

    with app.state.database.session_factory() as session:
        from uuid import UUID

        from travel_api.db.uow import UnitOfWork

        with UnitOfWork(lambda: session) as uow:
            stored = uow.ingestions.get(UUID(ingestion_id), owner_id="owner-a")

    assert stored.content_text == NOTE
    assert stored.facts
    for fact in stored.facts:
        assert fact.evidence in stored.content_text


def test_the_list_is_newest_first_and_bounded(client, app):
    """A note is usually re-used shortly after saving it, so recency orders the list."""
    key, _ = _keys(app)
    for index in range(3):
        _ingest(client, key, url=f"https://www.xiaohongshu.com/explore/{index}")

    listed = client.get("/api/v1/ingestions?limit=2", headers=_auth(key)).json()
    assert len(listed) == 2
    assert listed[0]["ingested_at"] >= listed[1]["ingested_at"]


# -- the bookmarklet -------------------------------------------------------


def test_the_bookmarklet_is_served_with_its_caveats(client, app):
    """The snippet is generated here, so the payload shape has one definition.

    A copy living in the frontend would drift from the endpoint it posts to, and the
    failure would look like a bookmarklet that mysteriously stopped working.
    """
    key, _ = _keys(app)
    body = client.get("/api/v1/content/bookmarklet", headers=_auth(key)).json()

    assert body["href"].startswith("javascript:")
    assert "/api/v1/ingestions" in body["href"]
    assert body["steps"] and body["notes"]


def test_the_bookmarklet_never_carries_or_asks_for_a_password(client, app):
    """It runs in the traveller's own logged-in browser, and must do only that.

    Reading cookies, local storage or a token would turn a convenience into a
    credential-harvesting tool, so the absence is asserted rather than assumed.
    """
    key, _ = _keys(app)
    href = client.get("/api/v1/content/bookmarklet", headers=_auth(key)).json()["href"]

    for forbidden in ("document.cookie", "localStorage", "sessionStorage", "password"):
        assert forbidden not in href
    # It reads the visible text of the page the traveller is already looking at.
    assert "innerText" in href


def test_the_bookmarklet_carries_the_deployments_own_origin(client, app):
    """So it posts back to the address the traveller is actually using."""
    key, _ = _keys(app)
    body = client.get("/api/v1/content/bookmarklet", headers=_auth(key)).json()

    assert body["href"].count(body["base_url"]) == 1
    assert "__BASE__" not in body["href"]


# -- the manifest tells the truth about this path --------------------------


def test_the_capability_matrix_does_not_credit_content_for_a_data_capability(client, app):
    """Ingested text is a citation, not a data source.

    If it were credited with, say, ``search_restaurants``, the matrix would report a
    capability as covered that nothing can actually serve from structured data.
    """
    key, _ = _keys(app)
    catalog = client.get("/api/v1/providers/catalog", headers=_auth(key)).json()

    entry = next(item for item in catalog["providers"] if item["id"] == "user-content")
    assert entry["capabilities"] == []
    assert entry["status"] == "available"
    # Nothing to configure, so no credential field may be advertised.
    assert entry["credentials"] == []
    assert entry["missing_credentials"] == []
    assert entry["caveats"], "the caveats are the honest part of this entry"


def test_content_ingestion_is_usable_with_no_credentials_at_all(client, app):
    """渐进增强: the traveller who configured nothing is who benefits most here."""
    key, _ = _keys(app)
    catalog = client.get("/api/v1/providers/catalog", headers=_auth(key)).json()
    entry = next(item for item in catalog["providers"] if item["id"] == "user-content")

    assert entry["active"] is True


def test_a_note_from_any_platform_is_attributed_to_that_platform(client, app):
    key, _ = _keys(app)
    for url, platform in (
        ("https://www.douyin.com/video/1", "douyin"),
        ("https://www.dianping.com/shop/1", "dianping"),
        ("https://you.ctrip.com/travels/1.html", "ctrip"),
        ("https://www.ly.com/1", "tongcheng"),
        ("https://example.com/post", "website"),
    ):
        body = _ingest(client, key, url=url).json()
        assert body["platform"] == platform, url


def test_the_ingestion_time_is_recorded_separately_from_the_data_time(client, app):
    """Both facts are reported, and they are different facts."""
    key, _ = _keys(app)
    body = _ingest(client, key).json()

    ingested = datetime.fromisoformat(body["ingested_at"].replace("Z", "+00:00"))
    assert ingested.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - ingested).total_seconds()) < 60
    # And the data's own time is absent, which the API must not paper over.
    assert body["published_at"] is None
