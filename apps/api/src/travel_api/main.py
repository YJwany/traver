from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from travel_api.ai import OpenAICompatibleAdapter
from travel_api.api.rate_limit import RateLimiter, client_key, too_many_requests
from travel_api.api.routes import router
from travel_api.config import Settings, get_settings
from travel_api.db.session import Database
from travel_api.db.uow import UnitOfWork
from travel_api.logging_config import configure_logging, reset_request_id, set_request_id
from travel_api.providers.amap import (
    AMAP_KEY_CREDENTIAL,
    AMAP_PROVIDER_ID,
    AmapKeyError,
    build_amap_providers,
)
from travel_api.providers.fixture import FixtureTravelProvider
from travel_api.providers.free import build_free_providers
from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.mock import MockTravelProvider
from travel_api.providers.registry import ProviderRegistry
from travel_api.providers.resilience import ProviderCallPolicy, ProviderRuntime
from travel_api.services.auth import KEY_PREFIX, AuthenticationError, AuthService, hash_api_key
from travel_api.services.budget_engine import BudgetEngine
from travel_api.services.content_service import ContentIngestionService
from travel_api.services.credentials import (
    CredentialCipher,
    CredentialResolver,
    CredentialVault,
    CredentialVaultError,
    load_master_key,
)
from travel_api.services.freshness import FreshnessPolicy, FreshnessService
from travel_api.services.model_runtime import ModelRuntime
from travel_api.services.owner_context import reset_current_owner, set_current_owner
from travel_api.services.planning_jobs import PlanningJobRegistry
from travel_api.services.tool_registry import build_default_tool_registry
from travel_api.services.travel_ai import TravelAIService
from travel_api.services.trip_service import TripService

settings = get_settings()
configure_logging(settings.log_level)

logger = logging.getLogger(__name__)


