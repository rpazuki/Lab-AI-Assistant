"""Stages 3 and 4: rights planning, acquisition, and the assisted loop.

Nothing is fetched before a rights decision exists for the action. Every attempt
is recorded, including the ones that failed, because "we tried the open route and
it 404ed, then the repository rate-limited us" is operational knowledge the
predecessor threw away.

Assisted acquisition is a first-class loop: the same candidate, the same plan, the
same validations, and the downstream tasks resume — not a separate upload page
that leaves the queue behind.
"""

from __future__ import annotations

from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.application.pagination import collect_pages
from lab_assistant.application.rate_limit import RateLimiter, budget_key, host_of
from lab_assistant.contracts.connector import (
    AcquisitionRequest,
    ConnectorOperation,
    Identifier,
)
from lab_assistant.contracts.errors import (
    AcquisitionOutcome,
    ErrorClass,
    LabAssistantError,
    NotFound,
    PolicyDenied,
)
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.contracts.stage import (
    StageCommand,
    StageName,
    StageResult,
    StageStatus,
    TaskRequest,
)
from lab_assistant.domain.assets import (
    AcquisitionAttempt,
    AcquisitionPlan,
    AcquisitionRoute,
    Asset,
    MalwareScanStatus,
    Manifestation,
    ManifestationKind,
    RightsDecision,
    ValidationStatus,
    asset_parse_blockers,
)
from lab_assistant.domain.identity import normalise_identifier
from lab_assistant.domain.states import RightsState
from lab_assistant.domain.workspace import Capability
from lab_assistant.policy.rights import RightsRequest
from lab_assistant.provenance.fingerprints import task_fingerprint

__all__ = [
    "PERMITTED_MEDIA_TYPES",
    "AcquisitionHandler",
    "AcquisitionPlanningHandler",
    "AcquisitionService",
    "detect_media_type",
]

IMPLEMENTATION_VERSION = "acquisition/1"

#: Media types a parser profile exists for. Anything else is invalid content,
#: not a "we'll try anyway".
PERMITTED_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/xml",
        "text/xml",
        "text/plain",
        "text/csv",
        "text/tab-separated-values",
        "text/html",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/json",
    }
)

_MAGIC = (
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (b"<?xml", "application/xml"),
    (b"<!DOCTYPE html", "text/html"),
    (b"<html", "text/html"),
)


def detect_media_type(content: bytes, *, declared: str | None = None) -> str:
    """Detect by magic bytes, not by filename.

    A declared type is a hint from an untrusted source. The bytes decide, and a
    mismatch between them is itself worth recording.
    """
    head = content[:512]
    for signature, media_type in _MAGIC:
        if head.startswith(signature):
            if (
                media_type.endswith("spreadsheetml.sheet")
                and declared
                and declared.startswith("application/vnd.openxmlformats")
            ):
                return declared
            return media_type
    if declared in PERMITTED_MEDIA_TYPES:
        return declared
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    if b"," in head or b"\t" in head:
        return "text/tab-separated-values" if b"\t" in head else "text/csv"
    return "text/plain"


#: Ceiling when the deployment has not configured one. A paper is not this
#: large; something that is, is a mistake or an attack.
DEFAULT_MAX_ASSET_BYTES = 64 * 1024 * 1024

