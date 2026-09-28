"""How a traveller's own notes reach the plan — and what they are not allowed to do.

The product promise is "the more you supply, the more detailed the plan". The risk that
creates is specific: ingested text is somebody's account of a trip, and the easy way to
make the feature look impressive is to let it write directly into the plan's entities.
Then a blog post's remembered price becomes a hotel's price, and a stranger's "two hours
is enough" becomes the measured visit duration.

These tests pin the boundary. Notes are attached *alongside* entities, matched by name,
carrying their own provenance and their own age; the entities themselves are unchanged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app

NOTE_TEMPLATE = "推荐去{place}，建议游玩 3 小时。人均 88 元，周二人少。"


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'notes.db'}",
        auto_create_schema=True,
        auth_enabled=True,
        # The deterministic provider, so the entity names to match against are stable and
        # the test does not depend on network data.
        free_providers_enabled=False,
        mock_provider_enabled=True,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _key(app, owner: str = "owner-a") -> str:
    key, _ = app.state.auth_service.mint(owner, label=owner)
    return key


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _profile() -> dict:
    return {
        "destination": "成都",
        "start_date": "2026-10-03",
        "end_date": "2026-10-05",
        "travelers": 2,
        "total_budget": 5000,
    }


def _preview(client, key: str):
    response = client.post("/api/v1/trips/preview", json=_profile(), headers=_auth(key))
    assert response.status_code == 200, response.text
    return response.json()


def _entity_names(preview) -> list[str]:
    return [
        item["canonical_name"]
        for group in ("hotels", "restaurants", "attractions")
        for item in preview[group]
        if item.get("canonical_name")
    ]


def test_a_plan_with_no_saved_notes_is_unchanged(client, app):
    """渐进增强 in the negative direction: saving nothing must cost nothing.

    An empty list rather than an absent key, so "no notes" is a positive statement and a
    client cannot confuse it with a field that failed to serialise.
    """
    key = _key(app)
    preview = _preview(client, key)

    assert preview["ingested_notes"] == []


def test_a_saved_note_reaches_the_plan(client, app):
    """The point of the feature: supplying content makes the plan more detailed."""
    key = _key(app)
    preview = _preview(client, key)
    names = _entity_names(preview)
    assert names, "the fixture plan must contain entities, or this test proves nothing"

    client.post(
        "/api/v1/ingestions",
        json={
            "text": NOTE_TEMPLATE.format(place=names[0]),
            "url": "https://www.dianping.com/shop/1",
        },
        headers=_auth(key),
    )

    with_notes = _preview(client, key)
    assert with_notes["ingested_notes"], "a saved note must reach the next plan"
    kinds = {note["kind"] for note in with_notes["ingested_notes"]}
    assert {"duration", "price"} <= kinds
    # The note is attached to the place it is about, which is the whole point of
    # matching — a hotel name ends in 酒店, so it is found from the sentence rather
    # than from a list of place suffixes.
    assert any(note["matched"] for note in with_notes["ingested_notes"])


def test_a_landmark_mention_is_still_listed_as_a_mention(client, app):
    """Landmark names — the ones a suffix can actually identify — keep their own kind.

    Venue suffixes are deliberately not in the extractor's list: adding 酒店 would make
    「这家酒店很贵」 yield a place called 这家酒店. Venues are reached by matching the
    sentence against names we already have, which needs no suffix guessing at all.
    """
    key = _key(app)
    client.post("/api/v1/ingestions", json={"text": "推荐去人民公园，很值得。"}, headers=_auth(key))

    mentions = [
        note for note in _preview(client, key)["ingested_notes"] if note["kind"] == "place_mention"
    ]
    assert [note["text"] for note in mentions] == ["人民公园"]


def test_a_mention_of_a_place_we_have_is_matched_to_it(client, app):
    """So the note lands next to the place it is about.

    Otherwise the traveller has to reconcile two lists by eye, which is the work the
    feature was supposed to remove.
    """
    key = _key(app)
    target = _entity_names(_preview(client, key))[0]
    client.post(
        "/api/v1/ingestions",
        json={"text": NOTE_TEMPLATE.format(place=target)},
        headers=_auth(key),
    )

    notes = _preview(client, key)["ingested_notes"]
    matched = [note for note in notes if note["matched"]]
    assert matched, f"nothing matched {target!r}"
    assert matched[0]["matched_entity_name"] == target
    assert matched[0]["matched_entity_id"]
    assert matched[0]["matched_entity_kind"] in {"hotel", "restaurant", "attraction"}


def test_a_mention_we_cannot_place_is_reported_rather_than_dropped(client, app):
    """An unmatched mention is a gap in our data, not a failure to hide.

    The place has no coordinates, so it cannot be routed — but saying so is more useful
    than omitting it, and it tells the traveller which source would fill the hole.
    """
    key = _key(app)
    client.post(
        "/api/v1/ingestions",
        json={"text": "推荐去一个不存在的地名博物馆，很值得。"},
        headers=_auth(key),
    )

    notes = _preview(client, key)["ingested_notes"]
    mentions = [note for note in notes if note["kind"] == "place_mention"]
    assert mentions, "a mention must be reported even when nothing matches it"
    assert all(note["matched"] is False for note in mentions)
    assert all(note["matched_entity_id"] is None for note in mentions)


def test_a_note_never_overwrites_a_measured_entity_field(client, app):
    """The rule that keeps an opinion from becoming a fact.

    The note asserts a price and a duration. Neither may appear on the entity: the price
    is somebody's memory, the duration is somebody's preference, and the entity's own
    values come from sources that measured them.

    Compared field by field rather than whole, because an entity carries a
    ``data_age_seconds`` computed from the current time — that value legitimately differs
    between two calls, and asserting on the whole object would test the clock instead of
    the rule.
    """
    key = _key(app)
    before = _preview(client, key)
    target = _entity_names(before)[0]

    def field_values(preview, name):
        entity = next(
            item
            for group in ("hotels", "restaurants", "attractions")
            for item in preview[group]
            if item.get("canonical_name") == name
        )
        return {
            field: entity.get(field)
            for field in (
                "id",
                "canonical_name",
                "rating",
                "average_cost",
                "ticket_price",
                "recommended_duration_minutes",
                "location",
                "cuisine",
                "opening_hours",
            )
        }

    client.post(
        "/api/v1/ingestions",
        json={"text": f"推荐去{target}，建议游玩 3 小时。人均 88 元。"},
        headers=_auth(key),
    )
    after = _preview(client, key)

    assert field_values(after, target) == field_values(before, target), (
        "ingested text must never be merged into an entity's own measured fields"
    )
    # And the note is still visible, just not merged in.
    assert any(note["matched_entity_name"] == target for note in after["ingested_notes"])


def test_the_note_is_cited_as_one_of_the_plans_sources(client, app):
    """An ingested note is a source like any other, and gets a reference.

    Without one its origin cannot be checked, which is the difference between a citation
    and an assertion.
    """
    key = _key(app)
    client.post(
        "/api/v1/ingestions",
        json={
            "text": "推荐去人民公园，建议游玩 2 小时。",
            "url": "https://www.xiaohongshu.com/explore/z",
        },
        headers=_auth(key),
    )

    references = _preview(client, key)["source_references"]
    content = [item for item in references if item["source"].startswith("user-content:")]
    assert content
    assert content[0]["source_type"] == "user_provided"
    assert content[0]["domain"] == "content_reference"
    assert content[0]["source_id"] == "https://www.xiaohongshu.com/explore/z"
    assert content[0]["observed_at_basis"] == "source_silent"


def test_an_undated_note_is_never_presented_as_current_advice(client, app):
    """The honesty rule, applied to the traveller's own material.

    A page that does not state a publication date leaves the advice's age genuinely
    unknown. Every note carries that fact explicitly, so the UI can say "data time
    unknown" instead of implying recency — and it stays true however old the note is.
    """
    key = _key(app)
    client.post("/api/v1/ingestions", json={"text": "推荐去人民公园。"}, headers=_auth(key))

    notes = _preview(client, key)["ingested_notes"]
    assert notes
    assert all(note["data_age_is_known"] is False for note in notes)
    assert all(note["published_at"] is None for note in notes)
    # The fetch time is still reported, because it is a real fact — just a different one.
    assert all(note["ingested_at"] for note in notes)


def test_a_dated_note_reports_how_old_its_advice_is(client, app):
    key = _key(app)
    client.post(
        "/api/v1/ingestions",
        json={"text": "推荐去人民公园。", "published_at": "2019-04-01T10:00:00"},
        headers=_auth(key),
    )

    notes = _preview(client, key)["ingested_notes"]
    assert notes
    assert all(note["data_age_is_known"] is True for note in notes)
    assert notes[0]["published_at"].startswith("2019-04-01")


def test_a_caution_is_listed_before_the_tips(client, app):
    """A closed-day note changes what can be planned; a tip does not.

    Ordering them the same way would bury the one fact that saves a wasted morning.
    """
    key = _key(app)
    client.post(
        "/api/v1/ingestions",
        json={"text": "建议早点去。周一闭馆，注意别白跑。"},
        headers=_auth(key),
    )

    notes = _preview(client, key)["ingested_notes"]
    assert notes
    assert notes[0]["kind"] == "opening_hours"


def test_notes_are_not_shared_between_owners(client, app):
    """A note names where its author ate and when they were away.

    An unconfigured owner must not inherit another's saved content, for the same reason
    they must not inherit a key or a quota.
    """
    alice, bob = _key(app, "owner-a"), _key(app, "owner-b")
    client.post(
        "/api/v1/ingestions",
        json={"text": "推荐去人民公园，建议游玩 2 小时。"},
        headers=_auth(alice),
    )

    assert _preview(client, alice)["ingested_notes"]
    assert _preview(client, bob)["ingested_notes"] == []


def test_every_note_quotes_the_sentence_it_came_from(client, app):
    """So a reader can disagree with a reading instead of having to trust it."""
    key = _key(app)
    text = "推荐去人民公园，建议游玩 2 小时。人均 88 元。"
    client.post("/api/v1/ingestions", json={"text": text}, headers=_auth(key))

    for note in _preview(client, key)["ingested_notes"]:
        assert note["evidence"] in text
        assert note["reason"], "a reading without its reason cannot be judged"
        assert note["confidence"] in {"high", "medium", "low"}


def test_a_deleted_note_stops_appearing_in_the_plan(client, app):
    """The plan reads the notes fresh, so deleting one takes effect immediately.

    A cached copy would leave advice in the plan that its author had removed.
    """
    key = _key(app)
    created = client.post(
        "/api/v1/ingestions", json={"text": "推荐去人民公园。"}, headers=_auth(key)
    ).json()
    assert _preview(client, key)["ingested_notes"]

    client.delete(f"/api/v1/ingestions/{created['id']}", headers=_auth(key))
    assert _preview(client, key)["ingested_notes"] == []


def test_the_default_local_mode_attributes_a_request_to_its_configured_owner(tmp_path):
    """The mode every fresh clone runs in, and the one where this feature was broken.

    ``AUTH_ENABLED=false`` does not mean "nobody owns this". It means the principal is the
    configured ``AUTH_ANONYMOUS_OWNER`` (``local-dev``, stated in ``.env.example``), and the
    middleware already binds exactly that as the acting owner for the request. Only the
    write routes disagreed, storing notes and trips with no owner at all — so a note saved
    with no API key was written under one answer to "whose is this" and looked up under
    another, and never reached a plan.

    Kept as its own test rather than folded into the authenticated one above, because that
    test passing was the reason the defect stayed hidden: with a key present both answers
    are the same string, so only the unauthenticated path can see the disagreement.
    """
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'local-mode.db'}",
        auto_create_schema=True,
        auth_enabled=False,
        free_providers_enabled=False,
        mock_provider_enabled=True,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    with TestClient(create_app(settings)) as client:
        # No Authorization header anywhere in this test: that is the whole point.
        saved = client.post(
            "/api/v1/ingestions", json={"text": NOTE_TEMPLATE.format(place="人民公园")}
        )
        assert saved.status_code == 201, saved.text

        preview = client.post("/api/v1/trips/preview", json=_profile())
        assert preview.status_code == 200, preview.text

    assert preview.json()["ingested_notes"], (
        "a note saved in the default local mode must reach the next plan"
    )


def test_the_default_local_mode_still_keeps_a_trip_reachable(tmp_path):
    """The other half of the same rule: the owner used to write must be the one used to read.

    Trips were consistent with themselves before the fix (both sides unfiltered) and must
    stay consistent after it, or fixing the notes would have cost the trips.
    """
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'local-trips.db'}",
        auto_create_schema=True,
        auth_enabled=False,
        free_providers_enabled=False,
        mock_provider_enabled=True,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/v1/trips", json=_profile())
        assert created.status_code == 200, created.text
        trip_id = created.json()["id"]

        assert client.get(f"/api/v1/trips/{trip_id}").status_code == 200
        assert trip_id in {trip["id"] for trip in client.get("/api/v1/trips").json()}
        # And the export, which reads the same rows and was the first place this class of
        # defect showed up.
        assert client.get(f"/api/v1/trips/{trip_id}/export.md").status_code == 200
