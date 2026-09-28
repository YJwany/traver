"""Inbound rate limiting for the API itself.

Distinct from ``providers/free/http.py``: that limiter is *outbound* and exists to
honour upstream usage policies. This one protects Traver from its own callers —
most importantly ``/ai/chat``, which can spend model tokens on every request.

Implemented as a fixed-window counter. A sliding window would be marginally
smoother at the boundary, but this keeps one small dict, no background task and no
clock dependence beyond monotonic time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass
class _Window:
    started: float
    count: int = 0


@dataclass
class RateLimiter:
    """Per-client fixed-window counter.

    The key is the authenticated owner when available, otherwise the client IP.
    Keying on the owner is what makes the limit meaningful behind a proxy, where
    many users can share one address.
    """

    requests: int
    window_seconds: float
    _windows: dict[str, _Window] = field(default_factory=dict)

    def check(self, key: str, *, now: float | None = None) -> float | None:
        """Record a request.

        Returns ``None`` when the request is allowed, or the number of seconds to
        wait when the window is exhausted.

        Deliberately returns a value instead of raising ``HTTPException``: this is
        called from HTTP middleware, and an exception raised there bypasses
        FastAPI's exception handlers, so a raised 429 would surface to the client
        as a 500. The middleware turns the returned delay into a real response.
        """
        if self.requests <= 0:
            return None
        moment = time.monotonic() if now is None else now
        window = self._windows.get(key)
        if window is None or moment - window.started >= self.window_seconds:
            self._windows[key] = _Window(started=moment, count=1)
            return None
        if window.count >= self.requests:
            return max(0.0, self.window_seconds - (moment - window.started))
        window.count += 1
        return None

    def reset(self) -> None:
        self._windows.clear()


def client_key(request: Request) -> str:
    """Identity used for rate limiting.

    Prefers the authenticated owner so one NAT'd office cannot exhaust a shared
    budget, and falls back to the socket address. ``X-Forwarded-For`` is not
    trusted: it is caller-controlled unless a proxy is known to rewrite it, and
    trusting it would let a client rotate keys to evade the limit.
    """
    principal = getattr(request.state, "principal", None)
    owner = getattr(principal, "owner_id", None)
    if owner:
        return f"owner:{owner}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def too_many_requests(retry_after: float) -> JSONResponse:
    """Build the 429 response for an exhausted window.

    Constructed as a real response in the middleware rather than raised, because
    ``HTTPException`` raised inside HTTP middleware is not handled by FastAPI's
    exception handlers and would become a 500.
    """
    seconds = str(int(retry_after) + 1)
    return JSONResponse(
        status_code=429,
        content={"detail": "rate limit exceeded"},
        headers={"Retry-After": seconds},
    )
