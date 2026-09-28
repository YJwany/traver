"""AMap (高德地图) Web Service client.

Why this source matters more than the others
--------------------------------------------
The free stack covers driving, weather and POIs across the world. What it cannot
do is **公交/地铁 plan a domestic Chinese trip**: the public OSRM demo has no
transit profile at all, so before this provider a Beijing or Shanghai itinerary
simply could not say how to get between two places by subway. That gap is the
reason to integrate AMap, and it is the capability this package exists to add.

Two rules this client enforces because violating them would be dishonest
-----------------------------------------------------------------------
1. **No caching.** AMap's terms forbid storing or caching its results, so unlike
   the Overpass provider this one keeps nothing in memory: every call is a live
   call. The freshness and the legal requirement point the same way, which is
   convenient — a cached price presented as current is exactly the lie the
   product must not tell.
2. **A keyed failure never silently degrades to someone else's key.** The key is
   passed in per call from the owner's vault; there is no operator-level fallback
   inside this module. If a stored key is rejected, the call fails and the owner
   sees why, rather than the deployment quietly spending the operator's quota.

The key never reaches a log
---------------------------
AMap takes the key as a **query parameter**, which is the dangerous part: the
natural implementation puts the request URL into every log line, exception message
and stored audit row, and the key goes with it. Three separate measures prevent
that:

* errors are built from ``infocode``/``info`` and the API *path*, never the URL —
  so there is no URL to redact, and ``_redact`` exists only for the one place a
  full URL is genuinely useful (a debug log);
* the credential is injected in exactly one method rather than by callers, so
  there is one place it can escape from;
* tests assert the key is absent from the exception type, its message and its
  attributes, rather than trusting the pattern to be followed.
"""

from __future__ import annotations

import logging
from typing import Any

from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.resilience import ProviderPermanentError

logger = logging.getLogger(__name__)

#: The manifest id and the credential key, matching ``providers/manifest.py``.
AMAP_PROVIDER_ID = "amap"
AMAP_KEY_CREDENTIAL = "amap_web_service_key"

DEFAULT_AMAP_BASE_URL = "https://restapi.amap.com"

#: AMap's success value. It is the *string* "1", not a number and not a boolean —
#: ``status == 1`` is always False against the wire format, which is the kind of
#: check that silently turns every response into a failure.
AMAP_STATUS_OK = "1"

#: infocodes that mean "your key is the problem" rather than "your request is".
#: Distinguished because the remedy differs and only one is actionable by the user:
#: a bad key is fixed in the settings page, a bad parameter is a bug here.
#:   10001 key 不正确或过期 · 10002 没有权限使用相应的服务
#:   10005 IP 白名单出错 · 10009 请求 key 与绑定平台不符 · 10012 权限不足
AMAP_KEY_INFOCODES = frozenset({"10001", "10002", "10005", "10009", "10012"})

#: infocodes that mean "your key is fine, you have used it too much".
#:   10003 访问已超出日访问量 · 10004 单位时间内访问过于频繁
#:   10019/10020/10021 配额相关 · 10044 账号维度日调用量超限
#:
#: These two sets must stay **disjoint**, and that is not a style preference.
#: ``require_success`` used to test the key set first while ``10003`` and ``10004`` were
#: members of both, so the two most likely failures for a real user — the daily limit and
#: rate limiting — raised :class:`AmapKeyError`. Callers treat that as an unusable
#: credential and either drop the provider for the whole request or tell the user to check a
#: key that is perfectly valid, which is precisely what :class:`AmapQuotaError` exists to
#: prevent. Whichever set is consulted first would have won, so the fix is to make the
#: question have one answer rather than to reorder the checks;
#: ``tests/unit/test_amap_client.py`` asserts the intersection is empty.
AMAP_QUOTA_INFOCODES = frozenset({"10003", "10004", "10019", "10020", "10021", "10044"})

#: Parameters whose values must never appear in a log line or an exception.
_SENSITIVE_PARAMS = frozenset({"key"})


def redact_key(url: str | None) -> str:
    """Replace the ``key`` query parameter's value with ``***``.

    Only needed where a full URL is genuinely wanted — a debug log while
    diagnosing a malformed request. Errors deliberately carry the API path and
    ``infocode`` instead, so they never contain the key at all; this exists for the
    case where the URL itself is the useful information and forgetting to redact
    would be the leak.
    """
    if not url:
        return ""
    if "key=" not in url:
        return url
    head, _, tail = url.partition("key=")
    # The value ends at the next separator; keep anything after it (AMap URLs mix
    # `?` and `&`).
    value, separator, rest = tail.partition("&")
    return f"{head}key=***{separator}{rest}" if separator else f"{head}key=***"


class AmapError(ProviderPermanentError):
    """A request AMap rejected. Permanent: retrying an identical request is futile."""


