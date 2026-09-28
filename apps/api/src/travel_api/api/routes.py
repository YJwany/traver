from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from travel_api.api.dependencies import get_principal, require_read, require_write
from travel_api.domain.models import (
    AIChatRequest,
    AIChatResult,
    ConversationMessage,
    ProviderHealth,
    Trip,
    TripPatchRequest,
    TripPreview,
    TripProfile,
    TripRevisionRequest,
    TripRevisionResult,
)
from travel_api.providers.free import attributions_as_dicts
from travel_api.services.auth import Principal
from travel_api.services.trip_service import (
    ConcurrentTripUpdateError,
    TripNotFoundError,
    TripRevisionError,
    VersionConflictError,
)

router = APIRouter(prefix="/api/v1")


def trip_service(request: Request):
    return request.app.state.trip_service


def _resolved_owner(request: Request, principal: Principal) -> str | None:
    """Owner filter, or ``None`` when the API is running without auth.

    Returning ``None`` means "no filter" and is reachable only when
    ``auth_enabled`` is false — the single-user local-development mode, which
    ``/api/v1/health`` advertises. With auth on this is always a real owner id,
    so no query can accidentally read across tenants.
    """
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None or not auth_service.enabled:
        return None
    return principal.owner_id


@router.get("/health")
async def health(request: Request) -> dict:
    """Liveness plus a real readiness signal.

    This used to return ``{"status": "ok"}`` unconditionally, without touching the
    database, so a probe could not distinguish "serving" from "cannot reach its
    own storage". ``ready`` now reflects an actual round-trip, and the auth mode
    is reported so an unauthenticated deployment is never mistaken for a secured
    one.
    """
    database = getattr(request.app.state, "database", None)
    database_ok = False
    database_error = None
    if database is not None:
        try:
            database.ping()
            database_ok = True
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            database_error = f"{type(exc).__name__}: {exc}"

    auth_service = getattr(request.app.state, "auth_service", None)
    auth_enabled = bool(auth_service is not None and auth_service.enabled)

    payload = {
        "status": "ok" if database_ok else "degraded",
        "ready": database_ok,
        "service": "travel-api",
        "environment": request.app.state.settings.app_env,
        "persistence": "sqlalchemy",
        "database": "ok" if database_ok else "unavailable",
        "authentication": "api-key" if auth_enabled else "disabled",
    }
    if not database_ok:
        payload["database_error"] = database_error
    return payload


@router.get("/providers/health", response_model=list[ProviderHealth])
async def provider_health(request: Request) -> list[ProviderHealth]:
    return await request.app.state.provider_registry.health()


@router.get("/providers/attributions")
async def provider_attributions() -> list[dict[str, str]]:
    """Licence-required credits for the open-data sources in use.

    ODbL and CC BY 4.0 both require visible attribution wherever the data is
    displayed, so the Web client renders this list.
    """
    return attributions_as_dicts()


@router.get("/tools")
async def tools(request: Request) -> list[dict]:
    return request.app.state.tool_registry.definitions()


