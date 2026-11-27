"""Letting existing rights decisions copy content to mirrors, by the current policy.

A rights decision keeps the actions it was made with (ADR-0026). So when the data
steward turns on ``LAB_RIGHTS_REPLICATE_OPEN_CONTENT``, papers decided before
that carry on without ``replicate_content``: their text and search index are
withheld from every snapshot, and a mirror holds their records and cannot answer
from them.

This applies the current rights policy to those decisions again, and only for
that one action:

- **It only adds.** A decision gains ``replicate_content`` when the current
  policy grants it for the decision's own licence and access basis. Nothing is
  taken away and no other action changes; narrowing has its own path
  (``RefreshService.revoke_rights``).
- **It never revives anything.** A prohibited or undecided decision, or one past
  its review date, is left as it is: a steward decides those, not a rule.
- **It changes the decision in place,** keeping its id, as a revocation does.
  Releases point at decisions by id, so a published release keeps its content
  and its digest; what changes is what the next snapshot may carry.
- **A dry run is the default.** ``apply`` writes, with one audit event for the
  run; each changed decision records who re-decided it and when.

A mirror gets the text with the next ``scripts/archive.sh publish`` and sync.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.states import RightsState
from lab_assistant.domain.workspace import Capability
from lab_assistant.policy.rights import RightsRequest

__all__ = ["REAPPLIED_NOTE", "ReapplyReport", "RightsReapplyService"]

REPLICATE = PolicyAction.REPLICATE_CONTENT

#: What a widened decision's note says, so the change can be found later.
REAPPLIED_NOTE = "replicate_content_reapplied"

#: Why a decision was left as it was.
SKIP_ALREADY = "already_granted"
SKIP_NOT_PERMITTED = "not_permitted"
SKIP_PAST_REVIEW = "past_review_date"
SKIP_POLICY = "policy_does_not_grant"


@dataclass
class ReapplyReport:
    applied: bool
    policy: str
    examined: int = 0
    #: Decisions that gain ``replicate_content`` (or would, in a dry run).
    widened: int = 0
    widened_by_licence: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    #: Licences of the decisions the policy does not grant replication to.
    not_granted_by_licence: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "policy": self.policy,
            "examined": self.examined,
            "widened": self.widened,
            "widened_by_licence": dict(self.widened_by_licence.most_common()),
            "skipped": dict(self.skipped.most_common()),
            "not_granted_by_licence": dict(self.not_granted_by_licence.most_common()),
        }


def _licence(decision: Any) -> str:
    return decision.licence_expression or "none recorded"


class RightsReapplyService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def _grants_replication(self, decision: Any) -> bool:
        """Whether the current policy would grant it for this decision's own inputs."""
        verdict = self._context.rights_policy.evaluate(
            RightsRequest(
                workspace_id=decision.workspace_id,
                subject_type=decision.subject_type,
                subject_id=decision.subject_id,
                licence_expression=decision.licence_expression,
                access_basis=decision.access_basis,
                requested_actions=frozenset({REPLICATE}),
                now=self._context.now(),
            )
        )
        return REPLICATE in verdict.allowed_actions

    async def reapply(self, actor: ActorContext, *, apply: bool = False) -> ReapplyReport:
        """Add ``replicate_content`` where the current policy grants it.

        A dry run unless ``apply``: the report says what would change.
        """
        context = self._context
        context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.DECIDE_RIGHTS)
        policy = context.rights_policy
        report = ReapplyReport(
            applied=apply, policy=f"{policy.policy_id}@{policy.semantic_version}"
        )
        now = context.now()
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            decisions = await uow.policies.list_rights_decisions(ws)
            widened = []
            for decision in sorted(decisions, key=lambda d: str(d.id)):
                report.examined += 1
                if REPLICATE in decision.allowed_actions:
                    report.skipped[SKIP_ALREADY] += 1
                elif decision.status not in (RightsState.PERMITTED, RightsState.RESTRICTED):
                    report.skipped[SKIP_NOT_PERMITTED] += 1
                elif decision.is_expired(now):
                    report.skipped[SKIP_PAST_REVIEW] += 1
                elif not self._grants_replication(decision):
                    report.skipped[SKIP_POLICY] += 1
                    report.not_granted_by_licence[_licence(decision)] += 1
                else:
                    report.widened += 1
                    report.widened_by_licence[_licence(decision)] += 1
                    widened.append(decision)
            if not apply or not widened:
                return report
            for decision in widened:
                await uow.policies.add_rights_decision(
                    decision.model_copy(
                        update={
                            "allowed_actions": decision.allowed_actions | {REPLICATE},
                            "decided_by": actor.user_id,
                            "decided_by_type": "human_steward",
                            "decided_at": now,
                            "note": REAPPLIED_NOTE,
                        }
                    )
                )
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RIGHTS_DECIDED,
                    subject_type="workspace",
                    subject_id=ws,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code=REAPPLIED_NOTE,
                    metadata={
                        "policy": report.policy,
                        "widened": report.widened,
                        "widened_by_licence": dict(report.widened_by_licence),
                    },
                )
            )
            await uow.commit()
        return report
