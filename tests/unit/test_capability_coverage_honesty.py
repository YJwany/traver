"""A capability that returns nothing must not be reported as covered.

Implementing a protocol method and serving a capability look identical to `isinstance`. That
gap produced a concrete lie: the capability matrix reported 房型与报价 as fully covered by
OpenStreetMap, in a deployment where **no** source can produce a room rate, because Overpass
implements `search_room_offers` as a method whose whole body returns an empty list. The
settings page therefore advertised a capability that returns nothing, while the provider's
own manifest caveat said it had no rates.

This matters more than a display bug. The product's premise is that the tool aggregates
whatever you plug in and is honest about what it has — "提供的少也会有攻略只不过要差一点".
A catalogue that credits coverage it does not have makes the one screen a user consults to
decide "is this set up" actively misleading, in the direction of overclaiming.

These tests pin the rule at every layer, because each layer alone can be wrong:

  * the provider declares what it cannot serve;
  * the registry distinguishes "can be called" from "will answer";
  * the manifest does not declare what the provider cannot serve;
  * the catalogue names the gap and says what would fill it.
"""

from __future__ import annotations

from travel_api.providers.base import (
    CAPABILITY_BY_METHOD,
    UNSERVED_CAPABILITIES,
    unserved_capabilities_of,
)
from travel_api.providers.free import build_free_providers
from travel_api.providers.manifest import (
    CAPABILITY_GAP_NOTES,
    CAPABILITY_ORDER,
    PROVIDER_CATALOG,
    capability_gaps,
    capability_matrix,
)
from travel_api.providers.registry import ProviderRegistry


def _free_providers() -> list:
    """The keyless providers, as the app builds them.

    ``build_free_providers`` returns ``(providers, http_client)`` — the client is shared so
    the polite-rate-limit state is not duplicated per source.
    """
    providers, _client = build_free_providers(user_agent="traver-tests", language="zh")
    return list(providers)


def _free_registry() -> ProviderRegistry:
    return ProviderRegistry(_free_providers())


def _all_capabilities() -> dict[str, set[str]]:
    """What the free providers report as serving, for the matrix."""
    registry = _free_registry()
    return {
        capability: {
            getattr(item, "name", "") for item in registry.serving_providers_for(capability)
        }
        for capability in CAPABILITY_ORDER
    }


# -- the provider's own declaration ---------------------------------------------


def test_the_overpass_provider_declares_the_capability_it_cannot_serve():
    """A source has to be able to say "I cannot answer that".

    Without an explicit declaration there is nothing to distinguish a stub from an
    implementation, and no amount of care at the other layers can recover the fact.
    """
    overpass = next(
        provider
        for provider in _free_providers()
        if "search_room_offers" in unserved_capabilities_of(provider)
    )
    assert "search_room_offers" in overpass.UNSERVED_CAPABILITIES
    # It must still implement the method: one provider serves hotels, restaurants and
    # attractions from a single dataset, so the interface union forces this on it.
    assert hasattr(overpass, "search_room_offers")


def test_every_declaration_names_a_real_capability():
    """A typo would silently do nothing, which is worse than an error.

    `UNSERVED_CAPABILITIES = {"search_room_offer"}` would look like a fix and change no
    behaviour at all.
    """
    methods = set(CAPABILITY_BY_METHOD)
    for provider in _free_providers():
        for declared in unserved_capabilities_of(provider):
            assert declared in methods, (
                f"{provider.name} declares an unserved capability that is not a capability: "
                f"{declared!r}. A typo here would silently have no effect."
            )


def test_the_reader_tolerates_a_provider_without_a_declaration():
    """Duck-typed providers and test doubles must not break the catalogue."""

    class Bare:
        pass

    assert unserved_capabilities_of(Bare()) == frozenset()
    assert unserved_capabilities_of(object()) == frozenset()
    assert UNSERVED_CAPABILITIES == frozenset()


# -- the registry's two questions ------------------------------------------------


def test_calling_still_reaches_the_provider_but_reporting_does_not():
    """The two questions must stay different, or a fix in one direction breaks the other.

    Dispatch must keep asking the stub: it declines by returning an empty success, and that
    empty result is both correct and useful — it shows up in the provider audit as "we asked
    and they had nothing", which is different from "we never asked".
    """
    registry = _free_registry()
    overpass = next(p for p in registry.all() if p.name == "openstreetmap")

    # Callable: dispatch includes it.
    assert overpass in registry.providers_for("search_room_offers")
    # Not serving: reporting excludes it.
    assert overpass not in registry.serving_providers_for("search_room_offers")


