"""HTTP routes.

Conventions the whole API keeps:

* no pipeline logic in a route — every handler delegates to an application
  service, so the same operation behaves identically from the CLI or a worker;
* problem-details errors, so a client can branch on ``error_class`` rather than
  parsing prose;
* cursor pagination on collections;
* server-sent events for progress and token streaming only, never as the sole
  record of what happened.
"""

from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lab_assistant.application.acquisition import AcquisitionService
from lab_assistant.application.analytics import AnalyticsService
from lab_assistant.application.chat import ChatService
from lab_assistant.application.discovery import DiscoveryService
from lab_assistant.application.export import ExportService
from lab_assistant.application.reconciliation import IdentityService
from lab_assistant.application.refresh import RefreshService
from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.application.review import ReviewService
from lab_assistant.application.scopes import ScopeService
from lab_assistant.contracts.errors import NotFound
from lab_assistant.domain.claims import ReviewDecision
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.scope import ConnectorPlanEntry
from lab_assistant.domain.states import RelevanceState
from lab_assistant.domain.workspace import Capability
from lab_assistant_api.deps import (
    ApiContainer,
    CurrentActor,
    CurrentToken,
    get_container,
    requires,
)
from lab_assistant_api.security import mint_token

__all__ = ["router"]


def _client_signal(request: Request) -> str | None:
    """The caller's network origin, as far as it can be trusted.

    Behind the supported reverse proxy this is the first hop in
    ``X-Forwarded-For``; without a proxy it is the socket address. It is a *signal*,
    not an identity — a shared NAT means several people share a bucket, which is why
    the per-account bucket exists alongside it.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    client = request.client
    return client.host if client else None


router = APIRouter(prefix="/api/v1")

Container = Annotated[ApiContainer, Depends(get_container)]


# ── schemas ─────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str
    workspace: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: UUID
    workspaces: list[dict[str, str]]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    alias: str = "current"


class FeedbackRequest(BaseModel):
    message_id: UUID
    rating: int = Field(ge=1, le=5)
    comment: str | None = None
    flagged_handles: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    """Direct retrieval without generation.

    Behaviour is defined against the retrieval planner, not left implicit: the
    same policy filter, the same release resolution, the same citation handles.
    Only the generation step is skipped.
    """

    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    release_id: UUID | None = None
    alias: str = "current"


class ScopeRequest(BaseModel):
    name: str
    scientific_objective: str
    inclusion_criteria: list[str] = Field(default_factory=list)
    exclusion_criteria: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    intended_uses: list[str] = Field(default_factory=list)
    connectors: list[dict[str, Any]] = Field(default_factory=list)
    scope_id: UUID | None = None


class RelevanceRequest(BaseModel):
    relevance: RelevanceState
    reason_code: str
    note: str | None = None


class MergeRequest(BaseModel):
    keep_work_id: UUID
    merge_work_id: UUID
    reason_code: str
    note: str | None = None


class ReviewRequest(BaseModel):
    decision: ReviewDecision
    reason_code: str
    comment: str | None = None
    corrected_value: dict[str, Any] | None = None
    expected_revision_number: int | None = None
    reviewer_interpretation: bool = False


class ReleaseRequest(BaseModel):
    name: str
    semantic_version: str
    embedding_profile_ids: list[str] = Field(default_factory=lambda: ["hashing-v1"])
    parent_release_id: UUID | None = None
    include_claims: bool = True
    require_claim_review: bool = True


# ── auth ────────────────────────────────────────────────────────────────────
@router.post(
    "/auth/login",
    response_model=LoginResponse,
    dependencies=[requires(InstallationCapability.RECORD_INTERACTION)],
)
async def login(
    request: Request, container: Container, payload: LoginRequest = Body(...)
) -> LoginResponse:
    user = await container.accounts.authenticate(
        email=payload.email,
        password=payload.password,
        # Two buckets: one per account, one per origin. The origin is hashed before
        # it is stored, so the limiter does not create a log of who signed in from
        # where.
        client_signal=_client_signal(request),
    )
    async with container.context.uow_factory() as uow:
        workspaces = await uow.workspaces.list_for_user(user.id)
    selected = None
    if payload.workspace:
        selected = next((w.id for w in workspaces if w.slug == payload.workspace.lower()), None)
    elif workspaces:
        selected = workspaces[0].id
    token, _minted = mint_token(
        secret=container.settings.secret_key,
        user_id=user.id,
        auth_epoch=user.auth_epoch,
        ttl_minutes=container.settings.access_token_ttl_minutes,
        workspace_id=selected,
    )
    return LoginResponse(
        access_token=token,
        expires_in=container.settings.access_token_ttl_minutes * 60,
        user_id=user.id,
        workspaces=[{"id": str(w.id), "slug": w.slug, "name": w.display_name} for w in workspaces],
    )


@router.post(
    "/auth/logout",
    status_code=204,
    dependencies=[requires(InstallationCapability.RECORD_INTERACTION)],
)
async def logout(token: CurrentToken, container: Container) -> Response:
    """Revoke every token for this user.

    A logout that only forgets the cookie is not a revocation; bumping the epoch
    is, and it is the same mechanism an incident response uses.
    """
    await container.accounts.revoke_sessions(user_id=token.user_id, actor_id=token.user_id)
    return Response(status_code=204)


@router.get("/auth/me")
async def me(actor: CurrentActor, container: Container) -> dict[str, Any]:
    async with container.context.uow_factory() as uow:
        user = await uow.users.get(actor.user_id)
        workspace = await uow.workspaces.get(actor.workspace_id)
    if user is None or workspace is None:
        raise NotFound("account not found")
    return {
        "user_id": str(user.id),
        "email": user.email_display,
        "full_name": user.full_name,
        "workspace": {
            "id": str(workspace.id),
            "slug": workspace.slug,
            "name": workspace.display_name,
        },
        "role": actor.membership.role.value if actor.membership else None,
        "capabilities": sorted(
            c.value for c in (actor.membership.capabilities() if actor.membership else set())
        ),
    }


# ── ask ─────────────────────────────────────────────────────────────────────
@router.post("/chat/sessions", dependencies=[requires(InstallationCapability.RECORD_INTERACTION)])
async def create_session(actor: CurrentActor, container: Container) -> dict[str, Any]:
    session = await ChatService(container.context).create_session(actor)
    return _session_json(session)


@router.get("/chat/sessions")
async def list_sessions(
    actor: CurrentActor,
    container: Container,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    sessions = await ChatService(container.context).list_sessions(actor, limit=limit)
    return {"items": [_session_json(session) for session in sessions], "next_cursor": None}


@router.get("/chat/sessions/{session_id}")
async def get_session(
    session_id: UUID, actor: CurrentActor, container: Container
) -> dict[str, Any]:
    session, messages = await ChatService(container.context).get_session(
        actor, session_id=session_id
    )
    return {
        **_session_json(session),
        "messages": [_message_json(message) for message in messages],
    }


@router.patch(
    "/chat/sessions/{session_id}",
    dependencies=[requires(InstallationCapability.RECORD_INTERACTION)],
)
async def rename_session(
    session_id: UUID,
    actor: CurrentActor,
    container: Container,
    title: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
    session = await ChatService(container.context).rename_session(
        actor, session_id=session_id, title=title
    )
    return _session_json(session)


@router.delete(
    "/chat/sessions/{session_id}",
    status_code=204,
    dependencies=[requires(InstallationCapability.RECORD_INTERACTION)],
)
async def delete_session(session_id: UUID, actor: CurrentActor, container: Container) -> Response:
    await ChatService(container.context).delete_session(actor, session_id=session_id)
    return Response(status_code=204)


@router.post(
    "/chat/sessions/{session_id}/messages",
    dependencies=[requires(InstallationCapability.RECORD_INTERACTION)],
)
async def ask(
    session_id: UUID,
    actor: CurrentActor,
    container: Container,
    payload: AskRequest = Body(...),
) -> StreamingResponse:
    """Stream an answer as server-sent events: sources, tokens, then done."""
    chat = ChatService(container.context)

    async def event_stream():
        try:
            async for event in chat.ask_stream(
                actor,
                session_id=session_id,
                question=payload.question,
                alias=payload.alias,
                top_k=payload.top_k,
            ):
                yield _sse(event["type"], event["data"])
        except Exception as exc:  # the client needs a terminal event, not a hang
            from lab_assistant.contracts.errors import LabAssistantError

            detail = exc.detail if isinstance(exc, LabAssistantError) else "internal error"
            error_class = (
                exc.error_class.value if isinstance(exc, LabAssistantError) else "internal"
            )
            yield _sse("error", {"detail": detail, "error_class": error_class})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Reverse proxies must not buffer a token stream.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/chat/messages/{message_id}/citations/{handle}")
async def resolve_citation(
    message_id: UUID, handle: str, actor: CurrentActor, container: Container
) -> dict[str, Any]:
    return await ChatService(container.context).resolve_citation(
        actor, message_id=message_id, handle=handle
    )


@router.post(
    "/feedback",
    status_code=201,
    dependencies=[requires(InstallationCapability.RECORD_INTERACTION)],
)
async def add_feedback(
    actor: CurrentActor, container: Container, payload: FeedbackRequest = Body(...)
) -> dict[str, str]:
    feedback = await ChatService(container.context).add_feedback(
        actor,
        message_id=payload.message_id,
        rating=payload.rating,
        comment=payload.comment,
        flagged_handles=tuple(payload.flagged_handles),
    )
    return {"id": str(feedback.id)}


# ── search ──────────────────────────────────────────────────────────────────
@router.post("/search", dependencies=[requires(InstallationCapability.SERVE_CORPUS)])
async def search(
    actor: CurrentActor, container: Container, payload: SearchRequest = Body(...)
) -> dict[str, Any]:
    result = await RetrievalService(container.context).retrieve(
        actor,
        query=payload.query,
        release_id=payload.release_id,
        alias=payload.alias,
        top_k=payload.top_k,
    )
    return {
        "release_id": str(result.plan.release_id),
        "classification": result.plan.classification,
        "retrieval_profile_version": result.plan.profile_version,
        "candidates_considered": result.candidate_count,
        # How much is withheld is a steward's number: it is the size of the
        # material this member cannot open. Every caller still learns *that* their
        # view is filtered, which is what they need in order to ask for access.
        "withheld_by_policy": (
            result.denied_count if actor.has(Capability.MANAGE_RELEASE) else None
        ),
        "results_filtered": result.denied_count > 0,
        "results": [
            {
                "handle": block.handle,
                "text": block.text,
                "title": block.work_title,
                "year": block.work_year,
                "source_label": block.source_label,
                "locator": block.locator,
                "display_restricted": block.display_restricted,
                "score": result.scores.get(result.handle_index[block.handle]["chunk_key"], None),
                "work_id": result.handle_index[block.handle]["work_id"],
            }
            for block in result.evidence
        ],
        "claims": result.claims,
    }


# ── collections: scope and discovery ────────────────────────────────────────
@router.post(
    "/scopes",
    status_code=201,
    dependencies=[requires(InstallationCapability.CURATE_CORPUS)],
)
async def create_scope(
    actor: CurrentActor, container: Container, payload: ScopeRequest = Body(...)
) -> dict[str, Any]:
    scope = await ScopeService(container.context).create_version(
        actor,
        scope_id=payload.scope_id,
        name=payload.name,
        scientific_objective=payload.scientific_objective,
        connector_plan=[
            ConnectorPlanEntry(
                connector_id=entry["connector_id"],
                connector_version=entry.get("connector_version", "1.0.0"),
                query=entry["query"],
                required=bool(entry.get("required", True)),
            )
            for entry in payload.connectors
        ],
        inclusion_criteria=tuple(payload.inclusion_criteria),
        exclusion_criteria=tuple(payload.exclusion_criteria),
        concepts=tuple(payload.concepts),
        intended_uses=tuple(payload.intended_uses),
    )
    return {"id": str(scope.id), "version": scope.version, "checksum": scope.config_checksum}


@router.get("/scopes")
async def list_scopes(actor: CurrentActor, container: Container) -> dict[str, Any]:
    async with container.context.uow_factory() as uow:
        scopes = await uow.configs.list_scope_versions(actor.workspace_id)
    return {
        "items": [
            {
                "id": str(scope.id),
                "scope_id": str(scope.scope_id),
                "version": scope.version,
                "name": scope.name,
                "approval_state": scope.approval_state.value,
                "connectors": [entry.connector_id for entry in scope.connector_plan],
            }
            for scope in scopes
        ]
    }


@router.post(
    "/scopes/{scope_version_id}/gate",
    dependencies=[requires(InstallationCapability.CURATE_CORPUS)],
)
async def scope_gate(
    scope_version_id: UUID,
    actor: CurrentActor,
    container: Container,
    gold_seed_hits: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    report = await ScopeService(container.context).evaluate_gate(
        actor, scope_version_id=scope_version_id, gold_seed_hits=gold_seed_hits
    )
    return {
        "passed": report.passed,
        "blockers": list(report.blockers),
        "compiled_connectors": list(report.compiled_connectors),
        "gold_seed_recall": report.gold_seed_recall,
        "estimated_requests": report.estimated_requests,
    }


@router.post(
    "/scopes/{scope_version_id}/approve",
    dependencies=[requires(InstallationCapability.CURATE_CORPUS)],
)
async def approve_scope(
    scope_version_id: UUID,
    actor: CurrentActor,
    container: Container,
    gold_seed_hits: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    scope = await ScopeService(container.context).approve(
        actor, scope_version_id=scope_version_id, gold_seed_hits=gold_seed_hits
    )
    return {"id": str(scope.id), "approval_state": scope.approval_state.value}


@router.post(
    "/discovery-runs",
    status_code=202,
    dependencies=[requires(InstallationCapability.ACQUIRE_SOURCES)],
)
async def start_discovery(
    actor: CurrentActor,
    container: Container,
    scope_version_id: Annotated[UUID, Body(embed=True)],
) -> dict[str, Any]:
    run, tasks = await DiscoveryService(container.context).start_run(
        actor, scope_version_id=scope_version_id
    )
    submitted = await container.orchestrator.submit_requests(
        tasks, workspace_id=actor.workspace_id, actor_id=actor.user_id, run_id=run.id
    )
    return {
        "run_id": str(run.id),
        "status": run.status.value,
        "run_fingerprint": run.run_fingerprint,
        "tasks": [str(task.id) for task in submitted],
    }


@router.get("/discovery-runs")
async def list_discovery_runs(actor: CurrentActor, container: Container) -> dict[str, Any]:
    async with container.context.uow_factory() as uow:
        runs = await uow.observations.list_runs(actor.workspace_id)
    return {
        "items": [
            {
                "id": str(run.id),
                "status": run.status.value,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "frozen_at": run.frozen_at.isoformat() if run.frozen_at else None,
                "degraded_reason": run.degraded_reason,
                "connectors": [
                    {
                        "connector_id": state.connector_id,
                        "required": state.required,
                        "outcome": state.outcome.value if state.outcome else None,
                        "records_seen": state.records_seen,
                        "declared_total": state.declared_total,
                        "accepted_degraded": state.accepted_degraded_by is not None,
                    }
                    for state in run.connector_states
                ],
                "metrics": run.metrics,
            }
            for run in runs
        ]
    }


@router.post(
    "/discovery-runs/{run_id}/freeze",
    dependencies=[requires(InstallationCapability.ACQUIRE_SOURCES)],
)
async def freeze_discovery(
    run_id: UUID,
    actor: CurrentActor,
    container: Container,
    accept_degraded: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    run, tasks = await DiscoveryService(container.context).freeze_run(
        actor, run_id=run_id, force_accept_degraded=accept_degraded
    )
    submitted = await container.orchestrator.submit_requests(
        tasks, workspace_id=actor.workspace_id, actor_id=actor.user_id, run_id=run.id
    )
    return {
        "run_id": str(run.id),
        "status": run.status.value,
        "metrics": run.metrics,
        "tasks": [str(task.id) for task in submitted],
    }


# ── collections: identity decisions ─────────────────────────────────────────
@router.get("/candidates")
async def list_candidates(
    actor: CurrentActor,
    container: Container,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    from lab_assistant.application.reconciliation import canonical_metadata

    async with container.context.uow_factory() as uow:
        works = await uow.works.list_works(actor.workspace_id, limit=limit)
        items = []
        for work in works:
            assertions = await uow.observations.list_assertions(actor.workspace_id, work_id=work.id)
            metadata = canonical_metadata(list(assertions))
            identifiers = await uow.works.list_identifiers(actor.workspace_id, work.id)
            items.append(
                {
                    "work_id": str(work.id),
                    "title": metadata.get("title"),
                    "year": metadata.get("year"),
                    "journal": metadata.get("journal"),
                    "work_kind": work.work_kind.value,
                    "identity_status": work.identity_status.value,
                    "relevance": work.relevance.value,
                    "retraction_status": work.retraction_status,
                    "identifiers": [
                        {"scheme": i.scheme.value, "value": i.value_display, "status": i.status}
                        for i in identifiers
                    ],
                    # Every canonical field can explain which assertions support it.
                    "provenance": metadata.get("_provenance", {}),
                }
            )
    return {"items": items, "next_cursor": None}


@router.get("/candidates/conflicts")
async def list_conflicts(actor: CurrentActor, container: Container) -> dict[str, Any]:
    service = IdentityService(container.context)
    conflicts = await service.list_conflicts(actor)
    proposals = await service.list_duplicate_proposals(actor)
    return {
        "conflicts": [{"work_id": str(work.id)} for work in conflicts],
        "duplicate_proposals": [
            {
                "left_work_id": str(proposal.left_work_id),
                "right_work_id": str(proposal.right_work_id),
                "similarity": proposal.similarity,
                "features": proposal.features,
                "reason_code": proposal.reason_code,
            }
            for proposal in proposals
        ],
    }


@router.post(
    "/candidates/{work_id}/relevance",
    dependencies=[requires(InstallationCapability.CURATE_CORPUS)],
)
async def set_relevance(
    work_id: UUID,
    actor: CurrentActor,
    container: Container,
    payload: RelevanceRequest = Body(...),
) -> dict[str, Any]:
    work = await IdentityService(container.context).set_relevance(
        actor,
        work_id=work_id,
        relevance=payload.relevance,
        reason_code=payload.reason_code,
        note=payload.note,
    )
    return {"work_id": str(work.id), "relevance": work.relevance.value}


@router.post("/decisions/merge", dependencies=[requires(InstallationCapability.CURATE_CORPUS)])
async def merge_works(
    actor: CurrentActor, container: Container, payload: MergeRequest = Body(...)
) -> dict[str, Any]:
    decision = await IdentityService(container.context).merge(
        actor,
        keep_work_id=payload.keep_work_id,
        merge_work_id=payload.merge_work_id,
        reason_code=payload.reason_code,
        note=payload.note,
    )
    return {"decision_id": str(decision.id), "after": decision.after}


@router.post(
    "/decisions/link-versions",
    dependencies=[requires(InstallationCapability.CURATE_CORPUS)],
)
async def link_versions(
    actor: CurrentActor,
    container: Container,
    preprint_work_id: Annotated[UUID, Body()],
    version_of_record_work_id: Annotated[UUID, Body()],
) -> dict[str, Any]:
    relation = await IdentityService(container.context).link_versions(
        actor,
        preprint_work_id=preprint_work_id,
        version_of_record_work_id=version_of_record_work_id,
    )
    return {"relation_id": str(relation.id), "kind": relation.kind.value}


# ── acquisition ─────────────────────────────────────────────────────────────
@router.get("/acquisition-plans")
async def list_plans(
    actor: CurrentActor,
    container: Container,
    status: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    async with container.context.uow_factory() as uow:
        plans = await uow.assets.list_plans(actor.workspace_id, status=status)
        items = []
        for plan in plans:
            attempts = await uow.assets.list_attempts(actor.workspace_id, plan_id=plan.id)
            items.append(
                {
                    "plan_id": str(plan.id),
                    "work_id": str(plan.work_id),
                    "status": plan.status,
                    "routes": [route.value for route in plan.route_order],
                    "manifestation_ids": [str(i) for i in plan.manifestation_ids],
                    "attempts": [
                        {
                            "route": attempt.route.value,
                            "connector_id": attempt.connector_id,
                            "outcome": attempt.outcome.value if attempt.outcome else None,
                            "http_status": attempt.http_status,
                            "note": attempt.note,
                        }
                        for attempt in attempts
                    ],
                }
            )
    return {"items": items}


@router.post(
    "/assisted-acquisitions/{plan_id}",
    status_code=201,
    dependencies=[requires(InstallationCapability.ACQUIRE_SOURCES)],
)
async def assisted_upload(
    plan_id: UUID,
    actor: CurrentActor,
    container: Container,
    manifestation_id: Annotated[UUID, Form()],
    access_basis: Annotated[str, Form()],
    attestation: Annotated[str, Form()],
    upload: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Complete the assisted loop: same validations, same plan, work resumes."""
    content = await upload.read()
    asset = await AcquisitionService(container.context).register_assisted_upload(
        actor,
        plan_id=plan_id,
        manifestation_id=manifestation_id,
        content=content,
        filename=upload.filename,
        access_basis=access_basis,
        attestation=attestation,
        declared_media_type=upload.content_type,
    )
    from lab_assistant.contracts.stage import StageName
    from lab_assistant.domain.assets import MalwareScanStatus

    # Parsing is only queued for an asset that actually passed scanning. Queuing it
    # regardless produced a task that could never run — the parse worker refuses an
    # unscanned asset — and left the upload looking accepted. A blocked upload is
    # reported as blocked, with the reason.
    scan_required = container.settings.malware_scan_required_for_uploads
    scan_passed = asset.malware_scan_status is MalwareScanStatus.PASSED
    task_id: str | None = None
    if scan_passed or not scan_required:
        task = await container.orchestrator.submit(
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            stage=StageName.PARSING,
            inputs={"asset_id": str(asset.id)},
        )
        task_id = str(task.id)
    return {
        "asset_id": str(asset.id),
        "sha256": asset.sha256,
        "validation_status": asset.validation_status.value,
        "malware_scan_status": asset.malware_scan_status.value,
        "identity_verified": asset.identity_verified,
        "parse_task_id": task_id,
        "blocked_reason": (
            None
            if task_id
            else f"scanning did not pass ({asset.malware_scan_status.value}); "
            "a steward must resolve this before the document is parsed"
        ),
    }


