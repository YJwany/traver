from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from travel_api.api.dependencies import get_principal, require_read, require_write
from travel_api.domain.models import (
    AIChatRequest,
    AIChatResult,
    ConversationMessage,
    CredentialWriteRequest,
    ProviderHealth,
    Trip,
    TripPatchRequest,
    TripPreview,
    TripProfile,
    TripRevisionRequest,
    TripRevisionResult,
)
from travel_api.providers.free import attributions_as_dicts
from travel_api.providers.manifest import capability_matrix, catalog_as_dicts, manifest_for
from travel_api.services.auth import Principal
from travel_api.services.credentials import (
    SOURCE_ENV,
    SOURCE_STORED,
    CredentialVaultError,
    VaultLockedError,
    configured_credentials,
)
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

    # Reported because an operator who thinks credentials are encrypted when the
    # vault is actually locked would be relying on a protection that is not in
    # effect. This is a fact about the deployment, not a secret.
    credential_vault = getattr(request.app.state, "credential_vault", None)
    vault_enabled = bool(credential_vault is not None and credential_vault.enabled)

    payload = {
        "status": "ok" if database_ok else "degraded",
        "ready": database_ok,
        "service": "travel-api",
        "environment": request.app.state.settings.app_env,
        "persistence": "sqlalchemy",
        "database": "ok" if database_ok else "unavailable",
        "authentication": "api-key" if auth_enabled else "disabled",
        "credential_vault": "ready" if vault_enabled else "locked",
        "credential_key_source": getattr(request.app.state, "credential_key_source", "unknown"),
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


def _credentials_configured(owner_id: str, request: Request) -> dict[str, set[str]]:
    """Credential names in effect for this owner, from the vault and the environment.

    Delegates to the credential layer so the catalogue and the settings form share
    one definition; a second implementation here is how the two screens end up
    disagreeing about what is configured.
    """
    return configured_credentials(request.app.state.credential_resolver, owner_id)


@router.get("/providers/catalog", dependencies=[Depends(require_read)])
async def provider_catalog(request: Request, principal: Principal = Depends(get_principal)) -> dict:
    """What this deployment can do now, and what would unlock more.

    Traver ships no data and binds to no model, so "which sources are plugged in"
    is the whole story of what the program is capable of. This returns the
    catalogue, the capability matrix, and which credentials are still missing —
    never a credential value.

    ``planned`` sources (currently 高德/AMap) appear in the catalogue but are
    deliberately excluded from the matrix, because nothing implements them yet.
    """
    settings = request.app.state.settings
    registry = request.app.state.provider_registry
    registered = [getattr(provider, "name", "") for provider in registry.all()]
    configured = _credentials_configured(principal.owner_id, request)

    catalog = catalog_as_dicts(registered, configured_credentials=configured)
    matrix = capability_matrix(registered)
    return {
        "providers": catalog,
        "capabilities": matrix,
        "registered": sorted(name for name in registered if name),
        "uncovered_capabilities": [row["capability"] for row in matrix if not row["covered"]],
        "vault": _vault_status(request, settings),
    }


def _vault_status(request: Request, settings) -> dict:
    """How stored credentials are protected, and whether that is currently working.

    Reported rather than assumed: "your keys are encrypted" is only worth saying
    if the vault actually opened, and which key source is in use changes what the
    encryption does and does not defend against.
    """
    vault = request.app.state.credential_vault
    return {
        "enabled": bool(vault.enabled),
        "locked_reason": request.app.state.credential_key_error,
        "key_source": request.app.state.credential_key_source,
        "key_file": settings.credential_key_file,
        # On Windows os.chmod cannot set an ACL, so the key file is not actually
        # owner-only. Saying so is better than implying POSIX-grade protection.
        "owner_only_permissions": request.app.state.credential_permissions_restricted,
    }


@router.get("/settings/credentials", dependencies=[Depends(require_read)])
async def list_credentials(request: Request, principal: Principal = Depends(get_principal)) -> dict:
    """Which credentials are set for the caller, and where each came from.

    Names and origins only. A value is never returned by any route: the vault can
    be written to and cleared, but not read back out through HTTP.
    """
    stored = request.app.state.credential_vault.configured_by_provider(principal.owner_id)

    entries: dict[str, dict[str, str]] = {}
    for provider_id, keys in _credentials_configured(principal.owner_id, request).items():
        for key in keys:
            origin = SOURCE_STORED if key in stored.get(provider_id, set()) else SOURCE_ENV
            entries.setdefault(provider_id, {})[key] = origin
    return {
        "vault": _vault_status(request, request.app.state.settings),
        "credentials": entries,
    }


def _require_declared_credential(provider_id: str, credential_key: str):
    """Reject a provider/credential pair no manifest declares.

    Two reasons, both practical: a typo would otherwise write a record nothing
    ever reads, which looks exactly like "it saved but nothing changed"; and
    without this the vault becomes general-purpose key-value storage for whoever
    holds a write key.
    """
    manifest = manifest_for(provider_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"unknown provider: {provider_id}")
    declared = {item.key for item in manifest.credentials}
    if credential_key not in declared:
        raise HTTPException(
            status_code=404,
            detail=f"provider {provider_id} does not declare credential {credential_key}",
        )
    return manifest


@router.put("/settings/credentials/{provider_id}/{credential_key}")
async def set_credential(
    provider_id: str,
    credential_key: str,
    payload: CredentialWriteRequest,
    request: Request,
    principal: Principal = Depends(require_write),
) -> dict:
    """Store one of the caller's own credentials, encrypted at rest.

    Scoped to the caller: the record is keyed by their owner id, so one user
    saving a key cannot affect anyone else's requests.
    """
    manifest = _require_declared_credential(provider_id, credential_key)
    vault = request.app.state.credential_vault
    try:
        vault.store(principal.owner_id, provider_id, credential_key, payload.value)
    except VaultLockedError as exc:
        # 503, not 500: the deployment is fine, this capability is switched off or
        # has no usable master key, and the reason is actionable.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CredentialVaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "provider_id": provider_id,
        "credential_key": credential_key,
        # Deliberately echoes only a masked acknowledgement, never the value.
        "stored": True,
        "secret": next(item.secret for item in manifest.credentials if item.key == credential_key),
    }


@router.delete("/settings/credentials/{provider_id}/{credential_key}")
async def delete_credential(
    provider_id: str,
    credential_key: str,
    request: Request,
    principal: Principal = Depends(require_write),
) -> dict:
    """Remove one stored credential, falling back to the environment default."""
    _require_declared_credential(provider_id, credential_key)
    removed = request.app.state.credential_vault.remove(
        principal.owner_id, provider_id, credential_key
    )
    return {"provider_id": provider_id, "credential_key": credential_key, "removed": removed}


@router.post("/settings/model/test", dependencies=[Depends(require_write)])
async def test_model(request: Request, principal: Principal = Depends(get_principal)) -> dict:
    """Call the caller's configured model once and report what happened.

    A real request, not a shape check on the key: the point is to tell the user
    whether their model actually answers, and a test that only inspected the
    credential would pass for a revoked key.
    """
    probe = await request.app.state.model_runtime.probe(principal.owner_id)
    return {
        "ok": probe.ok,
        "model": probe.model,
        "latency_ms": probe.latency_ms,
        "reply": probe.reply,
        "error": probe.error,
    }


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
