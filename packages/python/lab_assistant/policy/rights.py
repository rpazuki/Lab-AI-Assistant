"""Rights evaluation: what may be done with this content, and on what evidence.

The software does not decide copyright law. It executes institution-approved
rules, records the evidence, and routes anything unclear to a human steward. A
downloadable URL is never, by itself, permission.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.policy import Effect, PolicyAction
from lab_assistant.domain.assets import AcquisitionRoute
from lab_assistant.domain.states import RightsState

__all__ = [
    "RightsPolicy",
    "RightsPolicyRule",
    "RightsRequest",
    "RightsVerdict",
]


class RightsRequest(BaseModel):
    """Inputs to one rights evaluation (connectors §4.2)."""

    model_config = {"frozen": True}

    workspace_id: UUID
    subject_type: str
    subject_id: UUID
    licence_expression: str | None = None
    access_basis: str | None = None
    route: AcquisitionRoute | None = None
    content_kind: str = "article"
    publication_state: str | None = None
    sensitivity: str = "public"
    user_attestation: str | None = None
    requested_actions: frozenset[PolicyAction] = frozenset()
    intended_use: str = "internal_research"
    external_processor: str | None = None
    processor_region: str | None = None
    retention_days: int | None = None
    now: datetime | None = None
    evidence_assertion_ids: tuple[UUID, ...] = ()


def licence_key(licence: str | None) -> str:
    """A licence as a rule compares it: lower case, spaces and underscores as hyphens.

    Sources write one licence several ways: ``CC-BY-4.0``, ``cc by``, ``cc_by``,
    ``CC BY-NC-ND``. Read literally, ``cc by`` is not ``cc-by`` and a paper under an
    open licence is treated as unlicensed. Both the licence and a rule's prefixes go
    through this, so a deny rule (``all-rights-reserved``) matches its spellings too.
    """
    return re.sub(r"[\s_]+", "-", (licence or "").strip().lower())


class RightsPolicyRule(BaseModel):
    """One institution-approved rule. Ordered; first match wins per action."""

    model_config = {"frozen": True}

    rule_id: str
    description: str
    #: Match conditions. ``None`` means "any".
    licence_prefixes: tuple[str, ...] = ()
    routes: tuple[AcquisitionRoute, ...] = ()
    content_kinds: tuple[str, ...] = ()
    sensitivities: tuple[str, ...] = ()
    requires_attestation: bool = False
    effect: Effect = Effect.ALLOW
    actions: frozenset[PolicyAction] = frozenset()
    restrictions: tuple[str, ...] = ()
    review_after_days: int | None = None

    def matches(self, request: RightsRequest) -> bool:
        if self.licence_prefixes:
            licence = licence_key(request.licence_expression)
            if not any(licence.startswith(licence_key(p)) for p in self.licence_prefixes):
                return False
        if self.routes and request.route not in self.routes:
            return False
        if self.content_kinds and request.content_kind not in self.content_kinds:
            return False
        if self.sensitivities and request.sensitivity not in self.sensitivities:
            return False
        return not (self.requires_attestation and not request.user_attestation)


class RightsVerdict(BaseModel):
    model_config = {"frozen": True}

    state: RightsState
    allowed_actions: frozenset[PolicyAction]
    denied_actions: frozenset[PolicyAction]
    review_actions: frozenset[PolicyAction]
    restrictions: tuple[str, ...] = ()
    matched_rule_ids: tuple[str, ...] = ()
    review_due_at: datetime | None = None
    reason_code: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def needs_steward(self) -> bool:
        return bool(self.review_actions) or self.state is RightsState.UNKNOWN


class RightsPolicy(BaseModel):
    """A versioned, ordered rule set.

    ``default_effect`` is deny. An action nobody wrote a rule for is not
    permitted; it is a steward question.
    """

    model_config = {"frozen": True}

    policy_id: str
    semantic_version: str
    rules: tuple[RightsPolicyRule, ...] = ()
    default_review_after_days: int | None = 365
    #: Institution-approved external processors. Empty means no external transfer.
    approved_external_processors: tuple[str, ...] = ()
    approved_processor_regions: tuple[str, ...] = ()

    def evaluate(self, request: RightsRequest) -> RightsVerdict:
        now = request.now or datetime.now(tz=UTC)
        requested = request.requested_actions or frozenset(PolicyAction)
        allowed: set[PolicyAction] = set()
        review: set[PolicyAction] = set()
        restrictions: list[str] = []
        matched: list[str] = []
        earliest_review: datetime | None = None

        for rule in self.rules:
            if not rule.matches(request):
                continue
            relevant = rule.actions & requested
            if not relevant:
                continue
            matched.append(rule.rule_id)
            restrictions.extend(rule.restrictions)
            if rule.effect is Effect.ALLOW:
                allowed |= relevant - review
            elif rule.effect is Effect.REQUIRE_REVIEW:
                review |= relevant - allowed
            else:  # explicit deny is final for those actions
                requested = requested - relevant
                allowed -= relevant
                review -= relevant
            days = rule.review_after_days or self.default_review_after_days
            if days is not None:
                candidate = now + timedelta(days=days)
                earliest_review = (
                    candidate if earliest_review is None else min(earliest_review, candidate)
                )

        # External model transfer needs an approved processor *and* region, no
        # matter what a licence rule said.
        for action in (
            PolicyAction.SEND_TO_EXTERNAL_MODEL,
            PolicyAction.CREATE_EMBEDDINGS_EXTERNAL,
        ):
            if action in allowed and not self._processor_approved(request):
                allowed.discard(action)
                review.add(action)
                restrictions.append("external processor or region not approved")

        denied = frozenset(request.requested_actions or PolicyAction) - allowed - review
        if allowed and not review:
            state = RightsState.PERMITTED if not restrictions else RightsState.RESTRICTED
            reason = "matched_allow_rule"
        elif review:
            state = RightsState.UNKNOWN
            reason = "requires_steward_review"
        else:
            state = RightsState.PROHIBITED
            reason = "no_allow_rule_matched"

        return RightsVerdict(
            state=state,
            allowed_actions=frozenset(allowed),
            denied_actions=frozenset(denied),
            review_actions=frozenset(review),
            restrictions=tuple(dict.fromkeys(restrictions)),
            matched_rule_ids=tuple(matched),
            review_due_at=earliest_review,
            reason_code=reason,
            evidence={
                "licence_expression": request.licence_expression,
                "route": request.route.value if request.route else None,
                "access_basis": request.access_basis,
                "attestation": bool(request.user_attestation),
                "assertion_ids": [str(i) for i in request.evidence_assertion_ids],
            },
        )

    def _processor_approved(self, request: RightsRequest) -> bool:
        if request.external_processor is None:
            return False
        if request.external_processor not in self.approved_external_processors:
            return False
        return not (
            self.approved_processor_regions
            and request.processor_region not in self.approved_processor_regions
        )


def default_open_access_policy(
    *,
    policy_id: str = "default-oa",
    version: str = "1.0.0",
    replicate_open_content: bool = False,
) -> RightsPolicy:
    """A conservative starter policy: open licences only, no external transfer.

    Deployments replace this with an institution-approved policy version. It is
    deliberately restrictive so that an unconfigured deployment cannot transfer
    entitled content anywhere.

    ``replicate_open_content`` lets open-licence content be copied to the shared
    archive and its readers (`replicate_content`). Off unless the data steward
    turns it on (`LAB_RIGHTS_REPLICATE_OPEN_CONTENT`): whether a licence permits
    replication to other installations is the steward's decision, and the
    architecture asks for default denial while it is unmade. It is never
    granted to user-assisted or entitled material by this policy.

    An abstract stands in for a paper's full text only by a steward's recorded
    choice: the ``abstract-fallback`` rule needs content kind ``abstract`` and an
    attestation, which the fallback writes as who switched it on and when (see
    ``application/abstract_fallback.py``). It lets the abstract be indexed,
    searched and quoted as an excerpt, never shown whole, exported or sent on.
    """
    open_content = frozenset(
        {
            PolicyAction.STORE_METADATA,
            PolicyAction.VIEW_METADATA,
            PolicyAction.STORE_CONTENT,
            PolicyAction.PROCESS_LOCAL,
            PolicyAction.CREATE_EMBEDDINGS_LOCAL,
            PolicyAction.INDEX,
            PolicyAction.QUERY,
            PolicyAction.DISPLAY_EXCERPT,
            PolicyAction.DISPLAY_FULL_CONTENT,
            PolicyAction.EXPORT_METADATA,
            PolicyAction.EXPORT_CLAIMS,
            PolicyAction.EXPORT_EVIDENCE,
            PolicyAction.RETAIN,
        }
    )
    metadata_only = frozenset(
        {
            PolicyAction.STORE_METADATA,
            PolicyAction.VIEW_METADATA,
            PolicyAction.QUERY,
            PolicyAction.EXPORT_METADATA,
            PolicyAction.RETAIN,
        }
    )
    if replicate_open_content:
        open_content = open_content | {PolicyAction.REPLICATE_CONTENT}
    return RightsPolicy(
        policy_id=policy_id,
        semantic_version=version,
        rules=(
            RightsPolicyRule(
                rule_id="cc-open",
                description="Creative Commons and public-domain licences permit local processing",
                licence_prefixes=("cc-by", "cc0", "cc-by-sa", "cc-by-nc", "public-domain"),
                effect=Effect.ALLOW,
                actions=open_content,
            ),
            RightsPolicyRule(
                rule_id="user-assisted-attested",
                description="User-assisted upload with a recorded access basis",
                routes=(AcquisitionRoute.USER_ASSISTED,),
                requires_attestation=True,
                effect=Effect.ALLOW,
                # Attested access for one person is not a licence to copy the
                # material to every reader, whatever the open rule allows.
                actions=open_content
                - {PolicyAction.EXPORT_EVIDENCE, PolicyAction.REPLICATE_CONTENT},
                restrictions=("no external model transfer", "no content export"),
                review_after_days=180,
            ),
            RightsPolicyRule(
                rule_id="abstract-fallback",
                description=(
                    "A paper's abstract stands in for its full text, by a steward's "
                    "recorded choice, while no full text can be kept"
                ),
                content_kinds=("abstract",),
                requires_attestation=True,
                effect=Effect.ALLOW,
                actions=frozenset(
                    {
                        PolicyAction.STORE_METADATA,
                        PolicyAction.VIEW_METADATA,
                        PolicyAction.STORE_CONTENT,
                        PolicyAction.PROCESS_LOCAL,
                        PolicyAction.CREATE_EMBEDDINGS_LOCAL,
                        PolicyAction.INDEX,
                        PolicyAction.QUERY,
                        PolicyAction.DISPLAY_EXCERPT,
                        PolicyAction.RETAIN,
                    }
                ),
                restrictions=("abstract only: answers say so", "no content export"),
            ),
            RightsPolicyRule(
                rule_id="metadata-fallback",
                description="Metadata and abstract tier where content rights are absent",
                effect=Effect.ALLOW,
                actions=metadata_only,
            ),
            RightsPolicyRule(
                rule_id="entitled-content-review",
                description="Entitled or unclear content requires a steward decision",
                licence_prefixes=("all-rights-reserved", "publisher-specific", "unknown"),
                effect=Effect.REQUIRE_REVIEW,
                actions=frozenset(
                    {
                        PolicyAction.STORE_CONTENT,
                        PolicyAction.PROCESS_LOCAL,
                        PolicyAction.SEND_TO_EXTERNAL_MODEL,
                        PolicyAction.EXPORT_CONTENT,
                        PolicyAction.DISPLAY_FULL_CONTENT,
                    }
                ),
                review_after_days=90,
            ),
        ),
    )
