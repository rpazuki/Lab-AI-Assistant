"""Stage 5: parsing, OCR, and structural extraction.

Parser selection is by detected content, never by filename. The rendition tree
and its quality metrics are stored as an immutable object; the asset stays
available so a parser fix can produce a *new* rendition without re-acquiring
anything.

A rendition below its profile thresholds is ``degraded``. Degraded is a real
state with a reviewer decision attached, not a silent pass.
"""

from __future__ import annotations

from uuid import UUID

from lab_assistant.application.acquisition import parse_readiness
from lab_assistant.application.context import ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.canonical import fingerprint as make_fingerprint
from lab_assistant.contracts.errors import ErrorClass, NotFound, PolicyDenied
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.parsing import NodeKind, RenditionDocument
from lab_assistant.contracts.stage import (
    StageCommand,
    StageName,
    StageResult,
    StageStatus,
    TaskRequest,
)
from lab_assistant.domain.assets import AcquisitionRoute
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.renditions import (
    ParseRun,
    Rendition,
    parse_run_fingerprint,
    rendition_quality_blockers,
)
from lab_assistant.domain.states import ParseState
from lab_assistant.provenance.fingerprints import task_fingerprint

__all__ = ["ParsingHandler", "ParsingService"]

IMPLEMENTATION_VERSION = "parsing/1"


class ParsingHandler:
    stage_name = StageName.PARSING
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        context = self._context
        workspace_id = command.workspace_id
        asset_id = UUID(str(command.inputs["asset_id"]))
        parsers = context.require_parsers()

        async with context.uow_factory() as uow:
            asset = await uow.assets.get_asset(workspace_id, asset_id)
            if asset is None:
                raise NotFound(f"asset {asset_id} not found")
            rights = (
                await uow.policies.get_rights_decision(workspace_id, asset.rights_decision_id)
                if asset.rights_decision_id
                else None
            )

        route = asset.metadata.get("route")
        require_scan = (
            context.settings.malware_scan_required_for_uploads
            if route == AcquisitionRoute.USER_ASSISTED.value
            else context.settings.malware_scan_required_for_open_access
        )
        blockers = parse_readiness(
            asset=asset, rights=rights, now=context.now(), require_scan=require_scan
        )
        if blockers:
            raise PolicyDenied("asset is not parse-ready", context={"blockers": list(blockers)})

        parser = parsers.for_media_type(asset.media_type_detected)
        # The steward's limits (Collections > Parse quality) in place of the defaults.
        from lab_assistant.application.parse_quality import apply_limits, current_limits

        async with context.uow_factory() as uow:
            limits, _ = await current_limits(uow, workspace_id)
        profile = apply_limits(parsers.profile_for(asset.media_type_detected), limits)
        fingerprint = parse_run_fingerprint(
            asset_sha256=asset.sha256,
            parser_id=parser.parser_id,
            parser_version=parser.parser_version,
            profile=profile,
        )

        async with context.uow_factory() as uow:
            previous = await uow.renditions.find_parse_run_by_fingerprint(workspace_id, fingerprint)
            if previous is not None and previous.asset_id != asset.id:
                # The same bytes under another asset (one file fetched for two
                # papers): this asset's run is its own, with its own fingerprint.
                # Reusing the shared one inserted a second row with the same
                # fingerprint, which the database refused, and the task died.
                fingerprint = make_fingerprint(
                    kind="parse_run_for_asset",
                    parts={"parse_run": fingerprint, "asset_id": str(asset.id)},
                )
                previous = await uow.renditions.find_parse_run_by_fingerprint(
                    workspace_id, fingerprint
                )
            if previous is not None and previous.status == "succeeded":
                renditions = await uow.renditions.list_renditions(workspace_id, asset_id=asset_id)
                existing = next((r for r in renditions if r.parse_run_id == previous.id), None)
                if existing is not None:
                    return StageResult(
                        stage=StageName.PARSING,
                        status=StageStatus.SUCCEEDED_NOOP,
                        metrics={"rendition_id": str(existing.id), "cached": True},
                        next_tasks=(
                            TaskRequest(
                                stage=StageName.EXTRACTION,
                                inputs={"rendition_id": str(existing.id)},
                            ),
                        )
                        if existing.is_extraction_eligible
                        else (),
                    )

        content = await context.object_store.get(asset.object_id)
        if sha256_bytes(content) != asset.sha256:
            async with context.uow_factory() as uow:
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=workspace_id,
                        actor_type=ActorType.WORKER,
                        actor_id=command.actor_id,
                        action=AuditAction.OBJECT_INTEGRITY_FAILED,
                        subject_type="asset",
                        subject_id=asset.id,
                        previous_hash=await uow.audit.latest_hash(workspace_id),
                        metadata={"expected_sha256": asset.sha256},
                    )
                )
                await uow.commit()
            return StageResult(
                stage=StageName.PARSING,
                status=StageStatus.QUARANTINED,
                error_class=ErrorClass.INVALID_CONTENT,
                error_detail="stored bytes do not match the asset digest",
                quality_gates={"asset_integrity": False},
            )

        run = ParseRun(
            # A retry after a failed or interrupted run takes that run's row: the
            # fingerprint is unique, so a new row for the same input is refused.
            id=previous.id if previous is not None else context.new_id(),
            workspace_id=workspace_id,
            asset_id=asset.id,
            parser_id=parser.parser_id,
            parser_version=parser.parser_version,
            input_sha256=asset.sha256,
            run_fingerprint=fingerprint,
            status="running",
            started_at=context.now(),
        )
        async with context.uow_factory() as uow:
            if previous is not None:
                await uow.renditions.update_parse_run(run)
            else:
                await uow.renditions.add_parse_run(run)
            await uow.commit()

        try:
            result = parser.parse(
                content,
                profile=profile,
                media_type=asset.media_type_detected,
                hints={"work_id": str(asset.work_id) if asset.work_id else None},
            )
        except Exception as exc:
            run.status = "failed"
            run.completed_at = context.now()
            async with context.uow_factory() as uow:
                await uow.renditions.update_parse_run(run)
                await uow.commit()
            return StageResult(
                stage=StageName.PARSING,
                status=StageStatus.FAILED,
                error_class=ErrorClass.INVALID_CONTENT,
                error_detail=f"{type(exc).__name__}: {exc}",
                quality_gates={"parser_completed": False},
            )

        rendition_document = result.rendition
        payload = canonical_json(rendition_document.model_dump(mode="json"))
        stored = await context.object_store.put(
            payload,
            workspace_id=workspace_id,
            object_class="rendition",
            media_type="application/json",
        )
        quality_blockers = rendition_quality_blockers(rendition_document.quality, profile)
        quality_status = ParseState.READY if not quality_blockers else ParseState.DEGRADED

        rendition = Rendition(
            id=context.new_id(),
            workspace_id=workspace_id,
            parse_run_id=run.id,
            asset_id=asset.id,
            work_id=asset.work_id,
            format=f"structured/{rendition_document.schema_version}",
            schema_version=rendition_document.schema_version,
            object_id=stored.id,
            sha256=stored.sha256,
            language=rendition_document.language,
            page_count=rendition_document.quality.page_count,
            section_count=rendition_document.quality.section_count,
            table_count=rendition_document.quality.table_count,
            figure_count=rendition_document.quality.figure_count,
            quality_status=quality_status,
            created_at=context.now(),
        )
        run.status = "succeeded"
        run.completed_at = context.now()
        run.quality_metrics = rendition_document.quality

        async with context.uow_factory() as uow:
            await uow.renditions.update_parse_run(run)
            await register_object(uow, stored)
            await uow.renditions.add_rendition(rendition)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=workspace_id,
                    actor_type=ActorType.WORKER,
                    actor_id=command.actor_id,
                    action=AuditAction.PARSE_COMPLETED,
                    subject_type="rendition",
                    subject_id=rendition.id,
                    previous_hash=await uow.audit.latest_hash(workspace_id),
                    reason_code=quality_status.value,
                    metadata={
                        "parser": f"{parser.parser_id}@{parser.parser_version}",
                        "profile": f"{profile.profile_id}@{profile.profile_version}",
                        "sha256": rendition.sha256,
                        "quality_blockers": list(quality_blockers),
                        "sections": rendition.section_count,
                        "tables": rendition.table_count,
                    },
                )
            )
            await uow.commit()

        return StageResult(
            stage=StageName.PARSING,
            status=StageStatus.SUCCEEDED if not quality_blockers else StageStatus.DEGRADED,
            created_object_ids=(stored.id,),
            metrics={
                "rendition_id": str(rendition.id),
                "page_coverage": rendition_document.quality.page_coverage,
                "tables": rendition.table_count,
                "sections": rendition.section_count,
                "characters": rendition_document.quality.characters,
                "ocr_used": rendition_document.quality.ocr_used,
                "quality_blockers": list(quality_blockers),
            },
            quality_gates={
                "parser_completed": True,
                "profile_thresholds_met": not quality_blockers,
                "reference_separation": rendition_document.quality.reference_separation
                or not profile.require_reference_separation,
            },
            human_action_required=(
                "degraded rendition needs a reviewer decision" if quality_blockers else None
            ),
            next_tasks=(
                (
                    TaskRequest(
                        stage=StageName.EXTRACTION,
                        inputs={"rendition_id": str(rendition.id)},
                    ),
                )
                if not quality_blockers
                else ()
            ),
        )


