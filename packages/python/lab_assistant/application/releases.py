"""Stages 9, 11: release assembly, validation, publication, and rollback.

A release is immutable. Publishing switches an alias; rollback switches it back.
Neither rewrites a row, which is what lets a historical answer stay reproducible
after a correction.

The publish path reads gate results that validation recorded. It cannot pass a
gate by re-running it more leniently, and a failed build never touches an active
alias.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.application.pagination import collect_pages, every_session
from lab_assistant.application.release_gates import evaluation_gate_status
from lab_assistant.contracts.canonical import canonical_json
from lab_assistant.contracts.errors import (
    NotFound,
    PolicyDenied,
    StateTransitionError,
    ValidationFailure,
)
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.release import (
    ReleaseDiff,
    ReleaseItemRecord,
    ReleaseManifest,
    ReleaseValidationReport,
)
from lab_assistant.contracts.stage import StageName, TaskRequest
from lab_assistant.domain.claims import claim_release_blockers
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.release import (
    CorpusRelease,
    ReleaseItem,
    SearchAlias,
    compute_release_diff,
)
from lab_assistant.domain.states import ParseState, ReleaseState, RelevanceState, ReviewState
from lab_assistant.domain.workspace import Capability, Role

__all__ = ["DEFAULT_ALIAS", "ReleaseDraftRequest", "ReleaseService"]

DEFAULT_ALIAS = "current"


@dataclass(frozen=True, slots=True)
class ReleaseDraftRequest:
    name: str
    semantic_version: str
    scope_version_ids: tuple[UUID, ...] = ()
    embedding_profile_ids: tuple[str, ...] = ()
    parent_release_id: UUID | None = None
    require_claim_review: bool = True
    #: When false, the release serves renditions with verified spans and no claim
    #: graph — the M1 milestone, which needs parsing but not claim review.
    include_claims: bool = True
    policy_version_id: UUID | None = None


class ReleaseService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def draft(
        self, actor: ActorContext, request: ReleaseDraftRequest
    ) -> tuple[CorpusRelease, ReleaseManifest, ReleaseDiff]:
        """Assemble a draft release and its deterministic manifest."""
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.MANAGE_RELEASE)
        context = self._context
        async with context.uow_factory() as uow:
            # Paged, because a release that silently stopped at a fixed limit would
            # be a corpus snapshot missing material nobody can name afterwards —
            # and it would still pass its gates.
            works = await collect_pages(
                lambda after, size: uow.works.list_works(
                    actor.workspace_id,
                    relevance=RelevanceState.INCLUDED.value,
                    limit=size,
                    after_id=after,
                )
            )
            included = [work for work in works if work.is_active]
            records: list[ReleaseItemRecord] = []
            for work in included:
                renditions = [
                    rendition
                    for rendition in await uow.renditions.list_renditions(
                        actor.workspace_id, work_id=work.id
                    )
                    if rendition.is_extraction_eligible
                    or rendition.quality_status is ParseState.READY
                ]
                rendition = renditions[0] if renditions else None
                claim_revision_ids: list[UUID] = []
                if request.include_claims:
                    for claim in await uow.claims.list_claims(actor.workspace_id, work_id=work.id):
                        if claim.current_revision_id is None:
                            continue
                        revision = await uow.claims.get_revision(
                            actor.workspace_id, claim.current_revision_id
                        )
                        if revision is None:
                            continue
                        verified = await _verified_span_ids(
                            uow, actor.workspace_id, revision.evidence_span_ids
                        )
                        blockers = claim_release_blockers(
                            revision,
                            verified_span_ids=verified,
                            require_review=request.require_claim_review,
                        )
                        if blockers:
                            continue
                        claim_revision_ids.append(revision.id)
                if rendition is None and not claim_revision_ids:
                    continue
                asset = (
                    await uow.assets.get_asset(actor.workspace_id, rendition.asset_id)
                    if rendition
                    else None
                )
                records.append(
                    ReleaseItemRecord(
                        work_id=work.id,
                        manifestation_id=asset.manifestation_id if asset else None,
                        asset_id=asset.id if asset else None,
                        rendition_id=rendition.id if rendition else None,
                        rendition_digest=rendition.sha256 if rendition else None,
                        claim_revision_ids=tuple(sorted(claim_revision_ids, key=str)),
                        access_policy_snapshot_id=(asset.rights_decision_id if asset else None),
                        inclusion_reason="relevance_included",
                    )
                )

            release = CorpusRelease(
                id=context.new_id(),
                workspace_id=actor.workspace_id,
                name=request.name,
                semantic_version=request.semantic_version,
                status=ReleaseState.DRAFT,
                parent_release_id=request.parent_release_id,
                scope_version_ids=request.scope_version_ids,
                policy_version_id=request.policy_version_id,
                embedding_profile_ids=request.embedding_profile_ids,
                created_by=actor.user_id,
                created_at=context.now(),
            )
            manifest = ReleaseManifest(
                release_id=release.id,
                workspace_id=actor.workspace_id,
                name=request.name,
                semantic_version=request.semantic_version,
                parent_release_id=request.parent_release_id,
                scope_version_ids=request.scope_version_ids,
                policy_version_id=request.policy_version_id,
                embedding_profile_ids=request.embedding_profile_ids,
                items=tuple(records),
                created_at=context.now(),
            )
            stored = await context.object_store.put(
                manifest.canonical_bytes(),
                workspace_id=actor.workspace_id,
                object_class="release_manifest",
                media_type="application/json",
            )
            await register_object(uow, stored)
            release.release_manifest_object_id = stored.id
            release.manifest_sha256 = manifest.manifest_sha256

            await uow.releases.add(release)
            await uow.releases.add_items(
                [
                    ReleaseItem(
                        id=context.new_id(),
                        workspace_id=actor.workspace_id,
                        release_id=release.id,
                        record=record,
                    )
                    for record in records
                ]
            )

            parent_manifest = None
            if request.parent_release_id is not None:
                parent_manifest = await self._load_manifest(uow, actor, request.parent_release_id)
            diff = compute_release_diff(
                manifest=manifest,
                parent_manifest=parent_manifest,
                retracted_work_ids=frozenset(work.id for work in included if work.is_retracted),
            )
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEASE_DRAFTED,
                    subject_type="corpus_release",
                    subject_id=release.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    metadata={
                        "items": len(records),
                        "manifest_sha256": release.manifest_sha256,
                        "diff": {
                            "added": len(diff.added_work_ids),
                            "changed": len(diff.changed_work_ids),
                            "removed": len(diff.removed_work_ids),
                            "unchanged": len(diff.unchanged_work_ids),
                        },
                    },
                )
            )
            await uow.commit()
        return release, manifest, diff

    async def _load_manifest(
        self, uow, actor: ActorContext, release_id: UUID
    ) -> ReleaseManifest | None:
        release = await uow.releases.get(actor.workspace_id, release_id)
        if release is None or release.release_manifest_object_id is None:
            return None
        payload = await self._context.object_store.get(release.release_manifest_object_id)
        import json

        return ReleaseManifest.model_validate(json.loads(payload))

    async def load_manifest(self, actor: ActorContext, release_id: UUID) -> ReleaseManifest:
        async with self._context.uow_factory() as uow:
            manifest = await self._load_manifest(uow, actor, release_id)
        if manifest is None:
            raise NotFound(f"release {release_id} has no stored manifest")
        return manifest

    async def validate(self, actor: ActorContext, *, release_id: UUID) -> ReleaseValidationReport:
        """Run every release gate and record the results on the release."""
        actor.require(Capability.MANAGE_RELEASE)
        context = self._context
        gates: dict[str, bool] = {}
        failures: list[str] = []
        metrics: dict[str, object] = {}

        async with context.uow_factory() as uow:
            release = await uow.releases.get(actor.workspace_id, release_id)
            if release is None:
                raise NotFound(f"release {release_id} not found")
            if release.status not in (
                ReleaseState.DRAFT,
                ReleaseState.VALIDATING,
                ReleaseState.VALIDATED,
                ReleaseState.REJECTED,
            ):
                raise StateTransitionError(
                    f"release in state {release.status.value} cannot be validated"
                )
            release.status = ReleaseState.VALIDATING
            await uow.releases.update(release)

            items = await uow.releases.list_items(actor.workspace_id, release_id)
            manifest = await self._load_manifest(uow, actor, release_id)

            # 1. relational integrity and object availability
            integrity = True
            evidence_valid = True
            policy_complete = True
            identity_clean = True
            review_complete = True
            for item in items:
                record = item.record
                if record.rendition_id is not None:
                    rendition = await uow.renditions.get_rendition(
                        actor.workspace_id, record.rendition_id
                    )
                    if rendition is None or rendition.object_id is None:
                        integrity = False
                        failures.append(f"missing rendition for work {record.work_id}")
                        continue
                    if not await context.object_store.verify(rendition.object_id):
                        integrity = False
                        failures.append(f"rendition object checksum failed for {rendition.id}")
                    for span in await uow.renditions.list_spans(actor.workspace_id, rendition.id):
                        if span.end_offset <= span.start_offset:
                            evidence_valid = False
                            failures.append(f"span {span.id} has a non-positive range")
                if record.asset_id is not None:
                    asset = await uow.assets.get_asset(actor.workspace_id, record.asset_id)
                    if asset is None:
                        integrity = False
                        failures.append(f"missing asset {record.asset_id}")
                    elif asset.rights_decision_id is None:
                        policy_complete = False
                        failures.append(f"asset {asset.id} has no rights decision")
                    else:
                        decision = await uow.policies.get_rights_decision(
                            actor.workspace_id, asset.rights_decision_id
                        )
                        if decision is None or decision.is_expired(context.now()):
                            policy_complete = False
                            failures.append(
                                f"rights decision for asset {asset.id} is missing or expired"
                            )
                work = await uow.works.get(actor.workspace_id, record.work_id)
                if work is None or work.identity_status.value == "conflict":
                    identity_clean = False
                    failures.append(f"work {record.work_id} has an unresolved identity conflict")
                for revision_id in record.claim_revision_ids:
                    revision = await uow.claims.get_revision(actor.workspace_id, revision_id)
                    if revision is None:
                        integrity = False
                        failures.append(f"missing claim revision {revision_id}")
                        continue
                    if revision.review_status is not ReviewState.UNREVIEWED and not (
                        revision.review_status.is_approved
                    ):
                        review_complete = False
                        failures.append(
                            f"claim revision {revision_id} is {revision.review_status.value}"
                        )
                    verified = await _verified_span_ids(
                        uow, actor.workspace_id, revision.evidence_span_ids
                    )
                    blockers = claim_release_blockers(
                        revision, verified_span_ids=verified, require_review=True
                    )
                    if blockers:
                        evidence_valid = False
                        failures.extend(f"claim revision {revision_id}: {b}" for b in blockers)

            gates["relational_integrity"] = integrity
            gates["evidence_span_validity"] = evidence_valid
            gates["access_policy_completeness"] = policy_complete
            gates["identity_conflict_absence"] = identity_clean
            gates["required_review_status"] = review_complete
            gates["deterministic_manifest_hash"] = (
                manifest is not None and manifest.manifest_sha256 == release.manifest_sha256
            )
            gates["non_empty"] = bool(items)
            if not items:
                failures.append("release contains no items")

            metrics["item_count"] = len(items)
            metrics["claim_revision_count"] = sum(
                len(item.record.claim_revision_ids) for item in items
            )
            metrics["rendition_count"] = sum(
                1 for item in items if item.record.rendition_id is not None
            )

            report = ReleaseValidationReport(
                release_id=release_id,
                gates=gates,
                failures=tuple(failures),
                metrics=metrics,
                manifest_sha256=release.manifest_sha256,
                validated_at=context.now(),
            )
            release.gate_results = dict(gates)
            # Passing every gate makes a release validated, not approved: approval
            # is a person's decision, recorded with their name by ``approve``.
            release.status = ReleaseState.VALIDATED if report.passed else ReleaseState.REJECTED
            release.version += 1
            await uow.releases.update(release)
            # No column points at the validation report, so without this row
            # the object is unreachable from SQL and a restore check cannot
            # tell a missing report from one that was never written.
            report_object = await context.object_store.put(
                canonical_json(report.model_dump(mode="json")),
                workspace_id=actor.workspace_id,
                object_class="release_validation_report",
                media_type="application/json",
            )
            await register_object(uow, report_object)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEASE_VALIDATED,
                    subject_type="corpus_release",
                    subject_id=release_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="passed" if report.passed else "failed",
                    metadata={"gates": gates, "failures": failures[:20]},
                )
            )
            await uow.commit()
        return report

    async def approve(
        self, actor: ActorContext, *, release_id: UUID, evaluation_run_id: UUID | None = None
    ) -> tuple[CorpusRelease, tuple[TaskRequest, ...]]:
        """Record the approver and queue the projection build."""
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.APPROVE_RELEASE)
        context = self._context
        async with context.uow_factory() as uow:
            release = await uow.releases.get(actor.workspace_id, release_id)
            if release is None:
                raise NotFound(f"release {release_id} not found")
            if release.status is not ReleaseState.VALIDATED:
                raise StateTransitionError(
                    f"release must be validated before approval (state {release.status.value})"
                )
            failed = [name for name, ok in release.gate_results.items() if not ok]
            if failed:
                raise ValidationFailure(
                    "release has failed gates", context={"failed_gates": failed}
                )
            release.transition_to(ReleaseState.APPROVED)
            release.approved_by = actor.user_id
            release.evaluation_run_id = evaluation_run_id
            release.version += 1
            await uow.releases.update(release)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEASE_APPROVED,
                    subject_type="corpus_release",
                    subject_id=release_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    metadata={
                        "evaluation_run_id": str(evaluation_run_id) if evaluation_run_id else None
                    },
                )
            )
            await uow.commit()
        return release, (
            TaskRequest(stage=StageName.INDEXING, inputs={"release_id": str(release_id)}),
        )

    async def publish(
        self,
        actor: ActorContext,
        *,
        release_id: UUID,
        alias: str = DEFAULT_ALIAS,
        override_reason: str | None = None,
    ) -> SearchAlias:
        """Switch the alias atomically, or refuse with the blockers.

        Required benchmarks (see ``release_gates``) are blockers like any other,
        except that an admin may publish past them by giving ``override_reason``,
        which is audited. Nothing overrides the release's own blockers.
        """
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.APPROVE_RELEASE)
        reason = (override_reason or "").strip()
        context = self._context
        async with context.uow_factory() as uow:
            release = await uow.releases.get(actor.workspace_id, release_id)
            if release is None:
                raise NotFound(f"release {release_id} not found")
            gate_status = await evaluation_gate_status(uow, actor.workspace_id, release_id)
            unmet = [status for status in gate_status if not status.passed]
            is_admin = actor.membership is not None and actor.membership.role is Role.ADMIN
            if unmet and reason and not is_admin:
                raise PolicyDenied("only an admin can publish past a required benchmark")
            if unmet and not reason:
                raise ValidationFailure(
                    "release cannot be published: a required benchmark has not passed on it",
                    context={
                        "blockers": [f"evaluation:{status.key} has not passed" for status in unmet],
                        "evaluation_gates": [status.as_dict() for status in gate_status],
                    },
                )
            if release.status is ReleaseState.APPROVED:
                release.transition_to(ReleaseState.PUBLISHING)
            blockers = release.publish_blockers()
            if blockers:
                # Back to approved: a failed publish never leaves the alias moved
                # or the release in a state that looks published.
                if release.status is ReleaseState.PUBLISHING:
                    release.status = ReleaseState.APPROVED
                await uow.releases.update(release)
                await uow.commit()
                raise ValidationFailure(
                    "release cannot be published", context={"blockers": list(blockers)}
                )

            current = await uow.releases.get_alias(actor.workspace_id, alias)
            if current is None:
                current = SearchAlias(workspace_id=actor.workspace_id, alias=alias)
            previous_release_id = current.release_id
            current.switch_to(release_id, by=actor.user_id, at=context.now())
            release.transition_to(ReleaseState.PUBLISHED, at=context.now())
            await uow.releases.update(release)
            await uow.releases.upsert_alias(current)

            if previous_release_id is not None and previous_release_id != release_id:
                previous = await uow.releases.get(actor.workspace_id, previous_release_id)
                if previous is not None and previous.status is ReleaseState.PUBLISHED:
                    previous.superseded_by_id = release_id
                    previous.transition_to(ReleaseState.SUPERSEDED)
                    await uow.releases.update(previous)

            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEASE_PUBLISHED,
                    subject_type="corpus_release",
                    subject_id=release_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    metadata={
                        "alias": alias,
                        "previous_release_id": (
                            str(previous_release_id) if previous_release_id else None
                        ),
                        "manifest_sha256": release.manifest_sha256,
                    },
                )
            )
            if unmet:
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=actor.workspace_id,
                        actor_type=ActorType.USER,
                        actor_id=actor.user_id,
                        action=AuditAction.RELEASE_GATE_OVERRIDDEN,
                        subject_type="corpus_release",
                        subject_id=release_id,
                        previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                        reason_code="admin_override",
                        metadata={
                            "reason": reason[:500],
                            "unmet": [status.key for status in unmet],
                        },
                    )
                )
            await uow.audit.add_outbox(
                _outbox_event(
                    context,
                    workspace_id=actor.workspace_id,
                    topic="corpus_release.published",
                    payload={"release_id": str(release_id), "alias": alias},
                )
            )
            await uow.commit()
        return current

    async def rollback(
        self, actor: ActorContext, *, alias: str = DEFAULT_ALIAS, reason: str = "operator_rollback"
    ) -> SearchAlias:
        """Point the alias back at the previous release. No reindex, no rewrite."""
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.APPROVE_RELEASE)
        context = self._context
        async with context.uow_factory() as uow:
            current = await uow.releases.get_alias(actor.workspace_id, alias)
            if current is None:
                raise NotFound(f"alias {alias} does not exist")
            rolled_back_from = current.release_id
            target = current.rollback(by=actor.user_id, at=context.now())
            await uow.releases.upsert_alias(current)
            restored = await uow.releases.get(actor.workspace_id, target)
            if restored is not None and restored.status is ReleaseState.SUPERSEDED:
                restored.status = ReleaseState.PUBLISHED
                restored.superseded_by_id = None
                restored.version += 1
                await uow.releases.update(restored)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEASE_ROLLED_BACK,
                    subject_type="search_alias",
                    subject_id=None,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=reason,
                    metadata={
                        "alias": alias,
                        "from_release_id": str(rolled_back_from) if rolled_back_from else None,
                        "to_release_id": str(target),
                    },
                )
            )
            await uow.commit()
        return current

    async def withdraw(
        self, actor: ActorContext, *, release_id: UUID, reason: str = "operator_withdrawal"
    ) -> CorpusRelease:
        """Take a release out of service permanently.

        ``withdrawn`` is terminal, so a release withdrawn after a defect cannot be
        republished by someone who did not read the incident. It refuses while an
        alias still points at the release: withdrawing what is being served would
        leave readers on a release nobody may publish. Roll the alias back first.
        """
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.APPROVE_RELEASE)
        context = self._context
        async with context.uow_factory() as uow:
            release = await uow.releases.get(actor.workspace_id, release_id)
            if release is None:
                raise NotFound(f"release {release_id} not found")
            serving = [
                alias.alias
                for alias in await uow.releases.list_aliases(actor.workspace_id)
                if alias.release_id == release_id
            ]
            if serving:
                raise StateTransitionError(
                    "release is still served by an alias; roll the alias back first",
                    context={"aliases": serving},
                )
            release.transition_to(ReleaseState.WITHDRAWN, at=context.now())
            await uow.releases.update(release)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEASE_WITHDRAWN,
                    subject_type="corpus_release",
                    subject_id=release_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=reason,
                    metadata={"manifest_sha256": release.manifest_sha256},
                )
            )
            await uow.commit()
        return release

    async def delete_draft(self, actor: ActorContext, *, release_id: UUID) -> None:
        """Delete a release that was never published and that nothing points at.

        ``draft``, ``validated``, ``rejected``, and ``approved`` while it has never
        been published: an approved release is indexed but has not been served, so
        nothing was answered from it. Its search index goes with it. From
        publishing on, a release is what answers and exports cite, and it stays
        (withdraw it instead). Any release stays while another release names it as
        its parent, an alias serves it, a chat is pinned to it, an evaluation ran
        against it, or a task naming it is still queued or running; a finished task
        that names it (its indexing) goes with it. Its manifest and validation
        report stay in the object store.
        """
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.MANAGE_RELEASE)
        context = self._context
        async with context.uow_factory() as uow:
            release = await uow.releases.get(actor.workspace_id, release_id)
            if release is None:
                raise NotFound(f"release {release_id} not found")
            never_published = release.published_at is None
            if (
                release.status
                not in (
                    ReleaseState.DRAFT,
                    ReleaseState.VALIDATED,
                    ReleaseState.REJECTED,
                    ReleaseState.APPROVED,
                )
                or not never_published
            ):
                raise StateTransitionError(
                    f"a {release.status.value} release cannot be deleted; only one that "
                    "was never published can (withdraw a published one instead)",
                    context={"status": release.status.value},
                )
            others = await collect_pages(
                lambda after, size: uow.releases.list_all_releases(
                    actor.workspace_id, limit=size, after_id=after
                )
            )
            if any(
                release_id in (other.parent_release_id, other.superseded_by_id) for other in others
            ):
                raise StateTransitionError("another release is built on this one")
            if any(
                alias.release_id == release_id
                for alias in await uow.releases.list_aliases(actor.workspace_id)
            ):
                raise StateTransitionError("this release is served by an alias")
            if await uow.evaluations.list_runs(actor.workspace_id, release_id=release_id, limit=1):
                raise StateTransitionError(
                    "an evaluation ran against this release; delete that run first"
                )
            if any(
                session.pinned_release_id == release_id
                for session in await every_session(uow, actor.workspace_id)
            ):
                raise StateTransitionError("a conversation is pinned to this release")
            naming = [
                task
                for task in await uow.tasks.list_tasks(actor.workspace_id, limit=100_000)
                if str(task.inputs.get("release_id")) == str(release_id)
            ]
            if any(not task.state.is_settled for task in naming):
                raise StateTransitionError(
                    "a background task for this release is still queued or running"
                )
            await uow.tasks.delete_tasks(actor.workspace_id, [task.id for task in naming])
            await uow.releases.delete(actor.workspace_id, release_id)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEASE_DRAFT_DELETED,
                    subject_type="corpus_release",
                    subject_id=release_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=(
                        "approved_unpublished_deleted"
                        if release.status is ReleaseState.APPROVED
                        else "draft_deleted"
                    ),
                    metadata={
                        "name": release.name,
                        "semantic_version": release.semantic_version,
                        "status": release.status.value,
                        "approved_by": str(release.approved_by) if release.approved_by else None,
                        "manifest_sha256": release.manifest_sha256,
                    },
                )
            )
            await uow.commit()
        # An approved release was indexed: its search projections go too.
        if context.search_index is not None:
            await context.search_index.delete_release_projections(actor.workspace_id, release_id)

    async def active_release_id(
        self, workspace_id: UUID, *, alias: str = DEFAULT_ALIAS
    ) -> UUID | None:
        async with self._context.uow_factory() as uow:
            current = await uow.releases.get_alias(workspace_id, alias)
        return current.release_id if current else None


async def _verified_span_ids(
    uow, workspace_id: UUID, span_ids: tuple[UUID, ...]
) -> frozenset[UUID]:
    """Which of these span ids still resolve in this workspace.

    A plain comprehension cannot await, and an async generator silently is not a
    set — so this is a function rather than an inline expression.
    """
    verified: set[UUID] = set()
    for span_id in span_ids:
        if await uow.renditions.get_span(workspace_id, span_id) is not None:
            verified.add(span_id)
    return frozenset(verified)


def _outbox_event(context: ServiceContext, *, workspace_id: UUID, topic: str, payload: dict):
    from lab_assistant.contracts.events import OutboxEvent

    return OutboxEvent(
        id=context.new_id(),
        workspace_id=workspace_id,
        topic=topic,
        payload=payload,
        created_at=context.now(),
    )
