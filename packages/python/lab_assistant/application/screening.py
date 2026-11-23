"""Re-applying an approved scope version's screening rules to the candidates.

A frozen scope cannot change, so changing the rules means a new version: add
concepts and exclusion terms, approve it, then apply it here. Like every bulk
action it is a preview, which changes nothing and says what would change and
why, then an apply in chunks that the page repeats until nothing remains.

What the rules may change: a candidate still unreviewed, or one an earlier rule
decided (reconciliation, or an earlier application of a scope's rules). What they
never change: a candidate a person decided, one by one or with a filter, and a
merged record. Each change is a decision that names the scope version and the
reason ("concept_score:1.00", "exclusion_term:plant"), and the whole application
is one step in the review history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.bulk_review import RELEVANCE_CHUNK, paper
from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import collect_pages
from lab_assistant.application.review_history import record_step
from lab_assistant.contracts.errors import NotFound, StateTransitionError, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.domain.identity import IdentityDecision, IdentityDecisionType, Work
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.review import ReviewStepKind, WorkSummary
from lab_assistant.domain.scope import ApprovalState, ScopeVersion
from lab_assistant.domain.screening import reason_group, score_for_scope
from lab_assistant.domain.states import RelevanceState
from lab_assistant.domain.workspace import Capability

__all__ = ["REASONS", "ScreeningPlan", "ScreeningService", "scope_rules"]

#: What each reason means, for a preview a person reads.
REASONS: dict[str, str] = {
    "concept_score": "decided by the share of the scope's concepts the paper mentions",
    "no_abstract_concept_score": (
        "too few concepts in the title, and no abstract to read: left for review"
    ),
    "uncertain_concept_score": "some concepts present, not enough to decide: left for review",
    "no_scope_concepts_defined": "the scope defines no concepts, so its rules decide nothing",
}


def describe_reason(reason: str) -> str:
    if reason.startswith("exclusion_term:"):
        return f"mentions the exclusion term “{reason.split(':', 1)[1]}”"
    return REASONS.get(reason, reason)


def scope_rules(scope: ScopeVersion) -> dict[str, Any]:
    """The rules as stored with every decision and step they produce."""
    policy = scope.relevance_policy
    return {
        "scope_version_id": str(scope.id),
        "name": scope.name,
        "version": scope.version,
        "concepts": list(scope.concepts),
        "exclusion_terms": list(scope.exclusion_terms),
        "include_threshold": policy.include_threshold,
        "exclude_threshold": policy.exclude_threshold,
        "allow_included_uncertain": policy.allow_included_uncertain,
    }


@dataclass(slots=True)
class ScreeningPlan:
    """What applying a scope version's rules would change now."""

    scope: ScopeVersion
    #: Active candidates in the workspace.
    considered: int = 0
    #: Decided by a person: left alone.
    decided_by_person: int = 0
    #: Not yet indexed for the rules (the backfill has not reached them).
    not_indexed: int = 0
    #: What the rules say, over every candidate they may decide.
    outcome: dict[str, int] = field(default_factory=dict)
    #: (relevance, reason group) -> count, over every candidate they may decide.
    reasons: dict[tuple[str, str], int] = field(default_factory=dict)
    #: "unreviewed>excluded" -> count, over the changes only.
    transitions: dict[str, int] = field(default_factory=dict)
    #: Each change: the work, its new relevance, and why. Ordered by work id.
    changes: list[tuple[Work, RelevanceState, str]] = field(default_factory=list)
    samples: dict[str, list[tuple[Work, str]]] = field(default_factory=dict)
    summaries: dict[UUID, WorkSummary] = field(default_factory=dict)

    @property
    def unchanged(self) -> int:
        return sum(self.outcome.values()) - len(self.changes)

    def view(self) -> dict[str, Any]:
        return {
            "scope": scope_rules(self.scope),
            "considered": self.considered,
            "decided_by_person": self.decided_by_person,
            "not_indexed": self.not_indexed,
            "unchanged": self.unchanged,
            "changes": len(self.changes),
            "outcome": self.outcome,
            "transitions": self.transitions,
            "reasons": [
                {
                    "relevance": relevance,
                    "reason": reason,
                    "description": describe_reason(reason),
                    "count": count,
                }
                for (relevance, reason), count in sorted(
                    self.reasons.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "samples": {
                relevance: [
                    {
                        **paper(work.id, self.summaries.get(work.id), work),
                        "reason": describe_reason(reason_group(reason)),
                    }
                    for work, reason in rows
                ]
                for relevance, rows in self.samples.items()
            },
            "no_concepts": not self.scope.concepts,
        }


class ScreeningService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def _approved(
        self, uow: Any, actor: ActorContext, scope_version_id: UUID
    ) -> ScopeVersion:
        scope = await uow.configs.get_scope_version(actor.workspace_id, scope_version_id)
        if scope is None:
            raise NotFound(f"scope version {scope_version_id} not found")
        if scope.approval_state is not ApprovalState.APPROVED:
            raise StateTransitionError(
                "only an approved scope version's rules decide candidates; approve it first"
            )
        return scope

    async def plan(self, actor: ActorContext, scope_version_id: UUID) -> ScreeningPlan:
        """Score every candidate the rules may decide. Changes nothing."""
        async with self._context.uow_factory() as uow:
            scope = await self._approved(uow, actor, scope_version_id)
            works = await collect_pages(
                lambda after, size: uow.works.list_works(
                    actor.workspace_id, limit=size, after_id=after
                ),
                page_size=5_000,
            )
            active = [work for work in works if work.is_active]
            protected = await uow.works.human_relevance_work_ids(actor.workspace_id)
            summaries = await uow.works.get_summaries(
                actor.workspace_id, [w.id for w in active if w.id not in protected]
            )

        plan = ScreeningPlan(scope=scope, considered=len(active))
        for work in sorted(active, key=lambda w: str(w.id)):
            if work.id in protected:
                plan.decided_by_person += 1
                continue
            summary = summaries.get(work.id)
            if summary is None or summary.relevance_text is None:
                plan.not_indexed += 1
                continue
            relevance, reason = score_for_scope(
                summary.relevance_text, scope, has_abstract=summary.has_abstract
            )
            plan.outcome[relevance.value] = plan.outcome.get(relevance.value, 0) + 1
            key = (relevance.value, reason_group(reason))
            plan.reasons[key] = plan.reasons.get(key, 0) + 1
            if relevance is work.relevance:
                continue
            plan.changes.append((work, relevance, reason))
            transition = f"{work.relevance.value}>{relevance.value}"
            plan.transitions[transition] = plan.transitions.get(transition, 0) + 1
            examples = plan.samples.setdefault(relevance.value, [])
            if len(examples) < 5:
                examples.append((work, reason))
                plan.summaries[work.id] = summary
        return plan

    async def apply(
        self, actor: ActorContext, scope_version_id: UUID, *, step_id: UUID | None = None
    ) -> dict[str, Any]:
        """Make up to ``RELEVANCE_CHUNK`` of the plan's changes; report what is left."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.DECIDE_IDENTITY)
        plan = await self.plan(actor, scope_version_id)
        if not plan.scope.concepts:
            raise ValidationFailure(
                "this scope version defines no concepts, so its rules decide nothing; "
                "save a new version with concepts and approve it"
            )
        context = self._context
        chosen = plan.changes[:RELEVANCE_CHUNK]
        rules = scope_rules(plan.scope)
        counts: dict[str, int] = {}
        async with context.uow_factory() as uow:
            now = context.now()
            for planned, relevance, reason in chosen:
                # Re-read: the plan was made outside this transaction.
                work = await uow.works.get(actor.workspace_id, planned.id)
                if work is None or not work.is_active or work.relevance is not planned.relevance:
                    continue
                before = work.relevance
                work.relevance = relevance
                work.version += 1
                await uow.works.update(work)
                await uow.works.add_decision(
                    IdentityDecision(
                        id=context.new_id(),
                        workspace_id=actor.workspace_id,
                        decision_type=IdentityDecisionType.SET_RELEVANCE,
                        subject_work_ids=(work.id,),
                        before={"relevance": before.value},
                        after={
                            "relevance": relevance.value,
                            "scope_version_id": str(plan.scope.id),
                            "scope_version": plan.scope.version,
                        },
                        reason_code=reason,
                        decided_by=actor.user_id,
                        decided_by_type="scope_rule",
                        decided_at=now,
                    )
                )
                counts[relevance.value] = counts.get(relevance.value, 0) + 1
            applied = sum(counts.values())
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.SCOPE_RULES_APPLIED,
                    subject_type="scope_version",
                    subject_id=plan.scope.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="scope_rules",
                    metadata={"changed": counts, "rules": rules},
                )
            )
            step = await record_step(
                uow,
                context,
                workspace_id=actor.workspace_id,
                kind=ReviewStepKind.SCOPE_RULES_APPLIED,
                actor_id=actor.user_id,
                scope_version_id=plan.scope.id,
                rule=rules,
                counts=counts,
                subject_id=plan.scope.id,
                step_id=step_id,
            )
            await uow.commit()
        return {
            "applied": applied,
            "remaining": max(len(plan.changes) - len(chosen), 0),
            "step_id": str(step.id),
        }
