from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from travel_api.providers.free.http import DEFAULT_USER_AGENT
from travel_api.services.budget_engine import PriceFallback


class Settings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_url: str = "sqlite:///./travel.db"
    #: Note: there is deliberately no Redis/cache URL here. Nothing used it, and
    #: a configurable-but-ignored setting is worse than none. Caching is
    #: currently in-process and TTL-based (see the free providers); a shared
    #: cache can be added back together with a real implementation.
    #: Baseline budget for a provider call. Providers may override this with
    #: their own ``timeout_seconds``; the free community endpoints do, because
    #: Overpass legitimately takes several seconds and 5s would cut it off.
    provider_timeout_seconds: float = 15.0
    #: The free HTTP layer retries transient failures itself with exponential
    #: backoff, so the runtime retry only needs to catch a provider-level
    #: failure. A very short backoff here used to burn every attempt inside the
    #: same transient network window.
    provider_retry_count: int = 1
    provider_backoff_seconds: float = 1.0
    provider_circuit_breaker_threshold: int = 3
    provider_circuit_breaker_reset_seconds: float = 30.0
    provider_fixture_dir: str | None = None

    # -- Provider composition ---------------------------------------------
    # Free, keyless sources are the default data layer. The mock provider is
    # opt-in so synthetic places and prices never silently mix into real
    # results; it stays available for offline and deterministic testing.
    free_providers_enabled: bool = True
    mock_provider_enabled: bool = False

    # -- Free provider tuning ---------------------------------------------
    # A descriptive User-Agent is required by the Nominatim usage policy.
    # Verified live: an e-mail address in the UA gets HTTP 403 "Access denied",
    # and httpx's default UA is also rejected. A plain token plus a "+https://"
    # contact URL is accepted, so put a real contact URL here.
    free_provider_user_agent: str = DEFAULT_USER_AGENT
    free_provider_language: str = "zh"
    #: Budget for one free-provider call. Must comfortably exceed
    #: mirror_attempts x free_provider_request_timeout so fallback can run.
    free_provider_timeout_seconds: float = 90.0
    #: Timeout for a single HTTP request to a community endpoint.
    free_provider_request_timeout_seconds: float = 25.0
    #: Half-width of the POI search box in degrees (~5 km at these latitudes).
    #: Kept deliberately small because Overpass query cost grows with area.
    free_provider_bbox_radius_degrees: float = 0.045
    free_provider_poi_ttl_seconds: int = 3600
    free_provider_weather_ttl_seconds: int = 900
    free_provider_nominatim_fallback: bool = True
    #: Air quality (PM2.5 / European AQI / UV) from Open-Meteo. Same licence and
    #: endpoint family as the forecast, and best-effort: when it fails the trip
    #: still gets its forecast, just without air-quality fields.
    free_provider_air_quality_enabled: bool = True
    #: Public holidays from the MIT-licensed `holidays` library (local data, no
    #: network). Free for any use, including commercial.
    free_provider_holidays_enabled: bool = True
    #: Optional ISO alpha-2 override; blank means "geocode the destination".
    free_provider_holidays_country_code: str = ""
    #: Attraction descriptions from Wikipedia/Wikidata. Only ever applied to POIs
    #: carrying an explicit OSM wikipedia/wikidata tag (CC BY-SA 4.0).
    free_provider_wikipedia_enabled: bool = True
    #: Article text changes slowly, so this TTL is much longer than for POIs.
    free_provider_wikipedia_ttl_seconds: int = 604800
    #: Retries inside the shared free HTTP client, with exponential backoff. The
    #: public endpoints intermittently drop the first connection, so this is what
    #: makes real-data runs dependable. Rate limits still apply to every attempt
    #: because each one passes through the per-host limiter.
    free_provider_retry_count: int = 3
    free_provider_retry_backoff_seconds: float = 0.5
    #: Comma-separated Overpass mirrors, tried in order.
    overpass_mirrors: str = (
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter,"
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    )
    #: Routing server to use. Empty means the built-in default (FOSSGIS, which has real
    #: foot and bike profiles worldwide). Point it at the OSRM demo server
    #: (https://router.project-osrm.org) or a self-hosted instance to change that.
    osrm_base_url: str = ""
    #: Comma-separated routing profiles the configured server honours, for a self-hosted
    #: deployment.
    #:
    #: Only needed when pointing ``osrm_base_url`` somewhere unrecognised. An unknown host is
    #: treated as **car-only**, because a routing server that lacks the profile you ask for
    #: does not fail — the OSRM demo answers a foot request with car durations and HTTP 200.
    #: Assuming a pedestrian profile we cannot verify is how a driving time ends up in a
    #: walking leg, so the pessimistic default applies and an operator who knows their server
    #: supports more can say so. Recognised values: ``driving``, ``foot``, ``bike``.
    osrm_honoured_profiles: str = ""

    # -- AMap / 高德 ------------------------------------------------------
    #: Whether owners may plug in an AMap key. Off means the catalogue still lists
    #: the source but no credentialed provider is ever built, so an operator can
    #: withdraw it without editing code.
    amap_enabled: bool = True
    #: Overrides the API host, for a proxy or an offline test double.
    amap_base_url: str = "https://restapi.amap.com"
    #: Lifetime stamped on AMap data. Deliberately minutes, not hours, and there is
    #: no in-process cache for it: AMap's terms forbid storing or caching results,
    #: so every call is a live call. That is also what makes route and POI data
    #: genuinely current rather than "current as of whenever we last looked" — and
    #: the ToS reason and the freshness reason point the same way.
    amap_ttl_seconds: int = 300
    #: How many places AMap will route per trip. Each point beyond the distance sweep
    #: costs an extra request and the free personal quota is a monthly call count, so
    #: this is a real cost decision — and it belongs to whoever owns the key, which is
    #: why it is configurable rather than hardcoded. The default matches the OSRM
    #: provider's budget so that supplying a key never *reduces* the share of the
    #: itinerary that gets a measured route. The planner assembles up to
    #: ``MAX_ROUTE_POINTS`` (40) points; anything beyond this budget has its legs
    #: estimated, and the route's source reference reports that as ``truncated``.
    #:
    #: Clamped by ``AMAP_MAX_MATRIX_ORIGINS`` (100, AMap's documented per-request
    #: origin limit) because that is a property of the protocol, not a preference.
    amap_max_route_points: int = 25
    #: Requests per second allowed against the AMap host. The free personal quota
    #: is a monthly call count, not a rate, but a limiter keeps a burst from
    #: tripping the per-second guard AMap also enforces.
    amap_requests_per_second: float = 5.0

    # -- RollingGo / 道旅 -------------------------------------------------
    #: Whether owners may plug in a RollingGo key. Off means the catalogue still lists
    #: the source but no credentialed provider is ever built, so an operator can
    #: withdraw it without editing code.
    rollinggo_enabled: bool = True
    #: The MCP endpoint. RollingGo exposes hotel search and live rates over MCP
    #: (streamable-HTTP) rather than REST, so this program speaks JSON-RPC to it.
    #: Overridable for a proxy or an offline test double.
    rollinggo_base_url: str = "https://mcp.rollinggo.cn/mcp"
    #: Budget for one MCP tool call. No session handshake is issued, so this is the cost
    #: of a single invocation rather than of a session.
    rollinggo_timeout_seconds: float = 30.0
    #: Requests per second against the MCP host. The service publishes no rate limit and
    #: currently advertises no call cap, so this is a politeness guard rather than a
    #: compliance number — kept low because a plan can issue one call per candidate hotel.
    rollinggo_requests_per_second: float = 2.0
    #: Lifetime stamped on a rate. Minutes rather than hours: a room rate is a perishable
    #: commercial fact. Nothing is cached — every plan re-queries live — so this only
    #: bounds how long a persisted snapshot may still be shown as current.
    rollinggo_ttl_seconds: int = 1800

    auto_create_schema: bool = True

    # -- Authentication ----------------------------------------------------
    #: When true, every trip route requires a bearer API key and results are
    #: scoped to the key's owner. Defaults to false so a fresh clone runs without
    #: key management, and /api/v1/health reports the mode either way so an
    #: unauthenticated deployment cannot be mistaken for a secured one.
    auth_enabled: bool = False
    #: Owner attributed to requests when ``auth_enabled`` is false.
    auth_anonymous_owner: str = "local-dev"
    #: Bootstrap key, read once at startup to create the first credential.
    #: Leave blank in production and mint keys with
    #: ``python -m travel_api.cli create-key``.
    auth_bootstrap_key: str | None = None
    #: Bootstrap key owner, used only with ``auth_bootstrap_key``.
    auth_bootstrap_owner: str = "admin"

    # -- CORS --------------------------------------------------------------
    #: Comma-separated allowed origins. Blank uses the local development pair.
    #: Read from config because a hard-coded list breaks as soon as the Web app
    #: and the API are not served from the same localhost ports.
    cors_allow_origins: str = ""

    # -- Inbound rate limiting --------------------------------------------
    #: Requests per window per client, applied to the API itself (the per-host
    #: limiter protects upstream providers instead). ``/ai/chat`` is the costly
    #: endpoint because it can spend model tokens.
    rate_limit_requests: int = 120
    rate_limit_window_seconds: float = 60.0
    rate_limit_ai_requests: int = 20

    # -- Budget fallback allowances ---------------------------------------
    # Used only when no priced source exists for a category (the free data
    # layer has no prices). Every resulting budget line is flagged as an
    # estimate; these are planning allowances, never observed facts.
    budget_fallback_hotel_per_night: Decimal = Decimal("400")
    budget_fallback_food_per_person_per_day: Decimal = Decimal("120")
    budget_fallback_attraction_per_person_per_day: Decimal = Decimal("80")
    budget_fallback_transport_per_person_per_day: Decimal = Decimal("50")

    freshness_hotel_offer: int = 300
    freshness_hotel_availability: int = 120
    freshness_restaurant: int = 1800
    freshness_attraction: int = 3600
    freshness_weather: int = 900
    freshness_route: int = 600
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_model: str = "gpt-4o-mini"
    ai_timeout_seconds: float = 30.0
    ai_max_tokens: int = 1200
    ai_max_tool_calls: int = 20
    ai_max_same_tool_calls: int = 5
    conversation_recent_messages: int = 12

    # -- Credential vault --------------------------------------------------
    #: Master key for encrypting stored third-party credentials, as urlsafe
    #: base64 of 32 bytes (``python -c "from travel_api.services.credentials
    #: import generate_master_key; print(generate_master_key())"``).
    #:
    #: Setting this is the stronger option: the key then lives only in the
    #: process environment and never touches the database or its backups. When
    #: it is blank the vault reads ``credential_key_file`` instead and creates it
    #: on first use, which works out of the box but stores the key next to the
    #: data it protects. See ``services/credentials.py`` for exactly what each
    #: mode does and does not defend against.
    credential_encryption_key: str | None = None
    #: Where the master key is kept when ``credential_encryption_key`` is unset.
    credential_key_file: str = ".traver-credential-key"
    #: A malformed or missing key makes the vault refuse to serve secrets rather
    #: than silently minting a replacement, which would look like data loss.
    credential_vault_enabled: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def overpass_mirror_list(self) -> list[str]:
        return [item.strip() for item in self.overpass_mirrors.split(",") if item.strip()]

    def cors_origin_list(self) -> list[str]:
        """Allowed browser origins.

        An explicit list always wins. The localhost pair is a development default
        only; it is returned when nothing is configured so `next dev` keeps
        working out of the box.
        """
        configured = [item.strip() for item in self.cors_allow_origins.split(",") if item.strip()]
        if configured:
            return configured
        return ["http://localhost:3000", "http://127.0.0.1:3000"]

    def price_fallback(self) -> PriceFallback:
        return PriceFallback(
            hotel_per_night=self.budget_fallback_hotel_per_night,
            food_per_person_per_day=self.budget_fallback_food_per_person_per_day,
            attraction_per_person_per_day=self.budget_fallback_attraction_per_person_per_day,
            transport_per_person_per_day=self.budget_fallback_transport_per_person_per_day,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