def test_the_two_questions_agree_where_nothing_is_declared():
    """The filter must only ever remove declared-unserved providers, never others."""
    registry = _free_registry()
    for capability in CAPABILITY_ORDER:
        callable_names = {p.name for p in registry.providers_for(capability)}
        serving_names = {p.name for p in registry.serving_providers_for(capability)}
        assert serving_names <= callable_names, capability
    # Routing is untouched: the stub declaration is the only thing filtered out anywhere.
    assert {p.name for p in registry.serving_providers_for("calculate_routes")} == {"osrm"}
    assert {p.name for p in registry.serving_providers_for("search_hotels")} == {"openstreetmap"}


# -- what the catalogue claims ---------------------------------------------------


def test_no_free_source_is_credited_with_room_offers():
    """The concrete falsehood, asserted gone.

    Neither Overpass nor any other keyless source can produce a room rate, so the capability
    must read as uncovered rather than as served.
    """
    matrix = capability_matrix(
        [provider.name for provider in _free_providers()], _all_capabilities()
    )
    row = next(item for item in matrix if item["capability"] == "search_room_offers")

    assert row["covered"] is False
    assert row["complete"] is False
    assert row["providers"] == []


def test_the_gap_is_named_and_explained():
    """A count is not actionable; the product's premise is knowing what to plug in."""
    registered = [provider.name for provider in _free_providers()]
    gaps = capability_gaps(registered, _all_capabilities())

    assert [gap["capability"] for gap in gaps] == ["search_room_offers"]
    gap = gaps[0]
    assert gap["label"]
    # The note must say what happens *instead*, so the reader connects it to the estimated
    # accommodation line they will see in the budget rather than hunting for a bug.
    assert "估算" in gap["note"]
    assert "未接入" in gap["note"], (
        "the note must be about what is integrated, not an impossibility"
    )


def test_a_gap_only_a_credential_can_fill_is_not_reported_as_a_gap():
    """`nobody serves this` and `you have not configured it` have opposite remedies.

    Transit routing is the case: Amap serves it and simply needs a key. Reporting it as an
    unfillable gap would tell the user to give up on something one credential fixes.
    """
    registered = [provider.name for provider in _free_providers()]
    gaps = capability_gaps(registered, _all_capabilities())
    capability_ids = {gap["capability"] for gap in gaps}

    assert "calculate_routes" not in capability_ids
    # Transit is still reported — but through the *aspect* channel, which is where a
    # partially served capability belongs.
    assert any(
        entry.capabilities and "calculate_routes" in entry.capabilities
        for entry in PROVIDER_CATALOG
    )


def test_every_manifest_capability_is_one_the_provider_actually_serves():
    """The declaration fix, enforced against the catalogue itself.

    Checked **per capability**, not per source. Asserting only that a source has *some*
    registered provider passes even when the specific capability it declared is one that
    provider refuses to serve — which is exactly how the original bug survived: `openstreetmap`
    was registered, so the source looked fine, and nothing noticed that no provider served its
    `search_room_offers` claim.
    """
    registry = _free_registry()
    for entry in PROVIDER_CATALOG:
        if entry.status != "available":
            continue  # needs_credentials / planned are not claims about right now
        for capability in entry.capabilities:
            assert capability in CAPABILITY_BY_METHOD, (
                f"{entry.id} declares {capability!r}, which is not a capability name"
            )
            serving = {
                getattr(provider, "name", "")
                for provider in registry.serving_providers_for(capability)
            }
            assert serving & set(entry.provider_names), (
                f"{entry.id} declares {capability} but none of its providers "
                f"{entry.provider_names} serve it (serving: {sorted(serving)})"
            )


def test_the_note_map_does_not_describe_a_solvable_gap():
    """A note saying "nothing can do this" next to a source that can would be a new lie.

    Kept as a separate check because the notes are hand-written and will drift.
    """
    unlockable = {
        capability
        for entry in PROVIDER_CATALOG
        if entry.status in {"available", "needs_credentials"}
        for capability in entry.capabilities
    }
    # `search_room_offers` is the one entry, and no catalogue entry may claim it — if one
    # ever does, the note must be deleted rather than left contradicting the catalogue.
    for capability in CAPABILITY_GAP_NOTES:
        assert capability not in unlockable, (
            f"{capability} has a 'nothing can serve this' note but a source declares it"
        )