class ParsingService:
    """Parse-quality review actions."""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def accept_degraded(
        self,
        actor,
        *,
        rendition_id: UUID,
        reason: str,
        expected_version: int | None = None,
    ) -> Rendition:
        """Accept a degraded rendition for extraction, on the record."""
        self._context.require_installation(InstallationCapability.PROCESS_CONTENT)
        from lab_assistant.contracts.errors import ConcurrencyConflict
        from lab_assistant.domain.workspace import Capability

        actor.require(Capability.REVIEW_PARSE)
        context = self._context
        async with context.uow_factory() as uow:
            rendition = await uow.renditions.get_rendition(actor.workspace_id, rendition_id)
            if rendition is None:
                raise NotFound(f"rendition {rendition_id} not found")
            if expected_version is not None and rendition.version != expected_version:
                raise ConcurrencyConflict(
                    f"rendition {rendition_id} changed since it was read",
                    context={"expected": expected_version, "actual": rendition.version},
                )
            rendition.accepted_degraded_by = actor.user_id
            rendition.accepted_degraded_reason = reason
            rendition.version += 1
            await uow.renditions.update_rendition(rendition)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RENDITION_ACCEPTED_DEGRADED,
                    subject_type="rendition",
                    subject_id=rendition.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=reason,
                )
            )
            await uow.commit()
        return rendition

    async def load_document(self, workspace_id: UUID, rendition: Rendition) -> RenditionDocument:
        """Load and validate a rendition payload from object storage."""
        if rendition.object_id is None:
            raise NotFound(f"rendition {rendition.id} has no stored payload")
        payload = await self._context.object_store.get(rendition.object_id)
        if sha256_bytes(payload) != rendition.sha256:
            raise NotFound(f"rendition {rendition.id} payload failed its checksum")
        import json

        return RenditionDocument.model_validate(json.loads(payload))

    @staticmethod
    def table_node_ids(document: RenditionDocument) -> tuple[str, ...]:
        return tuple(node.node_id for node in document.walk() if node.kind is NodeKind.TABLE)
