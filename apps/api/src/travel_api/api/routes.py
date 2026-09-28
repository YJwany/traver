from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from travel_api.api.dependencies import get_principal, require_read, require_write
from travel_api.domain.ingestion import IngestionRequest
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
from travel_api.providers.manifest import (
    CAPABILITY_ORDER,
    capability_matrix,
    catalog_as_dicts,
    manifest_for,
    uncovered_aspects,
)
from travel_api.services.auth import Principal
from travel_api.services.credentials import (
    SOURCE_ENV,
    SOURCE_STORED,
    CredentialVaultError,
    VaultLockedError,
    configured_credentials,
)
from travel_api.services.planning_jobs import job_as_dict
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

    ``planned`` sources appear in the catalogue but are deliberately excluded from
    the matrix, because nothing implements them yet.

    ``uncovered_aspects`` is reported separately from ``uncovered_capabilities``.
    A missing capability is a visible absence; a missing *aspect* means a feature
    works for some journeys and silently not for others — routing without 公交/地铁
    being the case that matters, since a flat "routing: covered" would hide it.

    The registry is scoped to the caller, so the answer is what *this* owner can do.
    That matters now that a source can be per-owner: reporting the process-wide set
    would show 高德 as unavailable to an owner who has configured it, and, worse,
    would show it as available to one who has not.
    """
    settings = request.app.state.settings
    registry = request.app.state.provider_registry.for_owner(principal.owner_id)
    registered = [getattr(provider, "name", "") for provider in registry.all()]
    # Which *live* providers implement each capability, asked of the registry rather
    # than inferred from names. Without this, a source is credited with everything it
    # declares as soon as any one of its providers is registered — which is how a
    # source whose routing provider failed to register could still be reported as
    # covering 公交/地铁.
    live_providers = {
        provider: {getattr(item, "name", "") for item in registry.providers_for(provider)}
        for provider in CAPABILITY_ORDER
    }
    configured = _credentials_configured(principal.owner_id, request)

    catalog = catalog_as_dicts(registered, configured_credentials=configured)
    matrix = capability_matrix(registered, live_providers)
    return {
        "providers": catalog,
        "capabilities": matrix,
        "registered": sorted(name for name in registered if name),
        "uncovered_capabilities": [row["capability"] for row in matrix if not row["covered"]],
        "uncovered_aspects": uncovered_aspects(registered, live_providers),
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


# -- long-running planning -----------------------------------------------------
#
# Under ``/planning`` rather than ``/trips/jobs``: the latter would sit inside the
# ``/trips/{trip_id}`` space, where correct resolution depends on registration order — a
# route that breaks when someone reorders a file. A separate prefix removes the hazard
# rather than documenting it.


def planning_jobs(request: Request):
    """The job registry, or 503 when this deployment ran planning synchronously.

    A 503 rather than a fallback to a blocking plan: a client that asked for a job must
    not receive a request that hangs for 36 seconds instead.
    """
    registry = getattr(request.app.state, "planning_jobs", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="planning jobs are not enabled")
    return registry


@router.post("/planning/jobs", status_code=202, dependencies=[Depends(require_write)])
async def submit_planning_job(
    payload: TripProfile,
    request: Request,
    principal: Principal = Depends(get_principal),
    strategy: str = Query(default="balanced"),
    queue_trip: bool = Query(
        default=True,
        description="true 创建并保存行程；false 只生成预览供查看",
    ),
) -> dict:
    """Queue planning and return immediately.

    202 with the job body, not 201: nothing has been created yet at this point, and saying
    "created" would promise a trip that may yet fail.
    """
    trip_service_instance = trip_service(request)
    owner = _resolved_owner(request, principal)
    registry = planning_jobs(request)
    if not queue_trip:
        # A preview-only job: useful for trying a destination before committing to it,
        # which is exactly what the synchronous endpoint is slow at.
        async def runner(job):
            progress = registry.progress(job)
            await progress.step("resolve_sources", "按属主装配可用数据源")
            await progress.finish_step()
            # Built and discarded: the point of this mode is to see the plan without
            # committing to it, so nothing is persisted and the job reports no trip id.
            await trip_service_instance.preview(
                job.profile, job.budget_strategy, owner_id=owner, progress=progress
            )
            return None

        job = registry.submit(payload, runner, owner_id=owner, budget_strategy=strategy)
    else:
        job = trip_service_instance.queue(payload, strategy, owner_id=owner, jobs=registry)
    return job_as_dict(job)


@router.get("/planning/jobs", dependencies=[Depends(require_read)])
async def list_planning_jobs(
    request: Request, principal: Principal = Depends(get_principal)
) -> list[dict]:
    registry = planning_jobs(request)
    return [
        job_as_dict(job) for job in registry.list_for_owner(_resolved_owner(request, principal))
    ]


@router.get("/planning/jobs/{job_id}", dependencies=[Depends(require_read)])
async def get_planning_job(
    job_id: str, request: Request, principal: Principal = Depends(get_principal)
) -> dict:
    job = planning_jobs(request).get(job_id, owner_id=_resolved_owner(request, principal))
    if job is None:
        # 404 rather than 403 for another owner's job, same as trips and notes.
        raise HTTPException(status_code=404, detail="job not found")
    return job_as_dict(job)


@router.post("/planning/jobs/{job_id}/cancel", dependencies=[Depends(require_write)])
async def cancel_planning_job(
    job_id: str, request: Request, principal: Principal = Depends(get_principal)
) -> dict:
    """Ask a job to stop. Idempotent: cancelling a finished job is not an error."""
    job = planning_jobs(request).cancel(job_id, owner_id=_resolved_owner(request, principal))
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job_as_dict(job)


@router.get("/planning/jobs/{job_id}/events", dependencies=[Depends(require_read)])
async def stream_planning_job(
    job_id: str, request: Request, principal: Principal = Depends(get_principal)
):
    """Server-sent progress for a job.

    SSE rather than a WebSocket because the traffic is one-directional, survives proxies
    as plain HTTP, and reconnects on its own — all of which a traveller on hotel wifi
    benefits from. Each event carries the *whole* job rather than a delta, so a client that
    connects late, or reconnects, cannot end up with a progress display that disagrees with
    the server.

    The stream ends when the job reaches a terminal state, rather than staying open to be
    polled forever: a stream that never closes is a resource leak that only shows up in
    production.
    """
    import asyncio
    import json

    from fastapi.responses import StreamingResponse

    registry = planning_jobs(request)
    owner = _resolved_owner(request, principal)
    job = registry.get(job_id, owner_id=owner)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def events():
        queue = registry.subscribe(job_id)
        try:
            # The current state first, so a late subscriber sees where things already are.
            current = registry.get(job_id, owner_id=owner)
            if current is None:
                return
            yield f"data: {json.dumps(job_as_dict(current), ensure_ascii=False)}\n\n"
            if current.status.is_terminal:
                return
            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # A comment frame keeps intermediaries from closing an idle
                    # connection, and tells the client the server is still there.
                    yield ": keep-alive\n\n"
                    still = registry.get(job_id, owner_id=owner)
                    if still is None or still.status.is_terminal:
                        return
                    continue
                yield f"data: {json.dumps(job_as_dict(snapshot), ensure_ascii=False)}\n\n"
                if snapshot.status.is_terminal:
                    return
        finally:
            registry.unsubscribe(job_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


# -- user-supplied content -------------------------------------------------
#
# The platforms people actually read travel advice on — 小红书, 抖音, 美团, 携程,
# 同程 — publish no API. Scraping them would violate their terms and hold a
# traveller's session, so instead the traveller hands over text they can already
# see: a paste, or a bookmarklet that reads the page in *their* browser and posts
# the result here. No request is ever made to those hosts by this service.


def ingestion_service(request: Request):
    """The ingestion service, or a 503 when storage is not configured."""
    service = getattr(request.app.state, "ingestion_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="content ingestion is not configured")
    return service


@router.post("/ingestions", status_code=201, dependencies=[Depends(require_write)])
async def create_ingestion(
    payload: IngestionRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> dict:
    """Read a supplied note and store it, quoting the text behind every fact.

    Returns the extraction rather than just an id, because the interesting question is
    always "what did you get from it" — and showing the evidence is what lets the
    traveller disagree with a reading immediately rather than after it reaches a plan.
    """
    ingestion = ingestion_service(request).ingest(
        payload, owner_id=_resolved_owner(request, principal)
    )
    return ingestion.model_dump(mode="json")


@router.get("/ingestions", dependencies=[Depends(require_read)])
async def list_ingestions(
    request: Request,
    principal: Principal = Depends(get_principal),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    return [
        item.model_dump(mode="json")
        for item in ingestion_service(request).list(
            owner_id=_resolved_owner(request, principal), limit=limit
        )
    ]


@router.get("/ingestions/{ingestion_id}", dependencies=[Depends(require_read)])
async def get_ingestion(
    ingestion_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> dict:
    found = ingestion_service(request).get(
        ingestion_id, owner_id=_resolved_owner(request, principal)
    )
    if found is None:
        # Also the answer for a note owned by somebody else: confirming that an id
        # exists under another owner would leak its existence.
        raise HTTPException(status_code=404, detail="ingestion not found")
    return found.model_dump(mode="json")


@router.delete("/ingestions/{ingestion_id}", dependencies=[Depends(require_write)])
async def delete_ingestion(
    ingestion_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> dict:
    deleted = ingestion_service(request).delete(
        ingestion_id, owner_id=_resolved_owner(request, principal)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="ingestion not found")
    return {"deleted": True}


@router.get("/content/bookmarklet", dependencies=[Depends(require_read)])
async def ingestion_bookmarklet(request: Request) -> dict:
    """The bookmarklet source, generated rather than hard-coded in the Web app.

    Deliberately not under ``/ingestions/``: that prefix has a
    ``/ingestions/{ingestion_id}`` route, so a sibling literal path would be resolved
    only because of registration order — a route that works until someone reorders the
    file. A distinct prefix removes the hazard rather than relying on it.
    """
    return ingestion_service(request).bookmarklet(request)


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
