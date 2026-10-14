"""RollingGo（道旅）hotel MCP client.

Why this source
---------------
Traver ships no data of its own, and until now **no source could produce a room rate**:
``search_room_offers`` had no provider at all, so the accommodation line in every budget was
an estimate. RollingGo (道旅集团) is a Chinese hotel-supply aggregator that exposes hotel
search and live rates over MCP with a self-service personal API key — the first candidate
that is both usable by an individual and strong on domestic coverage, which is why it was
chosen over the international alternatives.

MCP is not a different transport
--------------------------------
It is still HTTP. What differs is the *shape* of the conversation: a JSON-RPC 2.0 envelope
(``{"jsonrpc": "2.0", "method": "tools/call", ...}``) rather than a REST path, and a server
that may answer ``text/event-stream`` rather than ``application/json``. Two consequences:

* ``Accept`` must list both ``application/json`` and ``text/event-stream``; the server
  returns HTTP 400 otherwise. The shared HTTP client defaults to ``application/json``, so
  the header is set per request.
* The body cannot be assumed to be JSON, so the request goes through
  ``FreeHttpClient.request_raw`` and the SSE framing is stripped here.

Verified vs not
---------------
Verified against the project's README (2026-10-14): the endpoint, the ``Authorization:
Bearer`` scheme, the required ``Accept`` pair, the JSON-RPC request shape, and the
``searchHotels`` argument names (``place`` / ``placeType`` / ``originQuery`` /
``checkInParam`` / ``filterOptions`` / ``size``).

**Not verified: the response envelope, and the ``getHotelDetail`` argument names.** The
README describes a search result in prose and shows one ``getHotelDetail`` output, but never
publishes a full response envelope, and the detail tool's arguments are only inferable from
a CLI example. So nothing here assumes a single layout: the parser locates the hotel and
room arrays among the keys a payload of this kind plausibly uses and records which one
matched on the source reference, so a real run reports the layout instead of the assumption.
No key matching yields no hotels — never a fabricated one.
``scripts/probe_rollinggo_shape.py`` prints the actual envelope and is the only way to turn
these assumptions into facts.

The key never reaches a log
---------------------------
Unlike AMap, the key travels in the ``Authorization`` header rather than a query parameter,
so there is no URL to redact. Errors are built from the tool name and the JSON-RPC error,
never from the request.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from travel_api.providers.free.http import FreeHttpClient
from travel_api.providers.resilience import ProviderPermanentError

logger = logging.getLogger(__name__)

#: The manifest id and the credential key, matching ``providers/manifest.py``.
ROLLINGGO_PROVIDER_ID = "rollinggo"
ROLLINGGO_KEY_CREDENTIAL = "rollinggo_api_key"

DEFAULT_ROLLINGGO_BASE_URL = "https://mcp.rollinggo.cn/mcp"

#: The server returns HTTP 400 unless ``Accept`` lists both content types. Verified in the
#: README, which states it explicitly and shows it in the working cURL example.
MCP_ACCEPT = "application/json, text/event-stream"

JSONRPC_VERSION = "2.0"
MCP_TOOLS_CALL = "tools/call"


class RollingGoError(ProviderPermanentError):
    """A request the RollingGo MCP server rejected. Permanent: retrying it is futile."""


class RollingGoKeyError(RollingGoError):
    """The key is missing, invalid or unauthorised.

    Its own type because it is the one failure the *user* can fix, and the settings page
    reports it differently from a transient outage.
    """


class RollingGoQuotaError(RollingGoError):
    """The key is valid but the account is over quota.

    Kept apart from :class:`RollingGoKeyError` for the reason 高德's two sets are kept
    apart: telling someone to check a key that is perfectly fine sends them to fix the
    wrong thing.
    """


def parse_jsonrpc_payload(text: str) -> dict[str, Any]:
    """Extract one JSON-RPC object from a body that may be SSE-framed.

    ``text/event-stream`` delivers the same JSON prefixed by ``data:`` lines, and a stream
    may carry several frames (progress notifications before the result). The last frame
    that parses as an object is returned, because that is where the JSON-RPC response
    lives; earlier frames are notifications and carry no ``result``.

    A module function so the framing can be asserted on a recorded body without a live
    server.
    """
    stripped = text.strip()
    if not stripped:
        raise RollingGoError("RollingGo returned an empty body")
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError as exc:
            raise RollingGoError("RollingGo returned a non-JSON body") from exc
        if isinstance(payload, dict):
            return payload
        raise RollingGoError("RollingGo returned a JSON value that is not an object")

    last: dict[str, Any] | None = None
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:") :].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            candidate = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            last = candidate
    if last is None:
        raise RollingGoError("RollingGo returned an event stream with no JSON-RPC frame")
    return last


def unwrap_result(payload: dict[str, Any], *, operation: str) -> Any:
    """Return the tool's own payload from a JSON-RPC response.

    MCP wraps a tool's output in ``result.content`` as a list of parts, and a text part
    holds the tool's JSON as a *string*. Both layers are unwrapped here so callers see the
    tool's payload rather than the transport's envelope.

    A JSON-RPC ``error`` is reported with its code and message and **not** mapped onto a
    category: the server publishes no error-code table, so guessing one would be inventing
    a taxonomy. The HTTP status is what distinguishes a rejected key (see
    :meth:`RollingGoMcpClient.call_tool`).
    """
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or "unknown error"
        raise RollingGoError(f"RollingGo {operation} failed: code={code} message={message}")
    if "result" not in payload:
        raise RollingGoError(f"RollingGo {operation} returned no result")

    result = payload["result"]
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        raise RollingGoError(f"RollingGo {operation} reported a tool error")

    content = result.get("content")
    if isinstance(content, list):
        joined = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
        if joined:
            try:
                return json.loads(joined)
            except ValueError:
                # A tool may legitimately answer with prose. Hand it back unchanged; the
                # caller decides whether it can use it, rather than this layer inventing
                # structure that is not there.
                return joined
    return result


class RollingGoMcpClient:
    """Calls RollingGo MCP tools with one owner's key. Caches nothing."""

    def __init__(
        self,
        *,
        http: FreeHttpClient,
        key: str,
        base_url: str = DEFAULT_ROLLINGGO_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        cleaned = (key or "").strip()
        if not cleaned:
            # Fail loudly at construction: a keyless client would get an authorisation
            # error on every call, which reads as a broken key rather than a missing one.
            raise RollingGoKeyError("a RollingGo API key is required to build the client")
        self._key = cleaned
        self.http = http
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke one MCP tool and return its payload.

        No ``initialize`` handshake is issued, because the README's working cURL example
        calls ``tools/call`` directly against this endpoint. If the live server turns out
        to require a session, this is the method that has to grow one —
        ``scripts/probe_rollinggo_shape.py`` is what would show it.
        """
        envelope = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self._next_id(),
            "method": MCP_TOOLS_CALL,
            "params": {"name": name, "arguments": arguments},
        }
        try:
            response = await self.http.request_raw(
                "POST",
                self.base_url,
                json_body=envelope,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Accept": MCP_ACCEPT,
                    "Content-Type": "application/json",
                },
            )
        except ProviderPermanentError as exc:
            raise self._classify(exc, operation=name) from exc
        return unwrap_result(parse_jsonrpc_payload(response.text), operation=name)

    def _classify(self, exc: ProviderPermanentError, *, operation: str) -> RollingGoError:
        """Turn an HTTP-level rejection into a typed, actionable error.

        The status is the only signal available. A 429 and a 5xx never reach here — the
        shared HTTP layer classifies those as transient and retries them — so this handles
        the permanent cases, of which only 401/403 is the user's to fix.
        """
        text = str(exc)
        if "status 401" in text or "status 403" in text:
            return RollingGoKeyError(f"RollingGo {operation}: the API key was rejected ({text})")
        return RollingGoError(f"RollingGo {operation} failed: {text}")


__all__ = [
    "DEFAULT_ROLLINGGO_BASE_URL",
    "JSONRPC_VERSION",
    "MCP_ACCEPT",
    "MCP_TOOLS_CALL",
    "ROLLINGGO_KEY_CREDENTIAL",
    "ROLLINGGO_PROVIDER_ID",
    "RollingGoError",
    "RollingGoKeyError",
    "RollingGoMcpClient",
    "RollingGoQuotaError",
    "parse_jsonrpc_payload",
    "unwrap_result",
]