def _bearer_from_header(request: Request) -> str | None:
    """Read a bearer credential, ignoring any other scheme."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


#: Paths exempt from inbound rate limiting. Health and attribution endpoints are
#: cheap, unauthenticated, and are what a probe or the Web shell polls, so
#: limiting them would cause outages without protecting anything.
_RATE_LIMIT_EXEMPT_SUFFIXES = ("/health", "/providers/health", "/providers/attributions")


def _is_rate_limited_path(path: str) -> bool:
    return not any(path.endswith(suffix) for suffix in _RATE_LIMIT_EXEMPT_SUFFIXES)


def _bootstrap_api_key(app_settings: Settings, database: Database) -> str | None:
    """Ensure the configured bootstrap key exists. Returns its display prefix.

    Deliberately one-way: the configured value is hashed and inserted if absent,
    and never re-read for comparison against stored rows. That keeps a key in an
    environment variable from silently remaining valid forever if the row is
    revoked — revoking wins, because nothing re-creates it on the next boot
    unless it is missing entirely.

    Returns a short display prefix (never the key) so startup logs can confirm
    bootstrap happened without leaking the credential.
    """
    configured = (app_settings.auth_bootstrap_key or "").strip()
    if not configured:
        return None
    if not configured.startswith(KEY_PREFIX):
        logger.warning(
            "AUTH_BOOTSTRAP_KEY does not start with %r and was ignored; "
            "mint a key with: python -m travel_api.cli create-key",
            KEY_PREFIX,
        )
        return None

    digest = hash_api_key(configured)
    with UnitOfWork(database.session_factory) as uow:
        if uow.api_keys.get_by_hash(digest) is not None:
            return configured[:12]
        uow.api_keys.create(
            key_hash=digest,
            key_prefix=configured[:12],
            owner_id=app_settings.auth_bootstrap_owner,
            label="bootstrap",
            scopes=[],
            created_at=datetime.now(timezone.utc),
        )
    logger.info("bootstrap API key registered for owner=%s", app_settings.auth_bootstrap_owner)
    return configured[:12]


def create_app(
    settings_override: Settings | None = None,
    database_override: Database | None = None,
) -> FastAPI:
    app_settings = settings_override or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = database_override or Database(app_settings.database_url)

        # Provider composition is additive and capability-routed:
        #   fixture (offline) + free keyless sources (real) + mock (synthetic).
        pending_providers: list[object] = []
        free_http_client = None

        if app_settings.provider_fixture_dir:
            pending_providers.append(
                FixtureTravelProvider("fixture-provider", Path(app_settings.provider_fixture_dir))
            )
        if app_settings.free_providers_enabled:
            free_providers, free_http_client = build_free_providers(
                user_agent=app_settings.free_provider_user_agent,
                language=app_settings.free_provider_language,
                request_timeout_seconds=app_settings.free_provider_request_timeout_seconds,
                timeout_seconds=app_settings.free_provider_timeout_seconds,
                bbox_radius_degrees=app_settings.free_provider_bbox_radius_degrees,
                overpass_mirrors=app_settings.overpass_mirror_list(),
                osrm_base_url=app_settings.osrm_base_url or None,
                osrm_honoured_profiles=app_settings.osrm_honoured_profiles or None,
                poi_ttl_seconds=app_settings.free_provider_poi_ttl_seconds,
                weather_ttl_seconds=app_settings.free_provider_weather_ttl_seconds,
                retry_count=app_settings.free_provider_retry_count,
                retry_backoff_seconds=app_settings.free_provider_retry_backoff_seconds,
                nominatim_fallback=app_settings.free_provider_nominatim_fallback,
                air_quality_enabled=app_settings.free_provider_air_quality_enabled,
                holidays_enabled=app_settings.free_provider_holidays_enabled,
                holidays_country_code=app_settings.free_provider_holidays_country_code or None,
                wikipedia_enabled=app_settings.free_provider_wikipedia_enabled,
                wikipedia_ttl_seconds=app_settings.free_provider_wikipedia_ttl_seconds,
            )
            pending_providers.extend(free_providers)
        if app_settings.mock_provider_enabled:
            pending_providers.extend(
                [
                    MockTravelProvider("ctrip-mock"),
                    MockTravelProvider("tongcheng-mock", price_adjustment=1),
                    MockTravelProvider("meituan-mock", price_adjustment=2),
                ]
            )

        registry = ProviderRegistry(
            pending_providers,
            runtime=ProviderRuntime(
                ProviderCallPolicy(
                    timeout_seconds=app_settings.provider_timeout_seconds,
                    retry_count=app_settings.provider_retry_count,
                    backoff_seconds=app_settings.provider_backoff_seconds,
                    circuit_breaker_threshold=app_settings.provider_circuit_breaker_threshold,
                    circuit_breaker_reset_seconds=app_settings.provider_circuit_breaker_reset_seconds,
                )
            ),
        )
        if app_settings.auto_create_schema:
            database.create_all()
        app.state.settings = app_settings
        app.state.database = database
        app.state.provider_registry = registry
        # Constructed before the trip service, because planning reads the caller's own
        # saved notes: a note the traveller supplied is one of the plan's sources.
        ingestion_service = ContentIngestionService(lambda: UnitOfWork(database.session_factory))
        app.state.ingestion_service = ingestion_service
        # Long-running planning. In-process, which is a real limitation and is why the
        # docs say so: a multi-worker deployment would need a shared store before a job
        # created on one worker could be polled on another. For a single-process
        # deployment this has no external dependency that can fail.
        planning_jobs = PlanningJobRegistry()
        app.state.planning_jobs = planning_jobs
        app.state.trip_service = TripService(
            provider_registry=registry,
            uow_factory=lambda: UnitOfWork(database.session_factory),
            budget_engine=BudgetEngine(fallback=app_settings.price_fallback()),
            freshness_service=FreshnessService(
                FreshnessPolicy(
                    hotel_offer=app_settings.freshness_hotel_offer,
                    hotel_availability=app_settings.freshness_hotel_availability,
                    restaurant=app_settings.freshness_restaurant,
                    attraction=app_settings.freshness_attraction,
                    weather=app_settings.freshness_weather,
                    route=app_settings.freshness_route,
                )
            ),
            content_service=ingestion_service,
            jobs=planning_jobs,
        )
        app.state.tool_registry = build_default_tool_registry(app.state.trip_service)

        # Authentication. The service is always constructed so routes can rely on
        # it existing; ``enabled`` is what decides whether a credential is
        # actually required.
        uow_factory = lambda: UnitOfWork(database.session_factory)  # noqa: E731
        auth_service = AuthService(
            uow_factory,
            enabled=app_settings.auth_enabled,
            anonymous_owner=app_settings.auth_anonymous_owner,
        )
        app.state.auth_service = auth_service
        if app_settings.auth_enabled:
            app.state.auth_bootstrap = _bootstrap_api_key(app_settings, database)

        app.state.rate_limiter = RateLimiter(
            requests=app_settings.rate_limit_requests,
            window_seconds=app_settings.rate_limit_window_seconds,
        )
        app.state.ai_rate_limiter = RateLimiter(
            requests=app_settings.rate_limit_ai_requests,
            window_seconds=app_settings.rate_limit_window_seconds,
        )

        # Credential vault. Built before the model runtime because the model is
        # resolved from it. An unusable master key must not take the service down:
        # with no model the local planner still produces a complete plan, so a
        # lock failure degrades, and the reason is reported by /health and the
        # settings page rather than hidden in a log.
        credential_cipher = None
        credential_key_source = "unavailable"
        credential_key_error: str | None = None
        credential_permissions_restricted = True
        if app_settings.credential_vault_enabled:
            try:
                master_key = load_master_key(
                    env_value=app_settings.credential_encryption_key,
                    key_file=Path(app_settings.credential_key_file),
                )
                credential_cipher = CredentialCipher(master_key.key)
                credential_key_source = master_key.source
                credential_permissions_restricted = master_key.permissions_restricted
            except CredentialVaultError as exc:
                credential_key_error = f"{type(exc).__name__}: {exc}"
                logger.error("credential vault is locked: %s", credential_key_error)
        else:
            credential_key_error = "disabled by configuration"

        credential_vault = CredentialVault(
            lambda: UnitOfWork(database.session_factory),
            credential_cipher,
            enabled=credential_cipher is not None,
            locked_reason=credential_key_error,
        )
        credential_resolver = CredentialResolver(credential_vault, app_settings)

        # AMap gets its own HTTP client rather than borrowing the free stack's.
        # Borrowing it made AMap depend on ``free_providers_enabled``: with the free
        # sources switched off there was no client, the factory was never installed,
        # and a saved 高德 key silently did nothing. Its rate limit is also its own,
        # because the per-second guard AMap enforces is not the one the community
        # endpoints need.
        amap_http_client = None
        if app_settings.amap_enabled:
            amap_host = httpx.URL(app_settings.amap_base_url).host or "restapi.amap.com"
            amap_http_client = FreeHttpClient(
                user_agent=app_settings.free_provider_user_agent,
                timeout_seconds=app_settings.free_provider_request_timeout_seconds,
                host_intervals={
                    amap_host: 1.0 / max(app_settings.amap_requests_per_second, 0.1),
                },
                retry_count=app_settings.free_provider_retry_count,
                retry_backoff_seconds=app_settings.free_provider_retry_backoff_seconds,
            )

        def credentialed_providers(owner_id: str | None) -> list[object]:
            """Sources that only exist for an owner who supplied a key.

            Called per request, so a key saved in the settings page is used by the
            very next plan without a restart — and a removed key stops being used,
            which a cached provider instance would not.

            Returns an empty list when nothing is configured, which is the normal
            state: Traver plans complete trips with no credentials at all, so this is
            an upgrade path rather than a requirement.
            """
            if amap_http_client is None:
                return []
            key = credential_resolver.value(owner_id, AMAP_PROVIDER_ID, AMAP_KEY_CREDENTIAL)
            if not key:
                return []
            try:
                return build_amap_providers(
                    key=key,
                    http=amap_http_client,
                    base_url=app_settings.amap_base_url,
                    timeout_seconds=app_settings.free_provider_timeout_seconds,
                    ttl_seconds=app_settings.amap_ttl_seconds,
                    max_route_points=app_settings.amap_max_route_points,
                )
            except AmapKeyError:
                # A stored value that is blank or whitespace. Treated as "not
                # configured" so planning proceeds on the free sources rather than
                # failing because of an unusable setting.
                logger.warning("AMap credential for owner %r is unusable", owner_id)
                return []

        registry.set_credential_provider_factory(credentialed_providers)
        model_runtime = ModelRuntime(
            credential_resolver,
            default_base_url=app_settings.ai_base_url,
            default_model=app_settings.ai_model,
            default_timeout_seconds=app_settings.ai_timeout_seconds,
            default_max_tokens=app_settings.ai_max_tokens,
        )
        app.state.credential_vault = credential_vault
        app.state.credential_resolver = credential_resolver
        app.state.model_runtime = model_runtime
        app.state.credential_key_source = credential_key_source
        app.state.credential_key_error = credential_key_error
        app.state.credential_permissions_restricted = credential_permissions_restricted

        # This adapter is now only the environment-supplied default. The effective
        # model is resolved per request through ``adapter_factory``, so a key
        # saved in the settings page takes effect on the next message without a
        # restart, and two owners can use different models in one deployment.
        #
        # Gated on the base URL alone: a keyless local server (Ollama, LM Studio)
        # is a valid, common choice, and requiring a key made it unreachable.
        adapter = None
        if app_settings.ai_base_url:
            adapter = OpenAICompatibleAdapter(
                base_url=app_settings.ai_base_url,
                api_key=app_settings.ai_api_key or "",
                model=app_settings.ai_model,
                timeout_seconds=app_settings.ai_timeout_seconds,
                # Named ``max_tokens`` on the adapter. This used to say
                # ``default_max_tokens``, which the adapter does not accept, so
                # configuring a model raised TypeError inside the lifespan and the
                # process died on startup. No test caught it because every test
                # ran with the model unconfigured — the one configuration the
                # failure was reachable from was the one nothing exercised.
                max_tokens=app_settings.ai_max_tokens,
            )
        app.state.travel_ai_service = TravelAIService(
            trip_service=app.state.trip_service,
            tool_registry=app.state.tool_registry,
            uow_factory=lambda: UnitOfWork(database.session_factory),
            adapter=adapter,
            adapter_factory=model_runtime.adapter_for,
            max_tool_calls=app_settings.ai_max_tool_calls,
            max_same_tool_calls=app_settings.ai_max_same_tool_calls,
            recent_message_limit=app_settings.conversation_recent_messages,
        )
        try:
            yield
        finally:
            if free_http_client is not None:
                await free_http_client.aclose()
            if amap_http_client is not None:
                await amap_http_client.aclose()
            database.engine.dispose()

    app = FastAPI(
        title="AI Travel Information Aggregation & Planning System",
        version="0.2.0",
        description="Fact-grounded travel aggregation and planning API.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid4()))
        request.state.request_id = request_id
        # Bind it for the logger too, otherwise the configured
        # `request_id=%(request_id)s` log field is a permanent "-" and a log
        # line cannot be matched to the x-request-id response header.
        token = set_request_id(request_id)
        # Bind the owner for the whole request as well, so the deep AI tool path -
        # which has no owner argument to forward - still uses the caller's own
        # credentials. Reset in the ``finally`` so one request cannot leak its
        # owner into the next use of this task.
        owner_token = set_current_owner(None)
        started = time.perf_counter()
        try:
            # Resolve the credential here, once, so that (a) the rate limiter can
            # key on the owner instead of a shared NAT address, and (b) the
            # `get_principal` dependency does not repeat the database lookup.
            # Failure is recorded, not raised: public routes such as /health and
            # /providers/attributions must stay reachable, and the dependency is
            # what turns a missing principal into a 401 for protected routes.
            auth_service = getattr(request.app.state, "auth_service", None)
            if auth_service is not None:
                try:
                    request.state.principal = auth_service.authenticate(
                        _bearer_from_header(request)
                    )
                except AuthenticationError as exc:
                    request.state.principal = None
                    request.state.auth_error = str(exc)
                else:
                    principal = getattr(request.state, "principal", None)
                    if principal is not None:
                        set_current_owner(principal.owner_id)

            limiter = getattr(request.app.state, "rate_limiter", None)
            if limiter is not None and _is_rate_limited_path(request.url.path):
                retry_after = limiter.check(client_key(request))
                if retry_after is not None:
                    return too_many_requests(retry_after)
                if request.url.path.endswith("/ai/chat"):
                    ai_limiter = getattr(request.app.state, "ai_rate_limiter", None)
                    if ai_limiter is not None:
                        ai_retry = ai_limiter.check(client_key(request))
                        if ai_retry is not None:
                            return too_many_requests(ai_retry)

            response = await call_next(request)
        except Exception:
            # Log the failure with the same id the caller will see, then let the
            # exception handlers produce the HTTP response.
            logger.exception(
                "%s %s failed after %.0f ms",
                request.method,
                request.url.path,
                (time.perf_counter() - started) * 1000,
            )
            raise
        else:
            response.headers["x-request-id"] = request_id
            # Logged while the id is still bound, so this line carries it.
            logger.info(
                "%s %s -> %s in %.0f ms",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
            return response
        finally:
            reset_request_id(token)
            reset_current_owner(owner_token)

    allowed_origins = app_settings.cors_origin_list()
    # Browsers reject credentials together with a wildcard origin, so a wildcard
    # configuration must drop the credential flag rather than produce a CORS
    # setup that silently never matches.
    allow_credentials = "*" not in allowed_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=allow_credentials,
        # PUT is required: saving a credential uses PUT
        # /settings/credentials/{provider}/{key}. Without it the browser's CORS
        # preflight rejects the request, and the failure surfaces only in the browser
        # console as a generic network error while the API looks healthy.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app(settings)
