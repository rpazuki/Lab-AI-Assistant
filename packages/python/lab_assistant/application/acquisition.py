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

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.application.pagination import collect_pages
from lab_assistant.application.rate_limit import RateLimiter, budget_key, host_of
from lab_assistant.contracts.connector import (
    AcquisitionRequest,
    ConnectorOperation,
    Identifier,
    IdentifierScheme,
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
    is_abstract,
)
from lab_assistant.domain.identity import normalise_identifier
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.states import RightsState
from lab_assistant.domain.workspace import Capability
from lab_assistant.policy.rights import RightsRequest
from lab_assistant.provenance.fingerprints import task_fingerprint

__all__ = [
    "PERMITTED_MEDIA_TYPES",
    "PLAN_ACTIONS",
    "AcquisitionHandler",
    "AcquisitionPlanningHandler",
    "AcquisitionService",
    "detect_media_type",
    "manifestation_kind",
    "route_ladder",
]

#: ``acquisition/3`` paces each publisher site. Answers the unpaced fetcher got
#: (rate limits, bot walls read as sign-in pages) do not count as answered, so the
#: next press of Fetch included papers asks those sites again, slowly.
IMPLEMENTATION_VERSION = "acquisition/3"

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
        self._context.require_installation(InstallationCapability.ACQUIRE_SOURCES)
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
        self._context.require_installation(InstallationCapability.ACQUIRE_SOURCES)
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
            summaries = await uow.works.get_summaries(actor.workspace_id, [plan.work_id])
        summary = summaries.get(plan.work_id)

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
            expected_title=summary.title if summary else None,
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
            # The upload closes the plan: it was waiting for a person, or only its
            # abstract stood in for it. Left as it was, an uploaded paper still
            # read as waiting, and a partial one as abstract-only.
            if asset.validation_status is ValidationStatus.PASSED:
                stored_plan = await uow.assets.get_plan(actor.workspace_id, plan_id)
                if stored_plan is not None and stored_plan.status != "acquired":
                    stored_plan.status = "acquired"
                    stored_plan.version += 1
                    await uow.assets.update_plan(stored_plan)
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
        expected_title: str | None = None,
    ) -> Asset:
        """Quarantine, validate, scan, verify identity, then register."""
        self._context.require_installation(InstallationCapability.ACQUIRE_SOURCES)
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
            # A scanner that could not run found nothing: the file is unscanned,
            # not infected. Recording it as failed quarantined it for good.
            scan_status = (
                MalwareScanStatus.PASSED
                if result.clean
                else MalwareScanStatus.SCANNER_UNAVAILABLE
                if not getattr(result, "available", True)
                else MalwareScanStatus.FAILED
            )
            scan_detail = result.detail
        elif needs_scan:
            scan_status = MalwareScanStatus.SCANNER_UNAVAILABLE
        else:
            # A skipped scan is a recorded exception, never an absent status.
            scan_status = MalwareScanStatus.SKIPPED_WITH_EXCEPTION
            scan_detail = "open-access route with scanning staged off (scale §3.2)"

        if scan_status is MalwareScanStatus.FAILED:
            validation = ValidationStatus.QUARANTINED

        evidence = identity_evidence(content, expected_identifiers, expected_title)
        identity_verified = evidence.verified

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
                **evidence.as_metadata(),
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
            # The rows an asset references before the asset: its object_id and
            # created_from_attempt_id are foreign keys, and a database that
            # enforces them rejects the asset otherwise. The attempt was only
            # saved after the fetch finished, so every file a source returned was
            # refused, and the task died, at the moment it succeeded.
            await register_object(uow, stored)
            if attempt is not None:
                await uow.assets.add_attempt(attempt)
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


#: DOI prefixes of preprint servers: bioRxiv and medRxiv, Research Square,
#: Preprints.org, arXiv, OSF and its family, SSRN, ChemRxiv, Authorea.
PREPRINT_DOI_PREFIXES = (
    "10.1101/",
    "10.21203/",
    "10.20944/",
    "10.48550/",
    "10.31219/",
    "10.31234/",
    "10.31235/",
    "10.2139/",
    "10.26434/",
    "10.22541/",
)
#: The identity rule an asset was checked by, recorded with it, so a file checked
#: by an older rule is checked again and one checked by this rule is not.
IDENTITY_RULE = "identifier-or-title/1"
#: A title shorter than this matches too much of any page to identify a paper.
MIN_TITLE_WORDS = 5
MIN_TITLE_CHARACTERS = 30


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    """Why a fetched file is taken to be the intended paper, or that it is not.

    ``basis`` is ``identifier`` (its DOI, PMID or PMCID is in the file) or
    ``title`` (its title is, word for word). ``version`` is ``preprint`` when
    the file carries a preprint server's DOI or an arXiv id: Unpaywall offers a
    preprint as the open copy of the published paper, and that preprint has its
    own DOI, so only its title says it is the same work.
    """

    verified: bool
    basis: str | None = None
    version: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "identity_basis": self.basis,
            "version": self.version,
            "identity_rule": IDENTITY_RULE,
        }