class AcquisitionService:
    """Rights decisions, plans, and the assisted upload path."""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def decide_rights(
        self,
        actor: ActorContext,
        *,
        subject_type: str,
        subject_id: UUID,
        licence_expression: str | None,
        access_basis: str | None,
        route: AcquisitionRoute | None,
        content_kind: str = "article",
        sensitivity: str = "public",
        user_attestation: str | None = None,
        requested_actions: frozenset[PolicyAction] | None = None,
        evidence_assertion_ids: tuple[UUID, ...] = (),
        decided_by_type: str = "automatic_rule",
        restricted_to_group_ids: tuple[UUID, ...] = (),
    ) -> RightsDecision:
        """Evaluate the rights policy and persist the decision."""
        context = self._context
        verdict = context.rights_policy.evaluate(
            RightsRequest(
                workspace_id=actor.workspace_id,
                subject_type=subject_type,
                subject_id=subject_id,
                licence_expression=licence_expression,
                access_basis=access_basis,
                route=route,
                content_kind=content_kind,
                sensitivity=sensitivity,
                user_attestation=user_attestation,
                requested_actions=requested_actions or frozenset(),
                evidence_assertion_ids=evidence_assertion_ids,
                now=context.now(),
            )
        )
        decision = RightsDecision(
            id=context.new_id(),
            workspace_id=actor.workspace_id,
            subject_type=subject_type,
            subject_id=subject_id,
            licence_expression=licence_expression,
            access_basis=access_basis,
            allowed_actions=verdict.allowed_actions,
            restrictions=verdict.restrictions,
            restricted_to_group_ids=restricted_to_group_ids,
            evidence_assertion_ids=evidence_assertion_ids,
            decided_by_type=decided_by_type,
            decided_by=actor.user_id if decided_by_type != "automatic_rule" else None,
            decided_at=context.now(),
            review_due_at=verdict.review_due_at,
            status=verdict.state,
            note=verdict.reason_code,
        )
        async with context.uow_factory() as uow:
            await uow.policies.add_rights_decision(decision)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=(
                        ActorType.USER if decided_by_type != "automatic_rule" else ActorType.SYSTEM
                    ),
                    actor_id=actor.user_id,
                    action=AuditAction.RIGHTS_DECIDED,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=verdict.reason_code,
                    metadata={
                        "state": verdict.state.value,
                        "allowed_actions": sorted(a.value for a in verdict.allowed_actions),
                        "review_actions": sorted(a.value for a in verdict.review_actions),
                        "matched_rules": list(verdict.matched_rule_ids),
                        "policy": (
                            f"{context.rights_policy.policy_id}"
                            f"@{context.rights_policy.semantic_version}"
                        ),
                    },
                )
            )
            await uow.commit()
        return decision

    async def register_assisted_upload(
        self,
        actor: ActorContext,
        *,
        plan_id: UUID,
        manifestation_id: UUID,
        content: bytes,
        filename: str | None,
        access_basis: str,
        attestation: str,
        declared_media_type: str | None = None,
    ) -> Asset:
        """The assisted loop's terminal step.

        Runs exactly the same integrity, malware, identity, and rights checks as
        automatic acquisition, attaches the asset to the original plan, and lets
        the caller resume downstream tasks.
        """
        actor.require(Capability.UPLOAD_ASSET)
        context = self._context
        async with context.uow_factory() as uow:
            plan = await uow.assets.get_plan(actor.workspace_id, plan_id)
            if plan is None:
                raise NotFound(f"acquisition plan {plan_id} not found")
            manifestation = await uow.assets.get_manifestation(actor.workspace_id, manifestation_id)
            if manifestation is None:
                raise NotFound(f"manifestation {manifestation_id} not found")
            identifiers = await uow.works.list_identifiers(actor.workspace_id, plan.work_id)

        decision = await self.decide_rights(
            actor,
            subject_type="manifestation",
            subject_id=manifestation_id,
            licence_expression=None,
            access_basis=access_basis,
            route=AcquisitionRoute.USER_ASSISTED,
            user_attestation=attestation,
            requested_actions=plan.requested_actions,
            decided_by_type="human_steward",
        )
        if not decision.permits(PolicyAction.PROCESS_LOCAL, now=context.now()):
            raise PolicyDenied(
                "the recorded access basis does not permit local processing of this upload"
            )

        asset = await self.register_asset(
            actor=actor,
            plan=plan,
            manifestation=manifestation,
            content=content,
            declared_media_type=declared_media_type,
            filename=filename,
            rights_decision=decision,
            route=AcquisitionRoute.USER_ASSISTED,
            connector_id="local-upload",
            expected_identifiers=tuple(
                Identifier(scheme=i.scheme, value=i.value_normalized)
                for i in identifiers
                if i.scheme.is_strong
            ),
            uploaded_by=actor.user_id,
        )
        async with context.uow_factory() as uow:
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ASSISTED_UPLOAD_ACCEPTED,
                    subject_type="asset",
                    subject_id=asset.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=access_basis,
                    metadata={
                        "plan_id": str(plan_id),
                        "sha256": asset.sha256,
                        "byte_length": asset.byte_length,
                        # The uploader's local path is deliberately absent.
                        "filename_safe": asset.original_filename_safe,
                    },
                )
            )
            await uow.commit()
        return asset

    async def register_asset(
        self,
        *,
        actor: ActorContext,
        plan: AcquisitionPlan,
        manifestation: Manifestation,
        content: bytes,
        declared_media_type: str | None,
        filename: str | None,
        rights_decision: RightsDecision,
        route: AcquisitionRoute,
        connector_id: str,
        expected_identifiers: tuple[Identifier, ...],
        uploaded_by: UUID | None = None,
        attempt: AcquisitionAttempt | None = None,
    ) -> Asset:
        """Quarantine, validate, scan, verify identity, then register."""
        context = self._context
        # The raise used to carry no error class, so it defaulted to INTERNAL —
        # which is retryable. A payload that is too large is too large every
        # time, and a worker would have spent its attempts rediscovering that.
        max_bytes = context.settings.object_store_max_object_bytes or DEFAULT_MAX_ASSET_BYTES
        if len(content) > max_bytes:
            raise LabAssistantError(
                f"content of {len(content)} bytes exceeds the {max_bytes} byte limit",
                context={"byte_length": len(content), "limit_bytes": max_bytes},
            ).with_class(ErrorClass.TOO_LARGE)

        media_type = detect_media_type(content, declared=declared_media_type)
        validation = ValidationStatus.PASSED
        if media_type not in PERMITTED_MEDIA_TYPES:
            validation = ValidationStatus.FAILED

        scan_status = MalwareScanStatus.NOT_SCANNED
        scan_detail: str | None = None
        needs_scan = (
            route is AcquisitionRoute.USER_ASSISTED
            and context.settings.malware_scan_required_for_uploads
        ) or (
            route is not AcquisitionRoute.USER_ASSISTED
            and context.settings.malware_scan_required_for_open_access
        )
        if context.malware_scanner is not None:
            result = await context.malware_scanner.scan(content, filename=filename)
            scan_status = MalwareScanStatus.PASSED if result.clean else MalwareScanStatus.FAILED
            scan_detail = result.detail
        elif needs_scan:
            scan_status = MalwareScanStatus.SCANNER_UNAVAILABLE
        else:
            # A skipped scan is a recorded exception, never an absent status.
            scan_status = MalwareScanStatus.SKIPPED_WITH_EXCEPTION
            scan_detail = "open-access route with scanning staged off (scale §3.2)"

        if scan_status is MalwareScanStatus.FAILED:
            validation = ValidationStatus.QUARANTINED

        identity_verified = _identity_matches(content, expected_identifiers)

        stored = await context.object_store.put(
            content,
            workspace_id=actor.workspace_id,
            object_class="asset",
            media_type=media_type,
        )

        asset = Asset(
            id=context.new_id(),
            workspace_id=actor.workspace_id,
            sha256=stored.sha256,
            byte_length=stored.byte_length,
            object_id=stored.id,
            media_type_detected=media_type,
            original_filename_safe=_safe_filename(filename),
            malware_scan_status=scan_status,
            validation_status=validation,
            created_from_attempt_id=attempt.id if attempt else None,
            manifestation_id=manifestation.id,
            work_id=plan.work_id,
            rights_decision_id=rights_decision.id,
            identity_verified=identity_verified,
            created_at=context.now(),
            metadata={
                "route": route.value,
                "connector_id": connector_id,
                "declared_media_type": declared_media_type,
                "uploaded_by": str(uploaded_by) if uploaded_by else None,
                "malware_scan_detail": scan_detail,
            },
        )
        async with context.uow_factory() as uow:
            # Idempotency is per *acquisition*, not per byte string. Re-running the
            # same route for the same work is a no-op; acquiring identical bytes for
            # a different work or manifestation is a new asset with its own rights
            # decision, attempt, and audit row. Returning the first work's asset
            # instead — as this did — attached the second acquisition to the wrong
            # work, and every citation into it pointed at the wrong paper.
            existing = await uow.assets.find_asset_by_digest(
                actor.workspace_id,
                stored.sha256,
                work_id=plan.work_id,
                manifestation_id=manifestation.id,
            )
            if existing is not None:
                return existing
            # The registry row before the row that references it: the asset's
            # object_id is a foreign key, and a database that enforces it
            # rejects the asset otherwise.
            await register_object(uow, stored)
            await uow.assets.add_asset(asset)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER if uploaded_by else ActorType.WORKER,
                    actor_id=uploaded_by or actor.user_id,
                    action=(
                        AuditAction.ASSET_QUARANTINED
                        if validation is ValidationStatus.QUARANTINED
                        else AuditAction.ASSET_REGISTERED
                    ),
                    subject_type="asset",
                    subject_id=asset.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    metadata={
                        "sha256": asset.sha256,
                        "media_type": media_type,
                        "malware_scan": scan_status.value,
                        "identity_verified": identity_verified,
                        "route": route.value,
                    },
                )
            )
            await uow.commit()
        return asset


