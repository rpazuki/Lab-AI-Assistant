"""Policy engine, rights policy, and model-transfer policy.

The properties under test are the ones the whole safety story rests on: content
actions default to deny, an explicit deny always wins, removing a grant only ever
removes permission, and group-restricted material needs a grant naming the group.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from lab_assistant.contracts.policy import (
    CONTENT_ACTIONS,
    Effect,
    GrantSpec,
    PolicyAction,
    ResourceRef,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.assets import AcquisitionRoute
from lab_assistant.domain.states import RightsState
from lab_assistant.domain.workspace import (
    Capability,
    Membership,
    MembershipStatus,
    Role,
)
from lab_assistant.policy.engine import PolicyEngine, PolicyInput
from lab_assistant.policy.model_transfer import ModelTransferPolicy, TransferRequest
from lab_assistant.policy.rights import RightsRequest, default_open_access_policy

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")
USER = UUID("00000000-0000-4000-8000-000000000002")
GROUP = UUID("00000000-0000-4000-8000-000000000003")
WORK = UUID("00000000-0000-4000-8000-000000000004")


_NO_MEMBERSHIP = object()


def _membership(role: Role = Role.RESEARCHER, **overrides: object) -> Membership:
    fields: dict[str, object] = {
        "id": uuid4(),
        "workspace_id": WORKSPACE,
        "user_id": USER,
        "role": role,
        "status": MembershipStatus.ACTIVE,
    }
    fields.update(overrides)
    return Membership(**fields)  # type: ignore[arg-type]


def _input(
    *,
    membership: Membership | object | None = _NO_MEMBERSHIP,
    grants: tuple[GrantSpec, ...] = (),
    group_ids: tuple[UUID, ...] = (),
    defaults: frozenset[PolicyAction] = frozenset(),
) -> PolicyInput:
    from lab_assistant.contracts.policy import SubjectRef

    return PolicyInput(
        workspace_id=WORKSPACE,
        subject=SubjectRef(subject_type=SubjectType.USER, subject_id=USER, group_ids=group_ids),
        membership=_membership() if membership is _NO_MEMBERSHIP else membership,  # type: ignore[arg-type]
        grants=grants,
        now=NOW,
        workspace_default_actions=defaults,
    )


def _grant(
    action: PolicyAction,
    *,
    effect: Effect = Effect.ALLOW,
    subject_type: SubjectType = SubjectType.USER,
    subject_id: UUID = USER,
    resource_type: ResourceType = ResourceType.WORKSPACE,
    resource_id: UUID | None = WORKSPACE,
    valid_until: datetime | None = None,
) -> GrantSpec:
    return GrantSpec(
        id=uuid4(),
        workspace_id=WORKSPACE,
        subject_type=subject_type,
        subject_id=subject_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        effect=effect,
        policy_basis="test",
        valid_until=valid_until,
    )


WORK_RESOURCE = ResourceRef(resource_type=ResourceType.WORK, resource_id=WORK)


class TestDefaultDeny:
    def test_membership_alone_does_not_grant_content_actions(self) -> None:
        engine = PolicyEngine()
        for action in sorted(CONTENT_ACTIONS, key=lambda a: a.value):
            decision = engine.decide(
                _input(membership=_membership(Role.DATA_STEWARD)),
                action=action,
                resource=WORK_RESOURCE,
            )
            assert not decision.allowed, action
            assert decision.reason_code in (
                "content_action_default_deny",
                "missing_capability",
                "no_matching_grant",
            )

    def test_no_membership_is_denied_before_anything_else(self) -> None:
        decision = PolicyEngine().decide(
            _input(membership=None), action=PolicyAction.QUERY, resource=WORK_RESOURCE
        )
        assert decision.reason_code == "no_active_membership"

    def test_expired_membership_is_not_current(self) -> None:
        membership = _membership(expires_at=NOW - timedelta(days=1))
        decision = PolicyEngine().decide(
            _input(membership=membership), action=PolicyAction.QUERY, resource=WORK_RESOURCE
        )
        assert not decision.allowed

    def test_capability_is_required_before_a_grant_is_consulted(self) -> None:
        decision = PolicyEngine().decide(
            _input(
                membership=_membership(Role.RESEARCHER),
                grants=(_grant(PolicyAction.APPROVE_RELEASE),),
            ),
            action=PolicyAction.APPROVE_RELEASE,
            resource=ResourceRef(resource_type=ResourceType.RELEASE, resource_id=uuid4()),
        )
        assert decision.reason_code == "missing_capability"


class TestGrants:
    def test_explicit_grant_allows(self) -> None:
        decision = PolicyEngine().decide(
            _input(grants=(_grant(PolicyAction.QUERY),)),
            action=PolicyAction.QUERY,
            resource=WORK_RESOURCE,
        )
        assert decision.allowed
        assert decision.matched_grant_ids

    def test_explicit_deny_beats_allow_regardless_of_order(self) -> None:
        allow = _grant(PolicyAction.QUERY)
        deny = _grant(PolicyAction.QUERY, effect=Effect.DENY)
        for grants in ((allow, deny), (deny, allow)):
            decision = PolicyEngine().decide(
                _input(grants=grants), action=PolicyAction.QUERY, resource=WORK_RESOURCE
            )
            assert decision.reason_code == "explicit_deny"

    def test_removing_a_grant_only_removes_permission(self) -> None:
        engine = PolicyEngine()
        grants = (_grant(PolicyAction.QUERY), _grant(PolicyAction.VIEW_CONTENT))
        with_grants = _input(grants=grants)
        assert engine.allows(with_grants, action=PolicyAction.QUERY, resource=WORK_RESOURCE)
        without = _input(grants=(grants[0],))
        assert engine.allows(without, action=PolicyAction.QUERY, resource=WORK_RESOURCE)
        assert not engine.allows(without, action=PolicyAction.VIEW_CONTENT, resource=WORK_RESOURCE)

    def test_expired_grant_stops_working(self) -> None:
        expired = _grant(PolicyAction.QUERY, valid_until=NOW - timedelta(seconds=1))
        assert not PolicyEngine().allows(
            _input(grants=(expired,)), action=PolicyAction.QUERY, resource=WORK_RESOURCE
        )

    def test_resource_scoped_grant_does_not_leak_to_other_resources(self) -> None:
        other = uuid4()
        grant = _grant(PolicyAction.QUERY, resource_type=ResourceType.WORK, resource_id=other)
        assert not PolicyEngine().allows(
            _input(grants=(grant,)), action=PolicyAction.QUERY, resource=WORK_RESOURCE
        )

    def test_review_required_is_neither_allow_nor_deny(self) -> None:
        decision = PolicyEngine().decide(
            _input(
                membership=_membership(Role.DATA_STEWARD),
                grants=(_grant(PolicyAction.PROCESS_LOCAL, effect=Effect.REQUIRE_REVIEW),),
            ),
            action=PolicyAction.PROCESS_LOCAL,
            resource=WORK_RESOURCE,
        )
        assert decision.effect is Effect.REQUIRE_REVIEW
        assert not decision.allowed


class TestGroupRestrictions:
    RESTRICTED = ResourceRef(
        resource_type=ResourceType.WORK, resource_id=WORK, restricted_to_groups=(GROUP,)
    )

    def test_workspace_grant_is_not_enough_for_restricted_material(self) -> None:
        workspace_grant = _grant(
            PolicyAction.QUERY, subject_type=SubjectType.WORKSPACE, subject_id=WORKSPACE
        )
        assert not PolicyEngine().allows(
            _input(grants=(workspace_grant,)),
            action=PolicyAction.QUERY,
            resource=self.RESTRICTED,
        )

    def test_group_member_with_a_group_grant_may_query(self) -> None:
        group_grant = _grant(PolicyAction.QUERY, subject_type=SubjectType.GROUP, subject_id=GROUP)
        assert PolicyEngine().allows(
            _input(grants=(group_grant,), group_ids=(GROUP,)),
            action=PolicyAction.QUERY,
            resource=self.RESTRICTED,
        )

    def test_non_member_with_the_same_group_grant_is_denied(self) -> None:
        group_grant = _grant(PolicyAction.QUERY, subject_type=SubjectType.GROUP, subject_id=GROUP)
        assert not PolicyEngine().allows(
            _input(grants=(group_grant,)),
            action=PolicyAction.QUERY,
            resource=self.RESTRICTED,
        )

    def test_filter_visible_returns_the_denials(self) -> None:
        engine = PolicyEngine()
        allowed_work, restricted_work = uuid4(), uuid4()
        resources = {
            allowed_work: ResourceRef(resource_type=ResourceType.WORK, resource_id=allowed_work),
            restricted_work: ResourceRef(
                resource_type=ResourceType.WORK,
                resource_id=restricted_work,
                restricted_to_groups=(GROUP,),
            ),
        }
        kept, denials = engine.filter_visible(
            _input(grants=(_grant(PolicyAction.QUERY),)),
            action=PolicyAction.QUERY,
            items=[allowed_work, restricted_work],
            resource_of=lambda work_id: resources[work_id],
        )
        assert kept == [allowed_work]
        assert len(denials) == 1
        assert denials[0].reason_code == "resource_restricted_to_groups"


class TestWorkspaceDefaults:
    def test_defaults_apply_to_metadata_actions_only(self) -> None:
        engine = PolicyEngine()
        defaults = frozenset({PolicyAction.VIEW_METADATA, PolicyAction.VIEW_CONTENT})
        assert engine.allows(
            _input(defaults=defaults),
            action=PolicyAction.VIEW_METADATA,
            resource=WORK_RESOURCE,
        )
        # A bundle that claims a content action still cannot grant it by default.
        assert not engine.allows(
            _input(defaults=defaults),
            action=PolicyAction.VIEW_CONTENT,
            resource=WORK_RESOURCE,
        )


class TestRolesAndCapabilities:
    def test_reviewer_can_review_without_being_an_administrator(self) -> None:
        reviewer = _membership(Role.REVIEWER)
        assert reviewer.has(Capability.REVIEW_CLAIMS)
        assert not reviewer.has(Capability.MANAGE_POLICY)

    def test_steward_can_run_the_lifecycle_but_not_manage_members(self) -> None:
        steward = _membership(Role.DATA_STEWARD)
        assert steward.has(Capability.RUN_DISCOVERY)
        assert steward.has(Capability.APPROVE_RELEASE)
        assert not steward.has(Capability.MANAGE_MEMBERS)

    def test_extra_capabilities_extend_a_role(self) -> None:
        researcher = _membership(
            Role.RESEARCHER, extra_capabilities=frozenset({Capability.REVIEW_CLAIMS})
        )
        assert researcher.has(Capability.REVIEW_CLAIMS)

    def test_suspended_membership_has_no_capabilities(self) -> None:
        suspended = _membership(Role.ADMIN, status=MembershipStatus.SUSPENDED)
        assert suspended.capabilities() == frozenset()


class TestRightsPolicy:
    POLICY = default_open_access_policy()

    def _request(self, **overrides: object) -> RightsRequest:
        base: dict[str, object] = {
            "workspace_id": WORKSPACE,
            "subject_type": "manifestation",
            "subject_id": WORK,
            "licence_expression": "cc-by-4.0",
            "route": AcquisitionRoute.OPEN_ACCESS_API,
            "requested_actions": frozenset(
                {PolicyAction.STORE_CONTENT, PolicyAction.PROCESS_LOCAL}
            ),
            "now": NOW,
        }
        base.update(overrides)
        return RightsRequest(**base)  # type: ignore[arg-type]

    def test_open_licence_permits_local_processing(self) -> None:
        verdict = self.POLICY.evaluate(self._request())
        assert verdict.state in (RightsState.PERMITTED, RightsState.RESTRICTED)
        assert PolicyAction.PROCESS_LOCAL in verdict.allowed_actions
        assert verdict.review_due_at is not None

    def test_unknown_licence_requires_review_rather_than_guessing(self) -> None:
        verdict = self.POLICY.evaluate(self._request(licence_expression="unknown"))
        assert verdict.needs_steward
        assert PolicyAction.PROCESS_LOCAL in verdict.review_actions

    def test_a_downloadable_url_is_not_permission(self) -> None:
        verdict = self.POLICY.evaluate(
            self._request(
                licence_expression="all-rights-reserved",
                access_basis="https://publisher.invalid/paper.pdf",
            )
        )
        assert PolicyAction.STORE_CONTENT not in verdict.allowed_actions

    def test_assisted_upload_needs_an_attestation(self) -> None:
        without = self.POLICY.evaluate(
            self._request(licence_expression=None, route=AcquisitionRoute.USER_ASSISTED)
        )
        assert PolicyAction.PROCESS_LOCAL not in without.allowed_actions
        with_attestation = self.POLICY.evaluate(
            self._request(
                licence_expression=None,
                route=AcquisitionRoute.USER_ASSISTED,
                user_attestation="institutional subscription copy",
            )
        )
        assert PolicyAction.PROCESS_LOCAL in with_attestation.allowed_actions
        assert "no external model transfer" in with_attestation.restrictions

    def test_external_transfer_needs_an_approved_processor(self) -> None:
        verdict = self.POLICY.evaluate(
            self._request(
                requested_actions=frozenset({PolicyAction.SEND_TO_EXTERNAL_MODEL}),
                external_processor="some-provider",
            )
        )
        assert PolicyAction.SEND_TO_EXTERNAL_MODEL not in verdict.allowed_actions


class TestModelTransferPolicy:
    def test_restricted_classification_falls_back_to_local(self) -> None:
        policy = ModelTransferPolicy(approved_providers=("anthropic",))
        verdict = policy.evaluate(
            TransferRequest(
                workspace_id=WORKSPACE,
                granted_actions=frozenset(
                    {PolicyAction.PROCESS_LOCAL, PolicyAction.SEND_TO_EXTERNAL_MODEL}
                ),
                classification="restricted",
                now=NOW,
            )
        )
        assert not verdict.external_allowed
        assert verdict.outcome.value == "allow_local_only"
        assert "local" in verdict.user_message.lower()

    def test_unapproved_provider_is_refused(self) -> None:
        from lab_assistant.contracts.models import ProviderProperties

        policy = ModelTransferPolicy(approved_providers=("anthropic",))
        verdict = policy.evaluate(
            TransferRequest(
                workspace_id=WORKSPACE,
                granted_actions=frozenset(
                    {PolicyAction.PROCESS_LOCAL, PolicyAction.SEND_TO_EXTERNAL_MODEL}
                ),
                classification="public",
                provider=ProviderProperties(provider_id="other"),
                now=NOW,
            )
        )
        assert verdict.reason_code == "provider_not_approved"

    def test_training_terms_block_transfer(self) -> None:
        from lab_assistant.contracts.models import ProviderProperties

        policy = ModelTransferPolicy(approved_providers=("anthropic",))
        verdict = policy.evaluate(
            TransferRequest(
                workspace_id=WORKSPACE,
                granted_actions=frozenset(
                    {PolicyAction.PROCESS_LOCAL, PolicyAction.SEND_TO_EXTERNAL_MODEL}
                ),
                classification="public",
                provider=ProviderProperties(provider_id="anthropic", trains_on_data=True),
                now=NOW,
            )
        )
        assert verdict.reason_code == "provider_trains_on_data"

    def test_message_never_leaks_policy_internals(self) -> None:
        policy = ModelTransferPolicy(approved_providers=("anthropic",))
        verdict = policy.evaluate(
            TransferRequest(
                workspace_id=WORKSPACE,
                granted_actions=frozenset({PolicyAction.PROCESS_LOCAL}),
                classification="confidential",
                now=NOW,
            )
        )
        assert "confidential" not in verdict.user_message
        assert verdict.evidence["classification"] == "confidential"


@pytest.mark.parametrize("action", sorted(PolicyAction, key=lambda a: a.value))
def test_every_action_has_a_capability_mapping(action: PolicyAction) -> None:
    """A new action without a capability would be silently ungated."""
    from lab_assistant.policy.engine import ACTION_CAPABILITY

    assert action in ACTION_CAPABILITY, action
