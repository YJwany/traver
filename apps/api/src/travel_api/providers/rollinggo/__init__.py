"""RollingGo provider package.

``build_rollinggo_providers`` is the one entry point the application uses. It is called
**per request** with the owner's own key, never once at startup, for the reason the AMap
package documents: a provider built once keeps whichever key it was first constructed with,
so a key saved in the settings page would appear not to work and a removed key would keep
being used.

Everything here requires a key. With none configured the package is never built and the
application runs on the free sources alone — RollingGo is the upgrade that makes room rates
possible, not a dependency.
"""

from __future__ import annotations

from typing import Any

from travel_api.providers.rollinggo.client import (
    DEFAULT_ROLLINGGO_BASE_URL,
    MCP_ACCEPT,
    ROLLINGGO_KEY_CREDENTIAL,
    ROLLINGGO_PROVIDER_ID,
    RollingGoError,
    RollingGoKeyError,
    RollingGoMcpClient,
    RollingGoQuotaError,
    parse_jsonrpc_payload,
    unwrap_result,
)
from travel_api.providers.rollinggo.hotels import (
    ROLLINGGO_HOTEL_PROVIDER_NAME,
    RollingGoHotelProvider,
    locate_list,
)

__all__ = [
    "DEFAULT_ROLLINGGO_BASE_URL",
    "MCP_ACCEPT",
    "ROLLINGGO_HOTEL_PROVIDER_NAME",
    "ROLLINGGO_KEY_CREDENTIAL",
    "ROLLINGGO_PROVIDER_ID",
    "ROLLINGGO_PROVIDER_NAMES",
    "RollingGoError",
    "RollingGoHotelProvider",
    "RollingGoKeyError",
    "RollingGoMcpClient",
    "RollingGoQuotaError",
    "build_rollinggo_providers",
    "locate_list",
    "parse_jsonrpc_payload",
    "unwrap_result",
]

#: Registry names for the providers this source backs. A tuple because the registry keys
#: providers by name and 高德's three-providers-one-name defect — only the last survived
#: registration, silently — is the failure this shape exists to prevent. ``ROLLINGGO_PROVIDER_ID``
#: remains the single *source* id: it is what the settings page writes credentials against
#: and what the manifest describes.
ROLLINGGO_PROVIDER_NAMES: tuple[str, ...] = (ROLLINGGO_HOTEL_PROVIDER_NAME,)


def build_rollinggo_providers(
    *,
    key: str,
    http: Any,
    base_url: str = DEFAULT_ROLLINGGO_BASE_URL,
    timeout_seconds: float = 30.0,
    ttl_seconds: int = 1800,
) -> list[Any]:
    """Build the RollingGo-backed providers for one owner's key.

    Raises :class:`RollingGoKeyError` for a blank key. Callers treat that as "not
    configured" rather than a failure: it means the owner has supplied none, and the free
    sources already cover their trip.
    """
    client = RollingGoMcpClient(
        http=http,
        key=key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    return [RollingGoHotelProvider(client, ttl_seconds=ttl_seconds)]
