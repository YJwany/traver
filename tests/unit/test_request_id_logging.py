"""Request-id correlation between log records and the x-request-id header.

The documented log format is
``%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s``, so
that field has to be populated for real. Previously the middleware generated an
id, stored it on ``request.state`` and echoed it in the response header, but
never published it to the logging context: every line printed ``request_id=-``
and no log could be tied back to the request that produced it.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from travel_api.logging_config import (
    RequestIdFilter,
    get_request_id,
    reset_request_id,
    set_request_id,
)

LOG_FORMAT = "%(levelname)s %(name)s request_id=%(request_id)s %(message)s"

#: Every application logger lives under this namespace, and the middleware in
#: travel_api.main logs through "travel_api.main", so capturing here catches all
#: of them regardless of which module emitted the record.
APP_LOGGER = "travel_api"


@pytest.fixture
def log_lines():
    """Capture formatted application log records."""
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    handler.addFilter(RequestIdFilter())
    logger = logging.getLogger(APP_LOGGER)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _format(record: logging.LogRecord) -> str:
    return logging.Formatter(LOG_FORMAT).format(record)


def test_the_filter_fills_the_request_id_from_the_context(log_lines):
    logger = logging.getLogger("travel_api.request_id_probe")
    token = set_request_id("ctx-123")
    try:
        logger.info("doing work")
    finally:
        reset_request_id(token)

    assert len(log_lines) == 1
    assert "request_id=ctx-123" in _format(log_lines[0])


def test_the_request_id_defaults_to_placeholder_outside_a_request(log_lines):
    assert get_request_id() == "-"
    logging.getLogger("travel_api.request_id_probe").info("background work")
    assert "request_id=-" in _format(log_lines[0])


def test_an_explicit_request_id_wins_over_the_context(log_lines):
    logger = logging.getLogger("travel_api.request_id_probe")
    token = set_request_id("ctx-123")
    try:
        logger.info("labelled work", extra={"request_id": "explicit-456"})
    finally:
        reset_request_id(token)

    assert "request_id=explicit-456" in _format(log_lines[0])


def test_the_context_is_restored_after_reset(log_lines):
    token = set_request_id("first")
    assert get_request_id() == "first"
    reset_request_id(token)
    assert get_request_id() == "-"


def _test_app(tmp_path):
    """Real app, hermetic providers, temp database."""
    from travel_api.config import Settings
    from travel_api.main import create_app

    return create_app(
        Settings(
            app_env="test",
            database_url=f"sqlite:///{tmp_path / 'request_id.db'}",
            auto_create_schema=True,
            provider_retry_count=0,
            free_providers_enabled=False,
            mock_provider_enabled=True,
        )
    )


def test_requests_are_logged_with_the_id_returned_to_the_client(log_lines, tmp_path):
    """The end-to-end contract: header, application log and request.state agree."""
    with TestClient(_test_app(tmp_path)) as client:
        response = client.get("/api/v1/health", headers={"x-request-id": "trace-me-001"})
    assert response.status_code == 200
    echoed = response.headers["x-request-id"]
    assert echoed == "trace-me-001"

    messages = [_format(record) for record in log_lines]
    matching = [line for line in messages if "trace-me-001" in line]
    assert matching, f"no application log line carried the request id; got {messages}"
    assert any("/api/v1/health" in line for line in matching)


def test_a_generated_request_id_is_still_logged(log_lines, tmp_path):
    with TestClient(_test_app(tmp_path)) as client:
        response = client.get("/api/v1/health")
    generated = response.headers["x-request-id"]
    assert generated

    messages = [_format(record) for record in log_lines]
    assert any(generated in line for line in messages), messages


def test_the_request_id_does_not_leak_between_requests(log_lines, tmp_path):
    with TestClient(_test_app(tmp_path)) as client:
        client.get("/api/v1/health", headers={"x-request-id": "first-request"})
        first_count = sum("first-request" in _format(r) for r in log_lines)

        client.get("/api/v1/health", headers={"x-request-id": "second-request"})
        # The first id must not reappear on the second request's lines.
        assert sum("first-request" in _format(r) for r in log_lines) == first_count
        assert any("second-request" in _format(r) for r in log_lines)

    # And after the request finishes, nothing is bound any more.
    assert get_request_id() == "-"


def test_provider_failures_are_not_labelled_system(log_lines):
    """A hardcoded request_id="system" used to hide the real id where it matters most."""
    import asyncio
    from datetime import date
    from decimal import Decimal

    from travel_api.domain.models import TripProfile
    from travel_api.providers.registry import ProviderRegistry
    from travel_api.services.aggregation import build_preview

    class Boom:
        name = "boom"

        async def search_attractions(self, profile):  # noqa: ANN001, ARG002
            raise RuntimeError("provider exploded")

    registry = ProviderRegistry([Boom()])
    profile = TripProfile(
        destination="成都",
        start_date=date(2026, 10, 3),
        end_date=date(2026, 10, 5),
        travelers=1,
        total_budget=Decimal("1000"),
    )

    token = set_request_id("request-with-failure")
    try:
        asyncio.run(build_preview(profile, registry))
    finally:
        reset_request_id(token)

    failures = [r for r in log_lines if "provider exploded" in r.getMessage()]
    assert failures, "expected a provider failure log line"
    assert "request_id=request-with-failure" in _format(failures[0])
    assert "request_id=system" not in _format(failures[0])


def test_the_app_actually_uses_the_documented_log_format():
    """configure_logging must not be silently overridden or skipped.

    ``logging.basicConfig`` is a no-op when the root logger already has a
    handler, so without ``force=True`` the documented format (the one carrying
    request_id) could be dropped with no error at all.
    """
    from travel_api.logging_config import LOG_FORMAT, configure_logging

    assert "request_id=%(request_id)s" in LOG_FORMAT

    root = logging.getLogger()
    saved = list(root.handlers)
    # Simulate a pre-existing handler, which used to defeat basicConfig.
    root.addHandler(logging.NullHandler())
    try:
        configure_logging("INFO")
        formats = {getattr(h.formatter, "_fmt", None) for h in root.handlers}
        assert any(fmt and "request_id=%(request_id)s" in fmt for fmt in formats), formats
        assert all(any(isinstance(f, RequestIdFilter) for f in h.filters) for h in root.handlers)
    finally:
        root.handlers[:] = saved

    # And the application's own log level is honoured, not left at WARNING.
    configure_logging("WARNING")
    try:
        assert logging.getLogger().level == logging.WARNING
    finally:
        root.handlers[:] = saved


def test_the_minimal_app_shape_is_supported():
    """Guard the middleware against FastAPI's expectation of an ASGI app."""
    app = FastAPI()

    @app.get("/ping")
    async def ping():  # noqa: ANN202
        return {"pong": True}

    assert TestClient(app).get("/ping").json() == {"pong": True}
