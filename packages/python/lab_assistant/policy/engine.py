"""The grant and capability engine.

Two properties matter more than features here:

* **default deny for content actions** — membership alone never grants
  ``view_content``, ``send_to_external_model``, or ``export_evidence``;
* **monotonicity** — removing a grant can only ever remove permissions. There is
  no rule whose absence grants something.

Retrieval calls ``allows`` *before* scoring and again *after* ranking. A UUID
being hard to guess is not access control.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel

from lab_assistant.contracts.policy import (
    CONTENT_ACTIONS,
    Effect,
    GrantSpec,
    PolicyAction,
    PolicyDecision,
    ResourceRef,
    ResourceType,
    SubjectRef,
    SubjectType,
)
from lab_assistant.domain.workspace import Capability, Membership

__all__ = ["ACTION_CAPABILITY", "PolicyEngine", "PolicyInput"]


#: Capability required before a grant is even consulted. Capabilities express
#: "what this person's role lets them do"; grants express "on which resources".
ACTION_CAPABILITY: dict[PolicyAction, Capability] = {
    PolicyAction.DISCOVER_METADATA: Capability.SEARCH,
    PolicyAction.STORE_METADATA: Capability.RUN_DISCOVERY,
    PolicyAction.VIEW_METADATA: Capability.SEARCH,
    PolicyAction.STORE_CONTENT: Capability.ACQUIRE,
    PolicyAction.PROCESS_LOCAL: Capability.ACQUIRE,
    PolicyAction.VIEW_CONTENT: Capability.SEARCH,
    PolicyAction.DISPLAY_EXCERPT: Capability.ASK,
    PolicyAction.DISPLAY_FULL_CONTENT: Capability.SEARCH,
    PolicyAction.SEND_TO_EXTERNAL_MODEL: Capability.RUN_EXTRACTION,
    PolicyAction.CREATE_EMBEDDINGS_LOCAL: Capability.MANAGE_RELEASE,
    PolicyAction.CREATE_EMBEDDINGS_EXTERNAL: Capability.MANAGE_RELEASE,
    PolicyAction.INDEX: Capability.MANAGE_RELEASE,
    PolicyAction.QUERY: Capability.ASK,
    PolicyAction.SHARE_WITH_GROUP: Capability.MANAGE_MEMBERS,
    PolicyAction.EXPORT_METADATA: Capability.EXPORT,
    PolicyAction.EXPORT_CLAIMS: Capability.EXPORT,
    PolicyAction.EXPORT_EVIDENCE: Capability.EXPORT,
    PolicyAction.EXPORT_CONTENT: Capability.EXPORT,
    PolicyAction.REVIEW: Capability.REVIEW_CLAIMS,
    PolicyAction.APPROVE_RELEASE: Capability.APPROVE_RELEASE,
    PolicyAction.ADMINISTER: Capability.MANAGE_POLICY,
    PolicyAction.RETAIN: Capability.MANAGE_POLICY,
    PolicyAction.DELETE: Capability.MANAGE_POLICY,
}

T = TypeVar("T")


class PolicyInput(BaseModel):
    """Everything one evaluation needs. No repository access from the engine."""

    model_config = {"frozen": True}

    workspace_id: UUID
    subject: SubjectRef
    membership: Membership | None
    grants: tuple[GrantSpec, ...] = ()
    now: datetime
    policy_version_id: UUID | None = None
    #: Actions the workspace policy bundle allows by default for members. Content
    #: actions are excluded by construction, whatever a bundle claims.
    workspace_default_actions: frozenset[PolicyAction] = frozenset()


class PolicyEngine:
    """Pure evaluation over supplied grants. Deterministic and side-effect free."""

    def __init__(self, *, strict_content_default_deny: bool = True) -> None:
        self._strict = strict_content_default_deny

    def decide(
        self,
        request: PolicyInput,
        *,
        action: PolicyAction,
        resource: ResourceRef,
    ) -> PolicyDecision:
        def deny(reason: str, **evidence: object) -> PolicyDecision:
            return PolicyDecision(
                workspace_id=request.workspace_id,
                subject=request.subject,
                resource=resource,
                action=action,
                effect=Effect.DENY,
                reason_code=reason,
                policy_version_id=request.policy_version_id,
                decided_at=request.now,
                evidence=dict(evidence),
            )

        membership = request.membership
        if membership is None or not membership.is_current(request.now):
            return deny("no_active_membership")

        required = ACTION_CAPABILITY.get(action)
        if required is not None and not membership.has(required):
            return deny("missing_capability", required_capability=required.value)

        # An explicit deny always wins, regardless of order or specificity.
        matching = [
            grant
            for grant in request.grants
            if grant.workspace_id == request.workspace_id
            and grant.action is action
            and grant.is_current(request.now)
            and self._subject_matches(request, grant)
            and self._resource_matches(grant, resource)
        ]
        explicit_denies = [g for g in matching if g.effect is Effect.DENY]
        if explicit_denies:
            return deny(
                "explicit_deny",
                grant_ids=[str(g.id) for g in explicit_denies],
            )

        reviews = [g for g in matching if g.effect is Effect.REQUIRE_REVIEW]
        allows = [g for g in matching if g.effect is Effect.ALLOW]

        # Group-restricted material needs a grant that names one of the resource's
        # groups, or a grant to this subject for *this specific resource*. A
        # blanket workspace-wide grant is not a share of restricted material — that
        # distinction is the whole point of restricting it.
        if resource.restricted_to_groups:
            authorised = any(
                (
                    grant.subject_type is SubjectType.GROUP
                    and grant.subject_id in resource.restricted_to_groups
                )
                or (
                    grant.subject_type in (SubjectType.USER, SubjectType.SERVICE_ACCOUNT)
                    and grant.resource_id is not None
                    and grant.resource_id == resource.resource_id
                )
                for grant in allows
            )
            if not authorised:
                return deny(
                    "resource_restricted_to_groups",
                    restricted_to=[str(g) for g in resource.restricted_to_groups],
                )

        if allows:
            return PolicyDecision(
                workspace_id=request.workspace_id,
                subject=request.subject,
                resource=resource,
                action=action,
                effect=Effect.ALLOW,
                reason_code="explicit_grant",
                policy_version_id=request.policy_version_id,
                matched_grant_ids=tuple(g.id for g in allows),
                conditions=tuple(sorted({g.policy_basis for g in allows})),
                decided_at=request.now,
            )

        if reviews:
            return PolicyDecision(
                workspace_id=request.workspace_id,
                subject=request.subject,
                resource=resource,
                action=action,
                effect=Effect.REQUIRE_REVIEW,
                reason_code="requires_steward_review",
                policy_version_id=request.policy_version_id,
                matched_grant_ids=tuple(g.id for g in reviews),
                decided_at=request.now,
            )

        if action in CONTENT_ACTIONS and self._strict:
            return deny("content_action_default_deny")

        if action in request.workspace_default_actions:
            return PolicyDecision(
                workspace_id=request.workspace_id,
                subject=request.subject,
                resource=resource,
                action=action,
                effect=Effect.ALLOW,
                reason_code="workspace_default",
                policy_version_id=request.policy_version_id,
                decided_at=request.now,
            )

        return deny("no_matching_grant")

    def allows(self, request: PolicyInput, *, action: PolicyAction, resource: ResourceRef) -> bool:
        return self.decide(request, action=action, resource=resource).allowed

    def filter_visible(
        self,
        request: PolicyInput,
        *,
        action: PolicyAction,
        items: Sequence[T],
        resource_of: Callable[[T], ResourceRef],
    ) -> tuple[list[T], list[PolicyDecision]]:
        """Filter a candidate list, returning the denials for audit.

        Used by retrieval both before scoring and after ranking. Returning the
        denials rather than discarding them is what makes the leakage probes
        meaningful: a denial that never appears anywhere cannot be counted.
        """
        kept: list[T] = []
        denials: list[PolicyDecision] = []
        for item in items:
            decision = self.decide(request, action=action, resource=resource_of(item))
            if decision.allowed:
                kept.append(item)
            else:
                denials.append(decision)
        return kept, denials

    @staticmethod
    def _subject_matches(request: PolicyInput, grant: GrantSpec) -> bool:
        """Whether ``grant`` speaks about this caller.

        A grant to the workspace itself is the materialised form of "every member
        may do this" (data model §4.2). It is matched here for members, and it is
        deliberately *not* enough to unlock group-restricted material — that check
        is separate and demands a grant naming the group or the exact resource.
        """
        if grant.subject_type is SubjectType.WORKSPACE and grant.subject_id == request.workspace_id:
            return True
        return request.subject.matches(grant.subject_type, grant.subject_id)

    @staticmethod
    def _resource_matches(grant: GrantSpec, resource: ResourceRef) -> bool:
        if grant.resource_type is ResourceType.WORKSPACE:
            return True
        if grant.resource_type is not resource.resource_type:
            return False
        if grant.resource_id is None:
            return True
        return grant.resource_id == resource.resource_id


def workspace_scope_guard(expected_workspace_id: UUID, *rows: Iterable[object]) -> None:
    """Assert every row belongs to ``expected_workspace_id``.

    Called by repositories and release validation. A missing workspace filter is
    the single most damaging bug class in a multi-workspace system, so it is
    checked rather than assumed — see ``tests/policy/``.
    """
    for group in rows:
        for row in group:
            row_workspace = getattr(row, "workspace_id", None)
            if row_workspace is not None and row_workspace != expected_workspace_id:
                raise AssertionError(
                    f"row {row!r} belongs to workspace {row_workspace}, expected "
                    f"{expected_workspace_id}"
                )