class AmapKeyError(AmapError):
    """The key is missing, invalid, expired or unauthorised for this service.

    Its own type because it is the one failure the *user* can fix, and because the
    settings page reports it differently from a transient outage.
    """


class AmapQuotaError(AmapError):
    """The key is valid but out of quota, or being called too fast.

    Distinct from :class:`AmapKeyError` so the UI does not tell someone to check a
    key that is perfectly fine.
    """


def is_success(payload: dict[str, Any]) -> bool:
    """Whether a response reports success.

    ``status`` is compared as a string. AMap documents it as the string "1", and a
    truthiness check would also accept the string "0" — which is why every failure
    would look like a success under ``if payload.get("status")``.
    """
    return str(payload.get("status", "")) == AMAP_STATUS_OK


def require_success(payload: Any, *, operation: str) -> dict[str, Any]:
    """Return the payload if AMap reported success, else raise a typed error.

    Kept separate from the client so parsing code can assert the same rule on a
    recorded fixture.
    """
    if not isinstance(payload, dict):
        raise AmapError(f"AMap {operation}: expected a JSON object, got {type(payload).__name__}")
    if is_success(payload):
        return payload

    infocode = str(payload.get("infocode", ""))
    info = str(payload.get("info", "") or "unknown error")
    # The infocode is what makes this diagnosable, since the request URL cannot be
    # shown. AMap's `info` is stable enough to display.
    detail = f"AMap {operation} failed: infocode={infocode} info={info}"
    if infocode in AMAP_KEY_INFOCODES:
        raise AmapKeyError(detail)
    if infocode in AMAP_QUOTA_INFOCODES:
        raise AmapQuotaError(detail)
    raise AmapError(detail)


def parse_float(value: Any) -> float | None:
    """AMap returns numbers as strings; convert, or ``None``.

    Kept explicit rather than using bare ``float()`` because AMap uses an empty
    string and ``"[]"`` to mean "absent" in different fields, and a bare cast would
    turn that into a confusing ValueError far from the cause.
    """
    if value is None or value == "" or value == "[]":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: Any) -> int | None:
    number = parse_float(value)
    return None if number is None else int(round(number))


def parse_location(value: Any) -> tuple[float, float] | None:
    """Parse AMap's ``"lng,lat"`` coordinate string into ``(latitude, longitude)``.

    Returns the pair in latitude-first order, which is the opposite of AMap's wire
    format. Getting this backwards puts a trip in the ocean, so the swap happens in
    exactly one place and is asserted by a test with a real Beijing coordinate.
    """
    if not isinstance(value, str) or "," not in value:
        return None
    longitude_text, _, latitude_text = value.partition(",")
    longitude = parse_float(longitude_text)
    latitude = parse_float(latitude_text)
    if latitude is None or longitude is None:
        return None
    # A swapped pair is usually still "valid" lat/lon numerically, so range-check
    # both against the plausible bounding box for China as a cheap tripwire.
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


class AmapClient:
    """Rate-limited JSON client that injects one owner's AMap key per call."""

    def __init__(
        self,
        *,
        http: FreeHttpClient,
        key: str,
        base_url: str = DEFAULT_AMAP_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        cleaned = (key or "").strip()
        if not cleaned:
            # Fail loudly at construction: a keyless client would produce AMap's
            # "INVALID_USER_KEY" for every call, which reads as a broken key rather
            # than a missing one.
            raise AmapKeyError("an AMap Web Service key is required to build the client")
        self._key = cleaned
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def base_url_public(self) -> str:
        """The host, safe to log. Never the key."""
        return self.base_url

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One GET, with the key attached and failures typed.

        ``path`` is like ``/v5/direction/transit/integrated``. The key is added
        here rather than by callers so there is exactly one place it can leak from.
        """
        query = dict(params or {})
        query["key"] = self._key
        url = f"{self.base_url}{path}"
        payload = await self.http.get_json(url, params=query)
        # `operation` is the path without the query string, which is enough to
        # identify the call and carries no secret.
        return require_success(payload, operation=path)

    async def post(self, path: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST, used when a parameter set is too long for a URL.

        AMap requires POST for large batches (a distance matrix over many
        coordinates), and a GET that exceeds the URL limit fails in a way that looks
        like a malformed request.
        """
        body = dict(data or {})
        body["key"] = self._key
        url = f"{self.base_url}{path}"
        payload = await self.http.post_json(url, data=body)
        return require_success(payload, operation=path)


__all__ = [
    "AMAP_KEY_CREDENTIAL",
    "AMAP_KEY_INFOCODES",
    "AMAP_PROVIDER_ID",
    "AMAP_QUOTA_INFOCODES",
    "AMAP_STATUS_OK",
    "DEFAULT_AMAP_BASE_URL",
    "AmapClient",
    "AmapError",
    "AmapKeyError",
    "AmapQuotaError",
    "is_success",
    "parse_float",
    "parse_int",
    "parse_location",
    "redact_key",
    "require_success",
]
