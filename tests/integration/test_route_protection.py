"""Every route is either protected or on a short, explicit public list — asked of the app.

Why this file exists
--------------------
``POST /api/v1/tools/{name}`` reached the trip service with no credential required at all.
It was not a mistake in a handler; it was a **missing dependency on the route**, and nothing
in the test suite could see it, because every existing auth test named the routes it checked.
A route that nobody thought to list is exactly the route that gets forgotten.

So this file does not list routes. It asks the built application for its own route table and
walks it, which means a new route is covered the moment it is added. Two failure modes are
caught by construction:

* a new route with no ``get_principal`` dependency — it appears in the walk and returns
  something other than 401 to a caller with no credential;
* a route accidentally made public — it has to be spelled out in ``PUBLIC_ROUTES`` below,
  which is a sentence somebody has to write and a reviewer has to read.

The walk is behavioural rather than introspective: it makes the request. Reading
``route.dependant`` would test that a dependency object exists, not that the route actually
refuses an anonymous caller, and the second is the property that matters. It also survives
FastAPI changing how it represents an included router, which ``app.routes`` already hides
behind an ``_IncludedRouter`` with no ``routes`` attribute.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from travel_api.config import Settings
from travel_api.main import create_app
from travel_api.providers.amap import AMAP_KEY_CREDENTIAL, AMAP_PROVIDER_ID

#: The only routes that may answer a caller with no credential, each with the reason it is
#: public. A name-based list rather than a pattern, so "make everything under /settings
#: public" cannot happen by accident.
PUBLIC_ROUTES: dict[str, str] = {
    "GET /api/v1/health": "liveness and readiness: a probe has no credential and must not need one",
    "GET /api/v1/providers/attributions": "licence attribution must be readable to be honoured",
    "GET /api/v1/tools": "tool *definitions* only: names, schemas and permissions, no data",
    "GET /api/v1/providers/health": (
        "operational status of the deployment's *shared* sources. It cannot report an owner's "
        "credentialed providers, because the base registry never holds one: credentialed "
        "sources are built per owner by a factory and are never registered on it. So the "
        "payload is the shared source list plus request counters, with no owner data and no "
        "credential value, and the source list is already public via /providers/attributions. "
        "Publishing it is the same deliberate choice /health already makes when it reports "
        "credential_vault: locked — and, here, whether fixture providers are enabled. An "
        "operator who believes they are serving a configured deployment while it is answering "
        "from mocks is the failure that hiding this would cause."
    ),
}

#: Values for the path parameters, so a request can actually be routed. A parameter that is
#: never resolved would 404 before authentication ran and the walk would pass for the wrong
#: reason — which is the failure this file is most exposed to, so each one is real or
#: well-formed rather than a placeholder that cannot match.
_PLACEHOLDERS = {
    "trip_id": "00000000-0000-0000-0000-000000000000",
    "ingestion_id": "00000000-0000-0000-0000-000000000000",
    "job_id": "00000000-0000-0000-0000-000000000000",
    "provider_id": AMAP_PROVIDER_ID,
    "credential_key": AMAP_KEY_CREDENTIAL,
    "tool_name": "get_budget",
}


def _settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'routes.db'}",
        auto_create_schema=True,
        # Authentication on, because the question is what an anonymous caller gets.
        auth_enabled=True,
        free_providers_enabled=False,
        mock_provider_enabled=True,
        provider_retry_count=0,
        credential_key_file=str(tmp_path / "credential.key"),
    )


def _routes(app) -> list[tuple[str, str]]:
    """Every (method, path) the app serves, from its own OpenAPI document."""
    entries: list[tuple[str, str]] = []
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            entries.append((method.upper(), path))
    return sorted(entries)


def _fill(path: str) -> str:
    filled = path
    for name, value in _PLACEHOLDERS.items():
        filled = filled.replace(f"{{{name}}}", value)
    return filled


def test_the_walk_sees_every_route_the_app_serves(tmp_path):
    """A guard that silently checked nothing would pass forever.

    The count is asserted as a floor rather than an equality so adding a route does not
    require editing this test — but a walk that suddenly sees three routes instead of
    thirty-eight fails loudly.
    """
    app = create_app(_settings(tmp_path))
    routes = _routes(app)

    assert len(routes) >= 30, f"the route walk found only {len(routes)} routes: {routes}"
    # Spot-check that the walk is reading the real table, including the route whose
    # missing dependency this file was written for.
    assert ("POST", "/api/v1/tools/{tool_name}") in routes
    assert ("GET", "/api/v1/trips") in routes


def test_every_route_is_protected_unless_it_is_declared_public(tmp_path):
    """The property: with authentication on, nobody gets in without a credential.

    Asserting ``!= 200`` would be too weak — a 422 from a malformed body also is not a 200,
    and it would hide an unprotected route behind a validation error. The assertion is
    therefore on 401 specifically, and a route that answers anything else is reported with
    what it did answer.
    """
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        unprotected = []
        for method, path in _routes(app):
            if f"{method} {path}" in PUBLIC_ROUTES:
                continue
            response = client.request(method, _fill(path), json={})
            if response.status_code != 401:
                unprotected.append((f"{method} {path}", response.status_code, response.text[:120]))

    assert not unprotected, (
        "these routes answered a caller with no credential with something other than 401:\n"
        + "\n".join(f"  {entry} -> {status} {body}" for entry, status, body in unprotected)
    )


def test_every_public_route_is_really_public_and_still_reachable(tmp_path):
    """The other direction: the allowlist must not go stale.

    A route listed as public that has since been protected is a lie in the comments, and —
    more practically — a health probe that starts needing a key takes the deployment down.
    """
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        for entry in PUBLIC_ROUTES:
            method, path = entry.split(" ", 1)
            response = client.request(method, _fill(path), json={})
            assert response.status_code == 200, (
                f"{entry} is listed public but returned {response.status_code}"
            )


def test_the_public_allowlist_is_all_reasons_and_no_leftovers(tmp_path):
    """Every entry carries a reason, and no entry is for a route that no longer exists.

    A stale allowlist entry is worse than a missing one: it is a standing exemption for a
    path that, if a route is ever added back under the same name, is public from birth.
    """
    app = create_app(_settings(tmp_path))
    served = {f"{method} {path}" for method, path in _routes(app)}

    for entry, reason in PUBLIC_ROUTES.items():
        assert reason.strip(), f"{entry} is public with no reason given"
        assert entry in served, f"{entry} is allowlisted but the app does not serve it"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/tools/{tool_name}"),
        ("GET", "/api/v1/tools"),
    ],
)
def test_the_tool_route_pair_is_split_the_way_the_allowlist_says(tmp_path, method, path):
    """The distinction that matters, stated once so it cannot be lost in a refactor.

    Listing the tool *definitions* is public on purpose: a client needs to know what exists
    before it has a key, and the payload is names and schemas. *Calling* a tool is not, and
    the two live under the same prefix — which is why the allowlist is per route rather than
    per prefix.
    """
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.request(method, _fill(path), json={})

    if f"{method} {path}" in PUBLIC_ROUTES:
        assert response.status_code == 200
    else:
        assert response.status_code == 401
