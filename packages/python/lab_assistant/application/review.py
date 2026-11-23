"""Stage 7: review and curation.

Review decisions are immutable. A correction creates a new claim revision *and* a
new review, both pointing at the exact revision that was reviewed — reviewing
"the claim" would be ambiguous the moment a second revision existed.

Reviewer edits use optimistic concurrency: a stale read is rejected rather than
silently overwriting someone else's correction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import collect_pages
from lab_assistant.contracts.errors import (
    ConcurrencyConflict,
    NotFound,
    ValidationFailure,
)
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.extraction import ProposalSource
from lab_assistant.domain.claims import (
    Claim,
    ClaimRevision,
    EpistemicStatus,
    Review,
    ReviewDecision,
)
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.states import ReviewState
from lab_assistant.domain.workspace import Capability

__all__ = ["ClaimReviewItem", "ReviewService"]


@dataclass(frozen=True, slots=True)
class ClaimReviewItem:
    """One row of the claim review queue, with everything the screen needs."""

    claim: Claim
    revision: ClaimRevision
    evidence: tuple[dict[str, Any], ...]
    work_id: UUID
    conflicting_revision_ids: tuple[UUID, ...]
    previous_revisions: tuple[UUID, ...]


class ReviewService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def queue(
        self,
        actor: ActorContext,
        *,
        work_id: UUID | None = None,
        limit: int = 50,
        include_invalid: bool = False,
        after_id: UUID | None = None,
    ) -> list[ClaimReviewItem]:
        """Claims awaiting review, one page by claim id after ``after_id``.

        The queue is filtered in the query, so a page is full whenever that many
        claims wait: filtering after the limit would show a short page, or none,
        while hundreds were waiting behind approved ones.
        """
        actor.require(Capability.REVIEW_CLAIMS)
        context = self._context
        items: list[ClaimReviewItem] = []
        async with context.uow_factory() as uow:
            claims = await uow.claims.list_review_queue(
                actor.workspace_id,
                work_id=work_id,
                include_invalid=include_invalid,
                limit=limit,
                after_id=after_id,
            )
            for claim in claims:
                revisions = await uow.claims.list_revisions(actor.workspace_id, claim.id)
                if not revisions:
                    continue
                current = next(
                    (r for r in revisions if r.id == claim.current_revision_id), revisions[-1]
                )
                if current.review_status.is_approved:
                    continue
                if not include_invalid and not current.is_valid:
                    continue
                evidence: list[dict[str, Any]] = []
                for span_id in current.evidence_span_ids:
                    span = await uow.renditions.get_span(actor.workspace_id, span_id)
                    if span is None:
                        continue
                    evidence.append(
                        {
                            "span_id": str(span.id),
                            "rendition_id": str(span.rendition_id),
                            "node_id": span.node_id,
                            "quote_preview": span.quote_preview,
                            "locator": span.locator,
                            "start_offset": span.start_offset,
                            "end_offset": span.end_offset,
                            "normalisation_version": span.normalisation_version,
                        }
                    )
                siblings = [
                    c
                    for c in await uow.claims.list_claims(actor.workspace_id, work_id=claim.work_id)
                    if c.id != claim.id and c.predicate == claim.predicate
                ]
                conflicting = tuple(
                    c.current_revision_id for c in siblings if c.current_revision_id is not None
                )
                items.append(
                    ClaimReviewItem(
                        claim=claim,
                        revision=current,
                        evidence=tuple(evidence),
                        work_id=claim.work_id,
                        conflicting_revision_ids=conflicting,
                        previous_revisions=tuple(r.id for r in revisions if r.id != current.id),
                    )
                )
        return items

    async def queue_size(
        self, actor: ActorContext, *, work_id: UUID | None = None, include_invalid: bool = False
    ) -> int:
        """How many claims ``queue`` would page through."""
        actor.require(Capability.REVIEW_CLAIMS)
        async with self._context.uow_factory() as uow:
            return await uow.claims.count_review_queue(
                actor.workspace_id, work_id=work_id, include_invalid=include_invalid
            )

    async def decide(
        self,
        actor: ActorContext,
        *,
        revision_id: UUID,
        decision: ReviewDecision,
        reason_code: str,
        comment: str | None = None,
        corrected_value: dict[str, Any] | None = None,
        expected_revision_number: int | None = None,
        reviewer_interpretation: bool = False,
    ) -> tuple[ClaimRevision, Review]:
        """Accept, correct, reject, defer, dispute, or request a reparse."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.REVIEW_CLAIMS)
        context = self._context
        async with context.uow_factory() as uow:
            revision = await uow.claims.get_revision(actor.workspace_id, revision_id)
            if revision is None:
                raise NotFound(f"claim revision {revision_id} not found")
            claim = await uow.claims.get_claim(actor.workspace_id, revision.claim_id)
            if claim is None:
                raise NotFound(f"claim {revision.claim_id} not found")
            if (
                expected_revision_number is not None
                and revision.revision_number != expected_revision_number
            ):
                raise ConcurrencyConflict(
                    "this claim changed since it was read",
                    context={
                        "expected": expected_revision_number,
                        "actual": revision.revision_number,
                    },
                )

            target_revision = revision
            if decision is ReviewDecision.CORRECT:
                if corrected_value is None:
                    raise ValidationFailure("a correction must supply the corrected value")
                revisions = await uow.claims.list_revisions(actor.workspace_id, claim.id)
                target_revision = ClaimRevision(
                    id=context.new_id(),
                    workspace_id=actor.workspace_id,
                    claim_id=claim.id,
                    revision_number=max(r.revision_number for r in revisions) + 1,
                    value=corrected_value,
                    #: A correction keeps the machine's evidence when the reviewer
                    #: is fixing a value the span supports; it loses it when the
                    #: reviewer is authoring an interpretation.
                    evidence_span_ids=() if reviewer_interpretation else revision.evidence_span_ids,
                    proposal_source=ProposalSource.HUMAN,
                    extractor_fingerprint=None,
                    validation_status="valid",
                    review_status=ReviewState.CORRECTED,
                    abstention_reason=revision.abstention_reason,
                    epistemic_status=(
                        EpistemicStatus.REVIEWER_INTERPRETATION
                        if reviewer_interpretation
                        else EpistemicStatus.ASSERTED_BY_SOURCE
                    ),
                    created_by=actor.user_id,
                    created_at=context.now(),
                    supersedes_revision_id=revision.id,
                )
                await uow.claims.add_revision(target_revision)
                revision.review_status = ReviewState.CORRECTED
                await uow.claims.update_revision(revision)
                claim.current_revision_id = target_revision.id
                claim.object_value = corrected_value
                claim.lifecycle_status = "active"
            elif decision is ReviewDecision.ACCEPT:
                if not revision.is_valid:
                    raise ValidationFailure(
                        "an invalid proposal cannot be accepted; correct it or reject it"
                    )
                revision.review_status = ReviewState.ACCEPTED
                await uow.claims.update_revision(revision)
                claim.lifecycle_status = "active"
            elif decision is ReviewDecision.REJECT:
                revision.review_status = ReviewState.REJECTED
                await uow.claims.update_revision(revision)
                claim.lifecycle_status = "withdrawn"
            elif decision is ReviewDecision.DEFER:
                revision.review_status = ReviewState.DEFERRED
                await uow.claims.update_revision(revision)
            elif decision is ReviewDecision.DISPUTE:
                revision.review_status = ReviewState.DISPUTED
                await uow.claims.update_revision(revision)
            elif decision is ReviewDecision.REQUEST_REPARSE:
                revision.review_status = ReviewState.DEFERRED
                await uow.claims.update_revision(revision)

            claim.version += 1
            await uow.claims.update_claim(claim)

            review = Review(
                id=context.new_id(),
                workspace_id=actor.workspace_id,
                subject_type="claim_revision",
                subject_id=revision.id,
                revision_id=revision.id,
                decision=decision,
                before={"value": revision.value, "review_status": revision.review_status.value},
                after={"value": target_revision.value},
                reason_code=reason_code,
                comment=comment,
                reviewer_id=actor.user_id,
                reviewed_at=context.now(),
            )
            await uow.claims.add_review(review)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.CLAIM_REVIEWED,
                    subject_type="claim_revision",
                    subject_id=revision.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=reason_code,
                    metadata={
                        "decision": decision.value,
                        "claim_id": str(claim.id),
                        "new_revision_id": (
                            str(target_revision.id) if target_revision.id != revision.id else None
                        ),
                        "reviewer_interpretation": reviewer_interpretation,
                    },
                )
            )
            await uow.commit()
        return target_revision, review

    async def correction_rate(
        self, actor: ActorContext, *, predicate: str | None = None
    ) -> dict[str, float]:
        """Measured correction rate by predicate.

        This is the input to the staged double-review decision: double review is
        for predicates whose correction rate exceeds the template threshold, not
        for everything by default (scale §3.2).
        """
        context = self._context
        totals: dict[str, int] = {}
        corrections: dict[str, int] = {}
        async with context.uow_factory() as uow:
            # A correction *rate* computed over a truncated sample is a wrong number
            # presented as a measurement, and it gates whether double review turns on.
            claims = await collect_pages(
                lambda after, size: uow.claims.list_claims(
                    actor.workspace_id, limit=size, after_id=after
                )
            )
            for claim in claims:
                if predicate is not None and claim.predicate != predicate:
                    continue
                revisions = await uow.claims.list_revisions(actor.workspace_id, claim.id)
                reviewed = [r for r in revisions if r.review_status is not ReviewState.UNREVIEWED]
                if not reviewed:
                    continue
                totals[claim.predicate] = totals.get(claim.predicate, 0) + 1
                if any(r.review_status is ReviewState.CORRECTED for r in reviewed):
                    corrections[claim.predicate] = corrections.get(claim.predicate, 0) + 1
        return {
            key: corrections.get(key, 0) / count for key, count in sorted(totals.items()) if count
        }