def _plain(text: str) -> str:
    """Case-folded words only: a title that wraps across lines still matches."""
    import re

    joined = re.sub(r"-\s*\n\s*", "", text)  # a word hyphenated across a line break
    return " ".join(re.findall(r"\w+", joined.casefold()))


def identity_evidence(
    content: bytes, expected: tuple[Identifier, ...], title: str | None = None
) -> IdentityEvidence:
    """Confirm the bytes belong to the intended work, and say how.

    Looks for a strong identifier, then the work's title, in the first part of
    the document. When neither is there, identity cannot be confirmed and the
    asset stays unverified rather than being assumed correct.
    """
    values = []
    for identifier in expected:
        try:
            value = normalise_identifier(identifier.scheme, identifier.value).casefold()
        except ValueError:
            continue
        if value:
            values.append(value)
    text = content[:200_000].decode("utf-8", errors="ignore")
    # A PDF keeps its text compressed, so its bytes almost never show the DOI;
    # its metadata and first pages, read as text, usually do.
    if content.startswith(b"%PDF"):
        text = f"{text}\n{_pdf_head_text(content)}"
    folded = text.casefold()
    if values and any(value in folded for value in values):
        return IdentityEvidence(verified=True, basis="identifier")
    words = _plain(title or "")
    long_enough = len(words.split()) >= MIN_TITLE_WORDS and len(words) >= MIN_TITLE_CHARACTERS
    if long_enough and words in _plain(text):
        preprint = any(prefix in folded for prefix in PREPRINT_DOI_PREFIXES) or ("arxiv:" in folded)
        return IdentityEvidence(
            verified=True, basis="title", version="preprint" if preprint else None
        )
    return IdentityEvidence(verified=False)


def _pdf_head_text(content: bytes, *, pages: int = 3) -> str:
    """A PDF's metadata and first pages as text; empty when it cannot be read."""
    import io

    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:  # pragma: no cover - the parsers extra is absent
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        metadata: Any = reader.metadata or {}
        texts = [str(value) for value in metadata.values() if isinstance(value, str)]
        texts += [(page.extract_text() or "") for page in reader.pages[:pages]]
    except Exception:
        return ""
    return "\n".join(texts)


