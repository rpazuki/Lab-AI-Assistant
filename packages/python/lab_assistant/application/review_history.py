"""A collection's review history: what was done, by whom, under which scope.

Every service that changes what a collection contains records one step here, in
the same transaction as the change: a scope approved, a run frozen, its records
reconciled, a scope's rules re-applied, a filter or merge rule applied, one paper
decided. A bulk action applied in chunks is one step, which each chunk extends.

Each step names the scope version in force. For a step that applies, approves or
searches a version, that is the version itself; for a review decision it is the
workspace's most recently approved version, which is the question the decision
answers. The decisions themselves stay in ``identity_decisions`` and the audit
chain: this is the readable account, not the evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.events import AuditAction
from lab_assistant.domain.identity import IdentityDecisionType
from lab_assistant.domain.review import ReviewStep, ReviewStepKind, WorkSummary
from lab_assistant.domain.scope import ApprovalState

__all__ = [
    "IN_FORCE",
    "ReviewHistoryService",
    "record_step",
    "scope_in_force",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: ``record_step(scope_version_id=IN_FORCE)``: look up the version in force.
IN_FORCE: Any = object()


async def scope_in_force(uow: Any, workspace_id: UUID) -> UUID | None:
    """The workspace's most recently approved scope version, if any."""
    approved = [
        scope
        for scope in await uow.configs.list_scope_versions(workspace_id)
        if scope.approval_state is ApprovalState.APPROVED
    ]
    if not approved:
        return None
    latest = max(approved, key=lambda s: (s.frozen_at or s.created_at or _EPOCH, s.version))
    return latest.id


async def record_step(
    uow: Any,
    context: ServiceContext,
    *,
    workspace_id: UUID,
    kind: ReviewStepKind,
    actor_id: UUID | None,
    scope_version_id: UUID | None = IN_FORCE,
    rule: dict[str, Any] | None = None,
    counts: dict[str, int] | None = None,
    work_ids: Sequence[UUID] = (),
    subject_id: UUID | None = None,
    note: str | None = None,
    step_id: UUID | None = None,
) -> ReviewStep:
    """Open a step, or with ``step_id`` extend the one an earlier chunk opened."""
    now = context.now()
    if step_id is not None:
        existing = await uow.works.get_review_step(workspace_id, step_id)
        if existing is not None and existing.kind is kind:
            existing.add_counts(counts or {})
            existing.finished_at = now
            await uow.works.update_review_step(existing)
            return existing
    if scope_version_id is IN_FORCE:
        scope_version_id = await scope_in_force(uow, workspace_id)
    step = ReviewStep(
        id=context.new_id(),
        workspace_id=workspace_id,
        kind=kind,
        scope_version_id=scope_version_id,
        actor_id=actor_id,
        rule=rule or {},
        counts={key: int(value) for key, value in (counts or {}).items()},
        work_ids=tuple(work_ids),
        subject_id=subject_id,
        note=note,
        started_at=now,
        finished_at=now,
    )
    await uow.works.add_review_step(step)
    return step


