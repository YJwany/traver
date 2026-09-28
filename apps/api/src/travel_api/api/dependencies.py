"""FastAPI dependencies for authentication and owner scoping.

Kept separate from ``routes.py`` so the security rules live in one place that can
be read and tested on its own.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from travel_api.services.auth import (
    SCOPE_READ,
    SCOPE_WRITE,
    Principal,
)


def _bearer_token(request: Request) -> str | None:
    """Extract the credential from the Authorization header.

    Only ``Bearer`` is accepted. A token in a query string would end up in access
    logs and browser history, so it is deliberately not supported.
    """
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def get_principal(request: Request) -> Principal:
    """Return the authenticated caller, or raise 401.

    The middleware has already resolved the credential, so this reads its result
    instead of repeating the database lookup. ``WWW-Authenticate`` is set so a
    client can tell "credentials missing or bad" from "route not found", and so
    intermediaries behave correctly.
    """
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:  # pragma: no cover - defensive: app always wires it
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="authentication is not configured",
        )

    if not auth_service.enabled:
        # Single-user local mode: one synthetic owner, no credential required.
        return Principal(owner_id=auth_service.anonymous_owner)

    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return principal

    error = getattr(request.state, "auth_error", None) or "not authenticated"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=error,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_scope(scope: str):
    """Build a dependency asserting the principal holds ``scope``."""

    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.can(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key lacks required scope: {scope}",
            )
        return principal

    return dependency


#: Read access to trips.
require_read = require_scope(SCOPE_READ)
#: Mutation of trips.
require_write = require_scope(SCOPE_WRITE)


__all__ = [
    "get_principal",
    "require_read",
    "require_scope",
    "require_write",
]