def _safe_filename(filename: str | None) -> str | None:
    """Keep a display name, never a path.

    An absolute filesystem path in a shared column is a per-topology address, and
    the uploader's directory layout is nobody else's business.
    """
    if not filename:
        return None
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in base if ch.isalnum() or ch in "._- ")
    return cleaned[:120] or None


def _identity_matches(content: bytes, expected: tuple[Identifier, ...]) -> bool:
    """Confirm the bytes belong to the intended work.

    Looks for a strong identifier in the first part of the document. When no
    identifier is expected, identity cannot be confirmed and the asset stays
    unverified rather than being assumed correct.
    """
    if not expected:
        return False
    head = content[:200_000]
    try:
        text = head.decode("utf-8", errors="ignore").casefold()
    except Exception:  # pragma: no cover - decode with errors="ignore" cannot raise
        return False
    for identifier in expected:
        try:
            value = normalise_identifier(identifier.scheme, identifier.value).casefold()
        except ValueError:
            continue
        if value and value in text:
            return True
    return False


class AcquisitionPlanningHandler:
    """Stage 3: freeze a plan per included work, with rights decided first."""

    stage_name = StageName.ACQUISITION_PLANNING
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._service = AcquisitionService(context)

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        context = self._context
        workspace_id = command.workspace_id
        actor = ActorContext(
            user_id=command.actor_id,
            workspace_id=workspace_id,
            membership=None,
        )
        planned = 0
        assisted = 0
        metadata_only = 0
        next_tasks: list[TaskRequest] = []

        async with context.uow_factory() as uow:
            # Planning covers the whole corpus. The predecessor's corpus already
            # exceeded the fixed limit that used to be here, which would have left
            # works unplanned and indistinguishable from works with nothing to fetch.
            works = await collect_pages(
                lambda after, size: uow.works.list_works(
                    workspace_id, limit=size, after_id=after
                )
            )
            included = [w for w in works if w.relevance.is_included and w.is_active]
            assertion_index = {
                work.id: await uow.observations.list_assertions(workspace_id, work_id=work.id)
                for work in included
            }

        for work in included:
            assertions = assertion_index[work.id]
            licence = _first_value(assertions, "licence")
            full_text_location = _first_value(assertions, "full_text_location")
            kind = (
                ManifestationKind.PMC_XML
                if isinstance(full_text_location, str) and "pmc" in full_text_location.lower()
                else ManifestationKind.PUBLISHER_PDF
                if full_text_location
                else ManifestationKind.ABSTRACT_ONLY
            )

            async with context.uow_factory() as uow:
                existing = await uow.assets.list_manifestations(workspace_id, work.id)
                if existing:
                    manifestation = existing[0]
                else:
                    manifestation = Manifestation(
                        id=context.new_id(),
                        workspace_id=workspace_id,
                        work_id=work.id,
                        manifestation_kind=kind,
                        declared_mime=(
                            "application/xml"
                            if kind is ManifestationKind.PMC_XML
                            else "application/pdf"
                        ),
                        created_at=context.now(),
                    )
                    await uow.assets.add_manifestation(manifestation)
                    await uow.commit()

            requested_actions = frozenset(
                {
                    PolicyAction.STORE_CONTENT,
                    PolicyAction.PROCESS_LOCAL,
                    PolicyAction.CREATE_EMBEDDINGS_LOCAL,
                    PolicyAction.INDEX,
                    PolicyAction.QUERY,
                    PolicyAction.DISPLAY_EXCERPT,
                    PolicyAction.EXPORT_EVIDENCE,
                }
            )
            decision = await self._service.decide_rights(
                actor,
                subject_type="manifestation",
                subject_id=manifestation.id,
                licence_expression=licence if isinstance(licence, str) else "unknown",
                access_basis=str(full_text_location) if full_text_location else None,
                route=(
                    AcquisitionRoute.OPEN_ACCESS_API
                    if kind is ManifestationKind.PMC_XML
                    else AcquisitionRoute.OPEN_REPOSITORY
                ),
                content_kind=work.work_kind.value,
                requested_actions=requested_actions,
                evidence_assertion_ids=tuple(
                    a.id for a in assertions if a.predicate.value in ("licence", "access_status")
                ),
            )

            if decision.status is RightsState.PROHIBITED:
                metadata_only += 1
                continue

            route_order: list[AcquisitionRoute] = []
            if decision.permits(PolicyAction.STORE_CONTENT, now=context.now()):
                if kind is ManifestationKind.PMC_XML:
                    route_order.append(AcquisitionRoute.OPEN_ACCESS_API)
                route_order.append(AcquisitionRoute.OPEN_REPOSITORY)
            route_order.append(AcquisitionRoute.USER_ASSISTED)
            if not decision.permits(PolicyAction.STORE_CONTENT, now=context.now()):
                metadata_only += 1

            plan = AcquisitionPlan(
                id=context.new_id(),
                workspace_id=workspace_id,
                work_id=work.id,
                manifestation_ids=(manifestation.id,),
                route_order=tuple(route_order),
                requested_actions=requested_actions,
                allow_assisted=True,
                requested_by=command.actor_id,
                created_at=context.now(),
                status="planned",
            )
            async with context.uow_factory() as uow:
                await uow.assets.add_plan(plan)
                await uow.commit()
            planned += 1
            if route_order == [AcquisitionRoute.USER_ASSISTED]:
                assisted += 1
            next_tasks.append(
                TaskRequest(
                    stage=StageName.ACQUISITION,
                    inputs={"plan_id": str(plan.id)},
                )
            )

        return StageResult(
            stage=StageName.ACQUISITION_PLANNING,
            status=StageStatus.SUCCEEDED,
            metrics={
                "plans_created": planned,
                "assisted_only": assisted,
                "metadata_only": metadata_only,
                "included_works": len(included),
            },
            quality_gates={"every_plan_has_rights_decision": True},
            next_tasks=tuple(next_tasks),
        )