class ReviewHistoryService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def page(
        self,
        actor: ActorContext,
        *,
        scope_version_id: UUID | None,
        kinds: Sequence[str] | None,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> tuple[list[ReviewStep], int, dict[UUID, WorkSummary], dict[UUID, str]]:
        """One page of steps, newest first, with the papers and people they name."""
        async with self._context.uow_factory() as uow:
            steps = await uow.works.list_review_steps(
                actor.workspace_id,
                scope_version_id=scope_version_id,
                kinds=kinds,
                limit=limit,
                before=before,
            )
            total = await uow.works.count_review_steps(
                actor.workspace_id, scope_version_id=scope_version_id, kinds=kinds
            )
            ids = {work_id for step in steps for work_id in step.work_ids}
            summaries = await uow.works.get_summaries(actor.workspace_id, list(ids))
            people: dict[UUID, str] = {}
            for user_id in {step.actor_id for step in steps if step.actor_id is not None}:
                user = await uow.users.get(user_id)
                if user is not None:
                    people[user_id] = user.full_name or user.email_display
        return steps, total, summaries, people

    async def backfill(self, workspace_id: UUID) -> int:
        """Record the steps taken before history was kept. Only on an empty history.

        From what is stored: scope approvals, frozen runs, reconciliation passes
        (their audit events carry the counts), and every merge, dismissal and
        relevance decision a person made. Returns how many steps it recorded.
        """
        context = self._context
        async with context.uow_factory() as uow:
            if await uow.works.count_review_steps(workspace_id) > 0:
                return 0
            steps: list[ReviewStep] = []

            def add(kind: ReviewStepKind, at: datetime, **fields: Any) -> None:
                steps.append(
                    ReviewStep(
                        id=context.new_id(),
                        workspace_id=workspace_id,
                        kind=kind,
                        started_at=at,
                        finished_at=at,
                        note="recorded from earlier decisions",
                        **fields,
                    )
                )

            scopes = await uow.configs.list_scope_versions(workspace_id)
            approved = sorted(
                (s for s in scopes if s.frozen_at is not None),
                key=lambda s: s.frozen_at,  # type: ignore[arg-type, return-value]
            )
            for scope in approved:
                add(
                    ReviewStepKind.SCOPE_APPROVED,
                    scope.frozen_at,  # type: ignore[arg-type]
                    scope_version_id=scope.id,
                    actor_id=scope.reviewed_by,
                    subject_id=scope.id,
                    rule={"name": scope.name, "version": scope.version},
                )

            def in_force(at: datetime) -> UUID | None:
                earlier = [s for s in approved if s.frozen_at and s.frozen_at <= at]
                return earlier[-1].id if earlier else None

            runs = {
                run.id: run for run in await uow.observations.list_runs(workspace_id, limit=10_000)
            }
            for run in runs.values():
                if run.frozen_at is None:
                    continue
                add(
                    ReviewStepKind.DISCOVERY_FROZEN,
                    run.frozen_at,
                    scope_version_id=run.scope_version_id,
                    subject_id=run.id,
                    counts={
                        key: int(value)
                        for key, value in (run.metrics or {}).items()
                        if key in ("records_seen", "connectors", "degraded_accepted")
                        and isinstance(value, int | float)
                    },
                )
            for event in await uow.audit.list_events(
                workspace_id, action=AuditAction.IDENTITY_DECIDED.value, limit=10_000
            ):
                if event.reason_code != "reconciliation_pass" or event.subject_id not in runs:
                    continue
                run = runs[event.subject_id]
                add(
                    ReviewStepKind.RECONCILED,
                    event.occurred_at,
                    scope_version_id=run.scope_version_id,
                    subject_id=run.id,
                    counts={
                        key: int(value)
                        for key, value in (event.metadata or {}).items()
                        if isinstance(value, int | float)
                    },
                )

            kinds = {
                IdentityDecisionType.MERGE: ReviewStepKind.MERGED,
                IdentityDecisionType.REJECT_LINK: ReviewStepKind.DISMISSED,
                IdentityDecisionType.SET_RELEVANCE: ReviewStepKind.RELEVANCE_SET,
            }
            for decision in await uow.works.list_decisions(workspace_id):
                if decision.decided_by_type != "human":
                    continue
                kind = kinds.get(decision.decision_type)
                if kind is None or decision.decided_at is None:
                    continue
                if kind is ReviewStepKind.RELEVANCE_SET and decision.reason_code == "steward_rule":
                    # One of many a filter made: the filter itself was the step.
                    continue
                add(
                    kind,
                    decision.decided_at,
                    scope_version_id=in_force(decision.decided_at),
                    actor_id=decision.decided_by,
                    work_ids=decision.subject_work_ids,
                    subject_id=decision.id,
                    rule={"reason": decision.reason_code, **_after(decision.after)},
                )
            for step in steps:
                await uow.works.add_review_step(step)
            await uow.commit()
        return len(steps)


def _after(after: dict[str, Any]) -> dict[str, Any]:
    return {key: after[key] for key in ("relevance",) if key in after}