#: What a plan asks to do with the paper it fetches.
PLAN_ACTIONS = frozenset(
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


def manifestation_kind(full_text_location: Any) -> ManifestationKind:
    """What discovery says the full text is: PMC XML, a publisher PDF, or only an abstract."""
    if isinstance(full_text_location, str) and "pmc" in full_text_location.lower():
        return ManifestationKind.PMC_XML
    if full_text_location:
        return ManifestationKind.PUBLISHER_PDF
    return ManifestationKind.ABSTRACT_ONLY


def route_ladder(
    decision: RightsDecision, kind: ManifestationKind, *, now: datetime
) -> list[AcquisitionRoute]:
    """The routes tried in order: open ones only when the licence allows keeping a copy."""
    routes: list[AcquisitionRoute] = []
    if decision.permits(PolicyAction.STORE_CONTENT, now=now):
        if kind is ManifestationKind.PMC_XML:
            routes.append(AcquisitionRoute.OPEN_ACCESS_API)
        routes.append(AcquisitionRoute.OPEN_REPOSITORY)
    routes.append(AcquisitionRoute.USER_ASSISTED)
    return routes


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
                lambda after, size: uow.works.list_works(workspace_id, limit=size, after_id=after)
            )
            # Only what has no plan yet: planning runs again whenever papers are
            # included, after a run and after a person includes more, and a paper
            # planned twice would be fetched twice.
            planned_works = {
                plan.work_id
                for plan in await collect_pages(
                    lambda after, size: uow.assets.list_plans(
                        workspace_id, limit=size, after_id=after
                    )
                )
            }
            included = [
                w
                for w in works
                if w.relevance.is_included and w.is_active and w.id not in planned_works
            ]
            assertion_index = {
                work.id: await uow.observations.list_assertions(workspace_id, work_id=work.id)
                for work in included
            }

        for work in included:
            assertions = assertion_index[work.id]
            licence = _first_value(assertions, "licence")
            full_text_location = _first_value(assertions, "full_text_location")
            kind = manifestation_kind(full_text_location)

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

            requested_actions = PLAN_ACTIONS
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

            route_order = route_ladder(decision, kind, now=context.now())
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

    async def _already_fetched(
        self,
        asset: Asset,
        *,
        plan_id: UUID,
        workspace_id: UUID,
        strong: tuple[Identifier, ...],
        title: str | None,
    ) -> StageResult:
        """A file is stored for this plan: parse it, once it is known to be the paper."""
        unconfirmed = not asset.identity_verified and bool(strong)
        if unconfirmed and not await _requalify(
            self._context,
            asset,
            workspace_id=workspace_id,
            plan_id=plan_id,
            strong=strong,
            title=title,
        ):
            return StageResult(
                stage=StageName.ACQUISITION,
                status=StageStatus.FAILED,
                metrics={"outcome": AcquisitionOutcome.IDENTITY_MISMATCH.value},
                error_class=ErrorClass.IDENTITY_MISMATCH,
                error_detail="the stored file names neither the paper's identifiers nor its title",
            )
        return StageResult(
            stage=StageName.ACQUISITION,
            status=StageStatus.SUCCEEDED_NOOP,
            metrics={"outcome": AcquisitionOutcome.ALREADY_AVAILABLE.value},
            next_tasks=(TaskRequest(stage=StageName.PARSING, inputs={"asset_id": str(asset.id)}),),
        )

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
            # An abstract standing in for the paper is not its full text: the
            # routes are still worth trying.
            all_assets = await uow.assets.list_assets(workspace_id, work_id=plan.work_id)
            existing_assets = [asset for asset in all_assets if not is_abstract(asset)]
            assertions = await uow.observations.list_assertions(workspace_id, work_id=plan.work_id)
            summaries = await uow.works.get_summaries(workspace_id, [plan.work_id])
            summary = summaries.get(plan.work_id)

        # What discovery recorded: the licence the plan was decided on, and where
        # the full text was seen. A fetch reports neither, so without them every
        # fetched file was judged "licence unknown" and every open-access link
        # was missing.
        licence = _first_value(assertions, "licence")
        locations = _full_text_locations(assertions)
        strong = tuple(
            Identifier(scheme=i.scheme, value=i.value_normalized)
            for i in identifiers
            if i.scheme.is_strong
        )
        if locations.pmcid and not any(i.scheme is IdentifierScheme.PMCID for i in strong):
            strong = (*strong, Identifier(scheme=IdentifierScheme.PMCID, value=locations.pmcid))

        if existing_assets:
            return await self._already_fetched(
                existing_assets[0],
                plan_id=plan_id,
                workspace_id=workspace_id,
                strong=strong,
                title=summary.title if summary else None,
            )
        registry = context.require_connectors()
        limiter = RateLimiter(context)
        #: Seconds until each site this fetch could not ask yet (busy or resting).
        deferred_waits: list[int] = []
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
                        target_url=locations.url,
                    )
                    api_host = _hostname(getattr(connector, "base_url", None))
                    try:
                        if getattr(connector, "paces_sites", False):
                            # It follows links to many sites: each site is paced on
                            # its own, its API included, through the stored budgets.
                            raw = await limiter.per_site(
                                lambda gate, connector=connector, request=acquisition_request: (
                                    connector.acquire(request, gate=gate)
                                )
                            )
                        else:
                            # Through the shared budget, exactly as discovery is.
                            # Fetching a document is an external source request; a
                            # limiter only discovery respects is not a limit the
                            # source experiences.
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
                        if exc.context.get("deferred"):
                            # Not asked: the site is busy or resting. No attempt is
                            # recorded, and the paper comes back once it may be.
                            deferred_waits.append(int(exc.context.get("retry_after_seconds") or 60))
                            continue
                        attempt = attempt.model_copy(
                            update={
                                "completed_at": context.now(),
                                "outcome": _acquisition_outcome(exc.error_class),
                                "error_class": exc.error_class.value,
                                "note": exc.detail,
                                "http_status": exc.context.get("http_status"),
                                "source_host": exc.context.get("host") or api_host,
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
                        licence_expression=(
                            raw.licence_expression
                            or (licence if isinstance(licence, str) else None)
                            or "unknown"
                        ),
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
                        expected_title=summary.title if summary else None,
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
                            "source_host": _hostname(raw.source_url_redacted) or api_host,
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

        # A source that was down, slow or rate-limiting is tried again while the
        # task has deliveries left; only then is the paper handed to a person.
        passing = [a for a in attempts if a.outcome in _PASSING_OUTCOMES]
        # A site this fetch could not ask yet may still have the paper: it comes
        # back when the site may be asked, without spending one of its tries.
        deferred = acquired_asset is None and bool(deferred_waits)
        retrying = (
            not deferred
            and acquired_asset is None
            and bool(passing)
            and command.attempt < command.max_attempts
        )
        if deferred:
            final_outcome = AcquisitionOutcome.RATE_LIMITED_RETRYABLE
        elif retrying:
            final_outcome = passing[-1].outcome or AcquisitionOutcome.SOURCE_UNAVAILABLE_RETRYABLE
        elif acquired_asset is None and AcquisitionRoute.USER_ASSISTED in plan.route_order:
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
            if not retrying and not deferred:
                plan.status = (
                    "acquired"
                    if final_outcome.produced_asset
                    # Its abstract still stands in for it.
                    else "partial"
                    if len(all_assets) > len(existing_assets)
                    else "assisted"
                    if final_outcome is AcquisitionOutcome.ASSISTED_REQUIRED
                    else "failed"
                )
                plan.version += 1
                await uow.assets.update_plan(plan)
            await uow.commit()

        if deferred:
            # Spread over a minute, so papers waiting on one site do not all come
            # back in the same instant and meet the same busy site.
            wait = max(30, min(deferred_waits)) + plan_id.int % 60
            return StageResult(
                stage=StageName.ACQUISITION,
                status=StageStatus.DEFERRED,
                metrics={"outcome": "deferred", "attempts": len(attempts)},
                quality_gates={"attempt_provenance_recorded": True},
                retry_after_seconds=wait,
            )

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


async def _requalify(
    context: ServiceContext,
    asset: Asset,
    *,
    workspace_id: UUID,
    plan_id: UUID,
    strong: tuple[Identifier, ...],
    title: str | None,
) -> bool:
    """Check a stored, unverified file again, as the identity check now reads it.

    A file a fetch stored but could not tie to its paper (no identifier in it) is
    still there. With the title, and preprints, now counted, it may pass: then
    the asset is verified on the record and its plan acquired.
    """
    content = await context.object_store.get(asset.object_id)
    evidence = identity_evidence(content, strong, title)
    async with context.uow_factory() as uow:
        stored = await uow.assets.get_asset(workspace_id, asset.id)
        if stored is None:  # pragma: no cover - deleted meanwhile
            return False
        # Recorded either way: a file this rule could not tie to its paper is not
        # read again by a later press, only by a later rule.
        stored.metadata = {**(stored.metadata or {}), **evidence.as_metadata()}
        if not evidence.verified:
            await uow.assets.update_asset(stored)
            await uow.commit()
            return False
        stored.identity_verified = True
        await uow.assets.update_asset(stored)
        plan = await uow.assets.get_plan(workspace_id, plan_id)
        if plan is not None and plan.status != "acquired":
            plan.status = "acquired"
            plan.version += 1
            await uow.assets.update_plan(plan)
        await uow.audit.append(
            context.audit.build(
                workspace_id=workspace_id,
                actor_type=ActorType.WORKER,
                actor_id=None,
                action=AuditAction.ASSET_IDENTITY_CONFIRMED,
                subject_type="asset",
                subject_id=asset.id,
                previous_hash=await uow.audit.latest_hash(workspace_id),
                reason_code=evidence.basis,
                metadata=evidence.as_metadata(),
            )
        )
        await uow.commit()
    return True


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


def _hostname(url: str | None) -> str | None:
    """The site a URL names, lower-case, without port or credentials."""
    if not url:
        return None
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower() or None


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


#: Outcomes that say nothing about the paper: the source may answer next time.
_PASSING_OUTCOMES = frozenset(
    {
        AcquisitionOutcome.RATE_LIMITED_RETRYABLE,
        AcquisitionOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
    }
)


@dataclass(frozen=True, slots=True)
class FullTextLocations:
    """Where discovery saw a work's full text: an open-access link and a PMCID."""

    url: str | None = None
    pmcid: str | None = None


def _full_text_locations(assertions: list) -> FullTextLocations:
    """The first https link and the PMCID among a work's full-text location assertions.

    Sources assert a location as a URL (OpenAlex), as ``pmc:PMC…`` (Europe PMC)
    or as a mapping with a ``url`` (Unpaywall).
    """
    url: str | None = None
    pmcid: str | None = None
    for assertion in assertions:
        if assertion.predicate.value != "full_text_location":
            continue
        value = assertion.normalised_value_json or assertion.value_json
        if isinstance(value, dict):
            value = value.get("url")
        if not isinstance(value, str):
            continue
        if value.lower().startswith("pmc:") and pmcid is None:
            pmcid = value.split(":", 1)[1].strip() or None
        elif value.startswith("https://") and url is None:
            url = value
    return FullTextLocations(url=url, pmcid=pmcid)


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