class AcquisitionHandler:
    """Stage 4: execute the route ladder for one plan."""

    stage_name = StageName.ACQUISITION
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._service = AcquisitionService(context)

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        context = self._context
        workspace_id = command.workspace_id
        plan_id = UUID(str(command.inputs["plan_id"]))
        actor = ActorContext(user_id=command.actor_id, workspace_id=workspace_id, membership=None)

        async with context.uow_factory() as uow:
            plan = await uow.assets.get_plan(workspace_id, plan_id)
            if plan is None:
                raise NotFound(f"acquisition plan {plan_id} not found")
            manifestations = [
                m
                for m in await uow.assets.list_manifestations(workspace_id, plan.work_id)
                if m.id in plan.manifestation_ids
            ]
            identifiers = await uow.works.list_identifiers(workspace_id, plan.work_id)
            existing_assets = await uow.assets.list_assets(workspace_id, work_id=plan.work_id)

        if existing_assets:
            asset = existing_assets[0]
            return StageResult(
                stage=StageName.ACQUISITION,
                status=StageStatus.SUCCEEDED_NOOP,
                metrics={"outcome": AcquisitionOutcome.ALREADY_AVAILABLE.value},
                next_tasks=(
                    TaskRequest(stage=StageName.PARSING, inputs={"asset_id": str(asset.id)}),
                ),
            )

        strong = tuple(
            Identifier(scheme=i.scheme, value=i.value_normalized)
            for i in identifiers
            if i.scheme.is_strong
        )
        registry = context.require_connectors()
        limiter = RateLimiter(context)
        attempts: list[AcquisitionAttempt] = []
        final_outcome = AcquisitionOutcome.FAILED_PERMANENT
        acquired_asset: Asset | None = None
        attempt_number = 0
        retry_after: int | None = None

        for manifestation in manifestations or [None]:
            for route in plan.route_order:
                if not route.is_automatic:
                    continue
                for connector_id in registry.list_ids(operation=ConnectorOperation.ACQUIRE):
                    attempt_number += 1
                    connector = registry.get(connector_id)
                    attempt = AcquisitionAttempt(
                        id=context.new_id(),
                        workspace_id=workspace_id,
                        acquisition_plan_id=plan.id,
                        manifestation_id=manifestation.id if manifestation else None,
                        connector_id=connector_id,
                        route=route,
                        attempt_number=attempt_number,
                        requested_at=context.now(),
                        worker_version=self.implementation_version,
                    )
                    acquisition_request = AcquisitionRequest(
                        identifiers=strong,
                        route=route.value,
                        preferred_formats=(
                            ("application/xml", "application/pdf")
                            if manifestation is None
                            or manifestation.manifestation_kind is ManifestationKind.PMC_XML
                            else ("application/pdf",)
                        ),
                        request_identity=context.settings.connector_request_identity,
                    )
                    try:
                        # Through the shared budget, exactly as discovery is. Fetching
                        # a document is an external source request; a limiter only
                        # discovery respects is not a limit the source experiences.
                        raw = await limiter.request(
                            lambda connector=connector, request=acquisition_request: (
                                connector.acquire(request)
                            ),
                            key=budget_key(
                                connector_id=connector_id,
                                host=host_of(getattr(connector, "base_url", None)),
                            ),
                            policy=connector.capabilities.rate_policy,
                        )
                    except LabAssistantError as exc:
                        attempt = attempt.model_copy(
                            update={
                                "completed_at": context.now(),
                                "outcome": _acquisition_outcome(exc.error_class),
                                "error_class": exc.error_class.value,
                                "note": exc.detail,
                                "retry_after_seconds": (exc.context or {}).get(
                                    "retry_after_seconds"
                                ),
                            }
                        )
                        attempts.append(attempt)
                        if attempt.outcome is AcquisitionOutcome.RATE_LIMITED_RETRYABLE:
                            retry_after = attempt.retry_after_seconds or 60
                        final_outcome = attempt.outcome or final_outcome
                        continue

                    decision = await self._service.decide_rights(
                        actor,
                        subject_type="manifestation",
                        subject_id=(
                            manifestation.id if manifestation else plan.manifestation_ids[0]
                        ),
                        licence_expression=raw.licence_expression or "unknown",
                        access_basis=raw.access_basis,
                        route=route,
                        requested_actions=plan.requested_actions,
                    )
                    if not decision.permits(PolicyAction.STORE_CONTENT, now=context.now()):
                        attempt = attempt.model_copy(
                            update={
                                "completed_at": context.now(),
                                "outcome": AcquisitionOutcome.LICENCE_NOT_PERMITTED,
                                "note": decision.note,
                            }
                        )
                        attempts.append(attempt)
                        final_outcome = AcquisitionOutcome.LICENCE_NOT_PERMITTED
                        continue

                    target_manifestation = manifestation or Manifestation(
                        id=plan.manifestation_ids[0],
                        workspace_id=workspace_id,
                        work_id=plan.work_id,
                    )
                    acquired_asset = await self._service.register_asset(
                        actor=actor,
                        plan=plan,
                        manifestation=target_manifestation,
                        content=raw.content,
                        declared_media_type=raw.declared_media_type,
                        filename=None,
                        rights_decision=decision,
                        route=route,
                        connector_id=connector_id,
                        expected_identifiers=strong,
                        attempt=attempt,
                    )
                    outcome = (
                        AcquisitionOutcome.IDENTITY_MISMATCH
                        if not acquired_asset.identity_verified and strong
                        else AcquisitionOutcome.ACQUIRED
                    )
                    attempt = attempt.model_copy(
                        update={
                            "completed_at": context.now(),
                            "outcome": outcome,
                            "http_status": raw.response.status,
                            "source_url_redacted": raw.source_url_redacted,
                            "asset_id": acquired_asset.id,
                        }
                    )
                    attempts.append(attempt)
                    final_outcome = outcome
                    break
                if acquired_asset is not None:
                    break
            if acquired_asset is not None:
                break

        if acquired_asset is None and AcquisitionRoute.USER_ASSISTED in plan.route_order:
            final_outcome = AcquisitionOutcome.ASSISTED_REQUIRED

        async with context.uow_factory() as uow:
            for attempt in attempts:
                await uow.assets.add_attempt(attempt)
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=workspace_id,
                        actor_type=ActorType.WORKER,
                        actor_id=command.actor_id,
                        action=AuditAction.ACQUISITION_ATTEMPTED,
                        subject_type="acquisition_attempt",
                        subject_id=attempt.id,
                        previous_hash=await uow.audit.latest_hash(workspace_id),
                        reason_code=attempt.outcome.value if attempt.outcome else None,
                        metadata={
                            "route": attempt.route.value,
                            "connector_id": attempt.connector_id,
                            "attempt_number": attempt.attempt_number,
                            "http_status": attempt.http_status,
                        },
                    )
                )
            plan = await uow.assets.get_plan(workspace_id, plan_id)
            assert plan is not None
            plan.status = (
                "acquired"
                if final_outcome.produced_asset
                else "assisted"
                if final_outcome is AcquisitionOutcome.ASSISTED_REQUIRED
                else "failed"
            )
            plan.version += 1
            await uow.assets.update_plan(plan)
            await uow.commit()

        if acquired_asset is not None and final_outcome is AcquisitionOutcome.ACQUIRED:
            return StageResult(
                stage=StageName.ACQUISITION,
                status=StageStatus.SUCCEEDED,
                metrics={"outcome": final_outcome.value, "attempts": len(attempts)},
                quality_gates={"attempt_provenance_recorded": bool(attempts)},
                next_tasks=(
                    TaskRequest(
                        stage=StageName.PARSING, inputs={"asset_id": str(acquired_asset.id)}
                    ),
                ),
            )

        status = (
            StageStatus.AWAITING_USER
            if final_outcome.needs_human
            else StageStatus.QUARANTINED
            if final_outcome is AcquisitionOutcome.MALWARE_QUARANTINED
            else StageStatus.FAILED
        )
        return StageResult(
            stage=StageName.ACQUISITION,
            status=status,
            metrics={"outcome": final_outcome.value, "attempts": len(attempts)},
            quality_gates={"attempt_provenance_recorded": bool(attempts)},
            human_action_required=(
                "assisted acquisition required" if final_outcome.needs_human else None
            ),
            # The error class decides retry behaviour, so it has to reflect the
            # outcome. Reporting a rate-limited attempt as `invalid_content` made it
            # permanent: the task failed for good over a limit that clears itself.
            error_class=(
                None
                if status is StageStatus.AWAITING_USER
                else _error_class_for_outcome(final_outcome)
            ),
            retry_after_seconds=retry_after,
        )