# ── review ──────────────────────────────────────────────────────────────────
@router.get("/claims")
async def review_queue(
    actor: CurrentActor,
    container: Container,
    work_id: Annotated[UUID | None, Query()] = None,
    include_invalid: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items = await ReviewService(container.context).queue(
        actor, work_id=work_id, limit=limit, include_invalid=include_invalid
    )
    return {
        "items": [
            {
                "claim_id": str(item.claim.id),
                "revision_id": str(item.revision.id),
                "revision_number": item.revision.revision_number,
                "work_id": str(item.work_id),
                "predicate": item.claim.predicate,
                "subject": item.claim.subject_label,
                "value": item.revision.value,
                "validation_status": item.revision.validation_status,
                "validation_issues": list(item.revision.validation_issues),
                "abstention_reason": item.revision.abstention_reason.value,
                "review_status": item.revision.review_status.value,
                "proposal_source": item.revision.proposal_source.value,
                "evidence": list(item.evidence),
                "previous_revisions": [str(i) for i in item.previous_revisions],
                "conflicting_revisions": [str(i) for i in item.conflicting_revision_ids],
            }
            for item in items
        ]
    }


@router.post(
    "/reviews/{revision_id}",
    dependencies=[requires(InstallationCapability.CURATE_CORPUS)],
)
async def decide_review(
    revision_id: UUID,
    actor: CurrentActor,
    container: Container,
    payload: ReviewRequest = Body(...),
) -> dict[str, Any]:
    revision, review = await ReviewService(container.context).decide(
        actor,
        revision_id=revision_id,
        decision=payload.decision,
        reason_code=payload.reason_code,
        comment=payload.comment,
        corrected_value=payload.corrected_value,
        expected_revision_number=payload.expected_revision_number,
        reviewer_interpretation=payload.reviewer_interpretation,
    )
    return {
        "revision_id": str(revision.id),
        "revision_number": revision.revision_number,
        "review_status": revision.review_status.value,
        "review_id": str(review.id),
    }


@router.get("/reviews/correction-rate")
async def correction_rate(actor: CurrentActor, container: Container) -> dict[str, float]:
    return await ReviewService(container.context).correction_rate(actor)


# ── releases ────────────────────────────────────────────────────────────────
@router.post(
    "/corpus-releases",
    status_code=201,
    dependencies=[requires(InstallationCapability.PUBLISH_RELEASES)],
)
async def draft_release(
    actor: CurrentActor, container: Container, payload: ReleaseRequest = Body(...)
) -> dict[str, Any]:
    release, manifest, diff = await ReleaseService(container.context).draft(
        actor,
        ReleaseDraftRequest(
            name=payload.name,
            semantic_version=payload.semantic_version,
            embedding_profile_ids=tuple(payload.embedding_profile_ids),
            parent_release_id=payload.parent_release_id,
            include_claims=payload.include_claims,
            require_claim_review=payload.require_claim_review,
        ),
    )
    return {
        "release_id": str(release.id),
        "status": release.status.value,
        "manifest_sha256": release.manifest_sha256,
        "items": len(manifest.items),
        "diff": {
            "added": len(diff.added_work_ids),
            "changed": len(diff.changed_work_ids),
            "removed": len(diff.removed_work_ids),
            "unchanged": len(diff.unchanged_work_ids),
            "full_build": diff.is_full_build,
        },
    }


@router.get("/corpus-releases")
async def list_releases(actor: CurrentActor, container: Container) -> dict[str, Any]:
    async with container.context.uow_factory() as uow:
        releases = await uow.releases.list_releases(actor.workspace_id)
        aliases = await uow.releases.list_aliases(actor.workspace_id)
    return {
        "items": [
            {
                "id": str(release.id),
                "name": release.name,
                "semantic_version": release.semantic_version,
                "status": release.status.value,
                "manifest_sha256": release.manifest_sha256,
                "published_at": release.published_at.isoformat() if release.published_at else None,
                "gate_results": release.gate_results,
                "projection_ready_profiles": list(release.projection_ready_profiles),
                "parent_release_id": (
                    str(release.parent_release_id) if release.parent_release_id else None
                ),
            }
            for release in releases
        ],
        "aliases": [
            {
                "alias": alias.alias,
                "release_id": str(alias.release_id) if alias.release_id else None,
                "previous_release_id": (
                    str(alias.previous_release_id) if alias.previous_release_id else None
                ),
            }
            for alias in aliases
        ],
    }


@router.post(
    "/corpus-releases/{release_id}/validate",
    dependencies=[requires(InstallationCapability.PUBLISH_RELEASES)],
)
async def validate_release(
    release_id: UUID, actor: CurrentActor, container: Container
) -> dict[str, Any]:
    report = await ReleaseService(container.context).validate(actor, release_id=release_id)
    return {
        "passed": report.passed,
        "gates": report.gates,
        "failures": list(report.failures),
        "metrics": report.metrics,
    }


@router.post(
    "/corpus-releases/{release_id}/approve",
    status_code=202,
    dependencies=[requires(InstallationCapability.PUBLISH_RELEASES)],
)
async def approve_release(
    release_id: UUID,
    actor: CurrentActor,
    container: Container,
    evaluation_run_id: Annotated[UUID | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    release, tasks = await ReleaseService(container.context).approve(
        actor, release_id=release_id, evaluation_run_id=evaluation_run_id
    )
    submitted = await container.orchestrator.submit_requests(
        tasks, workspace_id=actor.workspace_id, actor_id=actor.user_id
    )
    return {
        "release_id": str(release.id),
        "status": release.status.value,
        "indexing_tasks": [str(task.id) for task in submitted],
    }


@router.post(
    "/corpus-releases/{release_id}/publish",
    dependencies=[requires(InstallationCapability.PUBLISH_RELEASES)],
)
async def publish_release(
    release_id: UUID,
    actor: CurrentActor,
    container: Container,
    alias: Annotated[str, Query()] = "current",
) -> dict[str, Any]:
    switched = await ReleaseService(container.context).publish(
        actor, release_id=release_id, alias=alias
    )
    return {
        "alias": switched.alias,
        "release_id": str(switched.release_id),
        "previous_release_id": (
            str(switched.previous_release_id) if switched.previous_release_id else None
        ),
    }


@router.post(
    "/corpus-releases/rollback",
    dependencies=[requires(InstallationCapability.PUBLISH_RELEASES)],
)
async def rollback_release(
    actor: CurrentActor,
    container: Container,
    alias: Annotated[str, Query()] = "current",
    reason: Annotated[str, Query()] = "operator_rollback",
) -> dict[str, Any]:
    switched = await ReleaseService(container.context).rollback(actor, alias=alias, reason=reason)
    return {"alias": switched.alias, "release_id": str(switched.release_id)}


@router.post(
    "/exports/{release_id}",
    dependencies=[requires(InstallationCapability.PRIVATE_EXPORT)],
)
async def build_export(
    release_id: UUID,
    actor: CurrentActor,
    container: Container,
    purpose: Annotated[str, Query()] = "internal_research",
) -> dict[str, Any]:
    from lab_assistant.application.export import validate_package

    package = await ExportService(container.context).build(
        actor, release_id=release_id, purpose=purpose
    )
    valid, problems = validate_package(package)
    return {
        "profile": package.manifest.profile,
        "valid": valid,
        "problems": list(problems),
        "row_counts": package.manifest.row_counts,
        "files": sorted(package.files),
    }


# ── analytics ───────────────────────────────────────────────────────────────
@router.get("/analytics/corpus")
async def corpus_stats(
    actor: CurrentActor,
    container: Container,
    release_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    stats = await AnalyticsService(container.context).corpus_stats(actor, release_id=release_id)
    return {
        "release_id": str(stats.release_id),
        "release_name": stats.release_name,
        "release_version": stats.release_version,
        "published_at": stats.published_at,
        "works": stats.works,
        "renditions": stats.renditions,
        "claims": stats.claims,
        "chunks": stats.chunks,
        "withheld_by_policy": stats.withheld,
    }


@router.get("/analytics/temporal")
async def temporal(
    actor: CurrentActor,
    container: Container,
    release_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    service = AnalyticsService(container.context)
    return {"items": await service.temporal(actor, release_id=release_id)}


@router.get("/analytics/journals")
async def journals(
    actor: CurrentActor,
    container: Container,
    release_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    service = AnalyticsService(container.context)
    return {"items": await service.journals(actor, release_id=release_id)}


@router.get("/analytics/subject-terms")
async def subject_terms(
    actor: CurrentActor,
    container: Container,
    release_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return {
        "items": await AnalyticsService(container.context).subject_terms(
            actor, release_id=release_id
        )
    }


@router.get("/analytics/sources")
async def sources(
    actor: CurrentActor,
    container: Container,
    release_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    service = AnalyticsService(container.context)
    return {"items": await service.sources(actor, release_id=release_id)}


# ── operations ──────────────────────────────────────────────────────────────
@router.get("/operations/tasks")
async def task_snapshot(actor: CurrentActor, container: Container) -> dict[str, Any]:
    actor.require(Capability.VIEW_OPERATIONS)
    return await container.orchestrator.queue_snapshot(workspace_id=actor.workspace_id)


@router.post(
    "/operations/tasks/{task_id}/retry",
    dependencies=[requires(InstallationCapability.RUN_WORKERS)],
)
async def retry_task(task_id: UUID, actor: CurrentActor, container: Container) -> dict[str, Any]:
    actor.require(Capability.VIEW_OPERATIONS)
    task = await container.orchestrator.retry_dead_letter(
        workspace_id=actor.workspace_id, task_id=task_id
    )
    return {"task_id": str(task.id), "state": task.state.value}


@router.get("/audit-events")
async def audit_events(
    actor: CurrentActor,
    container: Container,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    actor.require(Capability.VIEW_OPERATIONS)
    async with container.context.uow_factory() as uow:
        events = await uow.audit.list_events(actor.workspace_id, limit=limit)
    return {
        "items": [
            {
                "id": str(event.id),
                "occurred_at": event.occurred_at.isoformat(),
                "actor_type": event.actor_type.value,
                "action": event.action.value,
                "subject_type": event.subject_type,
                "subject_id": str(event.subject_id) if event.subject_id else None,
                "reason_code": event.reason_code,
                "metadata": event.metadata,
                "integrity_hash": event.integrity_hash,
            }
            for event in events
        ]
    }


@router.post(
    "/refresh/retractions",
    dependencies=[requires(InstallationCapability.PUBLISH_RELEASES)],
)
async def record_retraction(
    actor: CurrentActor,
    container: Container,
    work_id: Annotated[UUID, Body()],
    reason_code: Annotated[str, Body()] = "source_retraction_feed",
) -> dict[str, Any]:
    plan = await RefreshService(container.context).record_retraction(
        actor, work_id=work_id, reason_code=reason_code
    )
    return plan.as_dict()


# ── helpers ─────────────────────────────────────────────────────────────────
def _sse(event: str, data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _session_json(session) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "title": session.title,
        "pinned_release_id": (
            str(session.pinned_release_id) if session.pinned_release_id else None
        ),
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _message_json(message) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "role": message.role.value,
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "release_id": str(message.release_id) if message.release_id else None,
        "model": message.llm_model,
        "latency_ms": message.latency_ms,
        "citation_validation_passed": message.citation_validation_passed,
        "unsupported_handles": list(message.unsupported_citation_handles),
        "citations": [
            {
                "handle": citation.handle,
                "status": citation.status.value,
                "work_id": str(citation.work_id) if citation.work_id else None,
                "display_label": citation.display_label,
                "evidence_span_id": (
                    str(citation.evidence_span_id) if citation.evidence_span_id else None
                ),
            }
            for citation in message.citations
        ],
    }
