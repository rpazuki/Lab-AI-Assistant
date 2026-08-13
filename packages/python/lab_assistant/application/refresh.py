"""Stage 12: monitoring, refresh, retraction, and correction propagation.

Dependency-driven recomputation, with the reason recorded:

    new source response  -> reparse assertions -> maybe an identity decision
    new asset            -> parse -> extract -> review -> release
    new parser           -> new rendition -> new spans -> re-extract affected claims
    new template         -> extract affected predicates only
    new embedding        -> rebuild that projection only
    rights revocation    -> policy update -> emergency successor release

An old release never changes. A correction produces a successor, and historical
answers that cited the corrected material are flagged rather than rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import NotFound
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import StageName, TaskRequest
from lab_assistant.domain.chat import CitationStatus
from lab_assistant.domain.identity import RelationKind, WorkRelation
from lab_assistant.domain.states import ReleaseState
from lab_assistant.domain.workspace import Capability

__all__ = ["RecomputationPlan", "RefreshService"]


@dataclass(slots=True)
class RecomputationPlan:
    """What must be recomputed, and why."""

    reason: str
    tasks: list[TaskRequest] = field(default_factory=list)
    affected_work_ids: list[UUID] = field(default_factory=list)
    affected_release_ids: list[UUID] = field(default_factory=list)
    affected_message_ids: list[UUID] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "tasks": [{"stage": task.stage.value, "inputs": task.inputs} for task in self.tasks],
            "affected_works": [str(i) for i in self.affected_work_ids],
            "affected_releases": [str(i) for i in self.affected_release_ids],
            "affected_messages": [str(i) for i in self.affected_message_ids],
            "notes": self.notes,
        }


class RefreshService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def record_retraction(
        self,
        actor: ActorContext,
        *,
        work_id: UUID,
        retracting_work_id: UUID | None = None,
        reason_code: str = "source_retraction_feed",
    ) -> RecomputationPlan:
        """Mark a work retracted and plan the successor release."""
        actor.require(Capability.DECIDE_IDENTITY)
        context = self._context
        plan = RecomputationPlan(reason=f"retraction:{reason_code}")
        async with context.uow_factory() as uow:
            work = await uow.works.get(actor.workspace_id, work_id)
            if work is None:
                raise NotFound(f"work {work_id} not found")
            work.retraction_status = "retracted"
            work.version += 1
            await uow.works.update(work)
            plan.affected_work_ids.append(work_id)

            if retracting_work_id is not None:
                await uow.works.add_relation(
                    WorkRelation(
                        id=context.new_id(),
                        workspace_id=actor.workspace_id,
                        from_work_id=retracting_work_id,
                        to_work_id=work_id,
                        kind=RelationKind.RETRACTS,
                        decided_by=actor.user_id,
                        created_at=context.now(),
                    )
                )

            # Which published releases contain it, and which answers cited it.
            for release in await uow.releases.list_releases(actor.workspace_id):
                if release.status not in (ReleaseState.PUBLISHED, ReleaseState.SUPERSEDED):
                    continue
                items = await uow.releases.list_items(actor.workspace_id, release.id)
                if any(item.record.work_id == work_id for item in items):
                    plan.affected_release_ids.append(release.id)

            for session in await uow.chat.list_workspace_sessions(actor.workspace_id):
                for message in await uow.chat.list_messages(actor.workspace_id, session.id):
                    if any(citation.work_id == work_id for citation in message.citations):
                        flagged = tuple(
                            citation.model_copy(update={"status": CitationStatus.SUPERSEDED})
                            if citation.work_id == work_id
                            else citation
                            for citation in message.citations
                        )
                        message.citations = flagged
                        message.citation_validation_passed = False
                        await uow.chat.add_message(message)
                        plan.affected_message_ids.append(message.id)

            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.IDENTITY_DECIDED,
                    subject_type="work",
                    subject_id=work_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=reason_code,
                    metadata={
                        "retraction": True,
                        "affected_releases": [str(i) for i in plan.affected_release_ids],
                        "flagged_messages": len(plan.affected_message_ids),
                    },
                )
            )
            await uow.commit()

        plan.notes.append(
            "published releases are unchanged; build a successor release to remove the work "
            "from the serving alias"
        )
        plan.tasks.append(
            TaskRequest(
                stage=StageName.RELEASE_ASSEMBLY,
                inputs={"reason": "retraction", "work_id": str(work_id)},
            )
        )
        return plan

    async def plan_reparse(
        self, actor: ActorContext, *, parser_id: str, parser_version: str
    ) -> RecomputationPlan:
        """A parser improvement invalidates renditions, not assets."""
        actor.require(Capability.REVIEW_PARSE)
        plan = RecomputationPlan(reason=f"parser_change:{parser_id}@{parser_version}")
        async with self._context.uow_factory() as uow:
            renditions = await uow.renditions.list_renditions(actor.workspace_id)
            for rendition in renditions:
                run = await uow.renditions.get_parse_run(actor.workspace_id, rendition.parse_run_id)
                if run is None:
                    continue
                if run.parser_id != parser_id or run.parser_version == parser_version:
                    continue
                plan.tasks.append(
                    TaskRequest(
                        stage=StageName.PARSING, inputs={"asset_id": str(rendition.asset_id)}
                    )
                )
                if rendition.work_id:
                    plan.affected_work_ids.append(rendition.work_id)
        plan.notes.append(
            "a new rendition creates new spans; existing citations keep pointing at the old "
            "rendition and are not retargeted"
        )
        return plan

    async def plan_template_change(
        self, actor: ActorContext, *, template_id: str, changed_target_ids: tuple[str, ...]
    ) -> RecomputationPlan:
        """A template change re-extracts only the affected predicates."""
        actor.require(Capability.RUN_EXTRACTION)
        plan = RecomputationPlan(reason=f"template_change:{template_id}")
        async with self._context.uow_factory() as uow:
            claims = await uow.claims.list_claims(actor.workspace_id, limit=2000)
            affected_renditions: set[UUID] = set()
            for claim in claims:
                if claim.template_id != template_id:
                    continue
                if changed_target_ids and claim.target_id not in changed_target_ids:
                    continue
                for rendition in await uow.renditions.list_renditions(
                    actor.workspace_id, work_id=claim.work_id
                ):
                    affected_renditions.add(rendition.id)
                plan.affected_work_ids.append(claim.work_id)
        for rendition_id in sorted(affected_renditions, key=str):
            plan.tasks.append(
                TaskRequest(
                    stage=StageName.EXTRACTION,
                    inputs={"rendition_id": str(rendition_id), "template": template_id},
                )
            )
        plan.notes.append("unchanged targets keep their existing claim revisions and reviews")
        return plan

    async def plan_embedding_change(
        self, actor: ActorContext, *, release_id: UUID, profile_id: str
    ) -> RecomputationPlan:
        """A new embedding profile rebuilds one projection, nothing else."""
        actor.require(Capability.MANAGE_RELEASE)
        plan = RecomputationPlan(reason=f"embedding_change:{profile_id}")
        plan.affected_release_ids.append(release_id)
        plan.tasks.append(
            TaskRequest(
                stage=StageName.INDEXING,
                inputs={"release_id": str(release_id), "profile_id": profile_id},
            )
        )
        plan.notes.append(
            "domain records are untouched; vectors of a different dimension live in their own "
            "projection table"
        )
        return plan

    async def revoke_rights(
        self,
        actor: ActorContext,
        *,
        rights_decision_id: UUID,
        reason_code: str = "licence_revoked",
    ) -> RecomputationPlan:
        """Emergency path: stop serving affected material immediately."""
        actor.require(Capability.DECIDE_RIGHTS)
        context = self._context
        plan = RecomputationPlan(reason=f"rights_revocation:{reason_code}")
        async with context.uow_factory() as uow:
            decision = await uow.policies.get_rights_decision(
                actor.workspace_id, rights_decision_id
            )
            if decision is None:
                raise NotFound(f"rights decision {rights_decision_id} not found")
            from lab_assistant.domain.states import RightsState

            revoked = decision.model_copy(
                update={
                    "status": RightsState.PROHIBITED,
                    "allowed_actions": frozenset(),
                    "decided_by": actor.user_id,
                    "decided_by_type": "human_steward",
                    "decided_at": context.now(),
                    "note": reason_code,
                }
            )
            await uow.policies.add_rights_decision(revoked)
            assets = [
                asset
                for asset in await uow.assets.list_assets(actor.workspace_id)
                if asset.rights_decision_id == rights_decision_id
            ]
            for asset in assets:
                if asset.work_id:
                    plan.affected_work_ids.append(asset.work_id)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RIGHTS_DECIDED,
                    subject_type="rights_decision",
                    subject_id=rights_decision_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=reason_code,
                    metadata={
                        "revocation": True,
                        "affected_assets": len(assets),
                    },
                )
            )
            await uow.commit()
        plan.notes.append(
            "build an emergency successor release; object purge follows the retention policy "
            "and is audited separately"
        )
        plan.tasks.append(
            TaskRequest(
                stage=StageName.RELEASE_ASSEMBLY,
                inputs={"reason": "rights_revocation"},
            )
        )
        return plan