@router.post("/tools/{tool_name}")
async def call_tool(
    tool_name: str,
    payload: dict,
    request: Request,
    confirm_write: bool = False,
) -> dict:
    """Invoke a registered tool.

    Write tools are gated: this route used to pass ``allow_write=True``
    unconditionally, which meant the registry's ``PermissionError`` guard could
    never fire over HTTP and any caller could mutate an arbitrary trip. The
    caller must now opt in explicitly with ``?confirm_write=true``, and a write
    without it is refused rather than silently performed.
    """
    try:
        definition = request.app.state.tool_registry.get(tool_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if definition.kind == "write" and not confirm_write:
        raise HTTPException(
            status_code=403,
            detail=(
                f"tool {tool_name!r} mutates state and requires explicit confirmation; "
                "retry with ?confirm_write=true"
            ),
        )

    try:
        return await request.app.state.tool_registry.call(
            tool_name, payload, allow_write=confirm_write
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/trips/preview", response_model=TripPreview, dependencies=[Depends(require_write)])
async def trip_preview(profile: TripProfile, request: Request) -> TripPreview:
    return await trip_service(request).preview(profile)


@router.post("/trips", response_model=Trip, dependencies=[Depends(require_write)])
async def create_trip(
    profile: TripProfile,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Trip:
    return await trip_service(request).create(profile, owner_id=_resolved_owner(request, principal))


@router.get("/trips", response_model=list[Trip], dependencies=[Depends(require_read)])
async def list_trips(
    request: Request,
    principal: Principal = Depends(get_principal),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Trip]:
    return trip_service(request).list(limit, offset, owner_id=_resolved_owner(request, principal))


@router.get("/trips/{trip_id}", response_model=Trip, dependencies=[Depends(require_read)])
async def get_trip(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Trip:
    try:
        return trip_service(request).get(trip_id, owner_id=_resolved_owner(request, principal))
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc


@router.patch("/trips/{trip_id}", response_model=Trip, dependencies=[Depends(require_write)])
async def patch_trip(
    trip_id: UUID,
    patch: TripPatchRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Trip:
    owner = _resolved_owner(request, principal)
    try:
        return await trip_service(request).patch(
            trip_id, patch, owner_id=owner, expected_version=patch.expected_version
        )
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    except VersionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrentTripUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/trips/{trip_id}/refresh", response_model=Trip, dependencies=[Depends(require_write)])
async def refresh_trip(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Trip:
    try:
        return await trip_service(request).refresh(
            trip_id, owner_id=_resolved_owner(request, principal)
        )
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    except ConcurrentTripUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/trips/{trip_id}/recalculate", response_model=Trip, dependencies=[Depends(require_write)]
)
async def recalculate_trip(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Trip:
    try:
        return await trip_service(request).refresh(
            trip_id, owner_id=_resolved_owner(request, principal)
        )
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    except ConcurrentTripUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/trips/{trip_id}/revision",
    response_model=TripRevisionResult,
    dependencies=[Depends(require_write)],
)
async def revise_trip(
    trip_id: UUID,
    revision: TripRevisionRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> TripRevisionResult:
    try:
        return await trip_service(request).revise(
            trip_id, revision, owner_id=_resolved_owner(request, principal)
        )
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    except TripRevisionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConcurrentTripUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/trips/{trip_id}/budget", dependencies=[Depends(require_read)])
async def get_budget(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> dict:
    try:
        trip = trip_service(request).get(trip_id, owner_id=_resolved_owner(request, principal))
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    return trip.budget.model_dump(mode="json") if trip.budget else {}


@router.get("/trips/{trip_id}/itinerary", dependencies=[Depends(require_read)])
async def get_itinerary(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> dict:
    try:
        trip = trip_service(request).get(trip_id, owner_id=_resolved_owner(request, principal))
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    return trip.itinerary.model_dump(mode="json")


@router.get("/trips/{trip_id}/sources", dependencies=[Depends(require_read)])
async def get_sources(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> list[dict]:
    try:
        trip = trip_service(request).get(trip_id, owner_id=_resolved_owner(request, principal))
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    return [
        item.model_dump(mode="json")
        for item in (trip.preview.source_references if trip.preview else [])
    ]


@router.get("/trips/{trip_id}/snapshots", dependencies=[Depends(require_read)])
async def get_snapshots(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> list[dict]:
    try:
        return trip_service(request).snapshots(
            trip_id, owner_id=_resolved_owner(request, principal)
        )
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc


@router.get("/trips/{trip_id}/changes", dependencies=[Depends(require_read)])
async def get_changes(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> list[dict]:
    try:
        trip = trip_service(request).get(trip_id, owner_id=_resolved_owner(request, principal))
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    return [item.model_dump(mode="json") for item in trip.change_history]


@router.post("/ai/chat", response_model=AIChatResult, dependencies=[Depends(require_write)])
async def ai_chat(
    payload: AIChatRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> AIChatResult:
    try:
        return await request.app.state.travel_ai_service.chat(
            payload, owner_id=_resolved_owner(request, principal)
        )
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
    except TripRevisionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConcurrentTripUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/trips/{trip_id}/conversation",
    response_model=list[ConversationMessage],
    dependencies=[Depends(require_read)],
)
async def get_conversation(
    trip_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> list[ConversationMessage]:
    try:
        return request.app.state.travel_ai_service.conversation(
            trip_id, owner_id=_resolved_owner(request, principal)
        )
    except TripNotFoundError as exc:
        raise HTTPException(status_code=404, detail="trip not found") from exc