def _error_class_for_outcome(outcome: AcquisitionOutcome) -> ErrorClass:
    """Map a terminal acquisition outcome back onto the retry taxonomy."""
    mapping = {
        AcquisitionOutcome.RATE_LIMITED_RETRYABLE: ErrorClass.RATE_LIMITED,
        AcquisitionOutcome.SOURCE_UNAVAILABLE_RETRYABLE: ErrorClass.SOURCE_UNAVAILABLE,
        AcquisitionOutcome.NOT_FOUND: ErrorClass.NOT_FOUND,
        AcquisitionOutcome.LICENCE_NOT_PERMITTED: ErrorClass.POLICY_BLOCKED,
        AcquisitionOutcome.ROBOTS_OR_POLICY_BLOCKED: ErrorClass.ROBOTS_BLOCKED,
        AcquisitionOutcome.IDENTITY_MISMATCH: ErrorClass.IDENTITY_MISMATCH,
        AcquisitionOutcome.MALWARE_QUARANTINED: ErrorClass.MALWARE_DETECTED,
        AcquisitionOutcome.TOO_LARGE: ErrorClass.TOO_LARGE,
        AcquisitionOutcome.AUTHENTICATION_REQUIRED: ErrorClass.AUTHENTICATION_REQUIRED,
        AcquisitionOutcome.ENTITLEMENT_REQUIRED: ErrorClass.ENTITLEMENT_REQUIRED,
        AcquisitionOutcome.CANCELLED: ErrorClass.CANCELLED,
    }
    return mapping.get(outcome, ErrorClass.INVALID_CONTENT)


def _acquisition_outcome(error_class: ErrorClass) -> AcquisitionOutcome:
    mapping = {
        ErrorClass.RATE_LIMITED: AcquisitionOutcome.RATE_LIMITED_RETRYABLE,
        ErrorClass.SOURCE_UNAVAILABLE: AcquisitionOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
        ErrorClass.TIMEOUT: AcquisitionOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
        ErrorClass.AUTHENTICATION_REQUIRED: AcquisitionOutcome.AUTHENTICATION_REQUIRED,
        ErrorClass.ENTITLEMENT_REQUIRED: AcquisitionOutcome.ENTITLEMENT_REQUIRED,
        ErrorClass.POLICY_BLOCKED: AcquisitionOutcome.ROBOTS_OR_POLICY_BLOCKED,
        ErrorClass.ROBOTS_BLOCKED: AcquisitionOutcome.ROBOTS_OR_POLICY_BLOCKED,
        ErrorClass.NOT_FOUND: AcquisitionOutcome.NOT_FOUND,
        ErrorClass.TOO_LARGE: AcquisitionOutcome.TOO_LARGE,
        ErrorClass.MALWARE_DETECTED: AcquisitionOutcome.MALWARE_QUARANTINED,
        ErrorClass.INVALID_CONTENT: AcquisitionOutcome.INVALID_CONTENT,
        ErrorClass.IDENTITY_MISMATCH: AcquisitionOutcome.IDENTITY_MISMATCH,
        ErrorClass.CANCELLED: AcquisitionOutcome.CANCELLED,
    }
    return mapping.get(error_class, AcquisitionOutcome.FAILED_PERMANENT)


def _first_value(assertions: list, predicate_value: str):
    for assertion in assertions:
        if assertion.predicate.value == predicate_value:
            return assertion.normalised_value_json or assertion.value_json
    return None


def parse_readiness(
    *, asset: Asset, rights: RightsDecision | None, now, require_scan: bool
) -> tuple[str, ...]:
    """Public wrapper so the parsing stage shares one readiness definition."""
    return asset_parse_blockers(
        asset,
        rights=rights,
        permitted_media_types=PERMITTED_MEDIA_TYPES,
        now=now,
        require_malware_scan=require_scan,
    )
