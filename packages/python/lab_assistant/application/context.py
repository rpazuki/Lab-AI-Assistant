"""Composition context and actor resolution.

``ServiceContext`` is the single object every application service takes. It holds
seams (clock, ids, object store, registries) and never a live transaction: each
service opens its own unit of work, so a request's transaction boundary is
explicit.

``ActorContext`` is the resolved caller: user, workspace, membership, groups, and
grants. Services receive it rather than a bare user id, so no service can forget
to load grants before asking the policy engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from lab_assistant.application.ports import (
    ObjectStore,
    SearchIndex,
    UnitOfWorkFactory,
)
from lab_assistant.application.registries import (
    ConnectorRegistry,
    MalwareScanner,
    ParserRegistry,
)
from lab_assistant.config import Settings
from lab_assistant.contracts.errors import InstallationDenied, PolicyDenied
from lab_assistant.contracts.extraction import Extractor
from lab_assistant.contracts.models import EmbeddingProvider, LLMProvider
from lab_assistant.contracts.policy import GrantSpec, PolicyAction, SubjectRef, SubjectType
from lab_assistant.contracts.runtime import Clock, IdFactory
from lab_assistant.domain.installation import InstallationCapability, InstanceCapabilities
from lab_assistant.domain.workspace import Capability, Membership
from lab_assistant.policy.engine import PolicyEngine, PolicyInput
from lab_assistant.policy.model_transfer import ModelTransferPolicy
from lab_assistant.policy.rights import RightsPolicy
from lab_assistant.provenance.audit import AuditTrail

__all__ = [
    "DEFAULT_MEMBER_ACTIONS",
    "ActorContext",
    "ServiceContext",
    "resolve_actor",
]


#: Metadata-level actions a workspace member may take without a specific grant.
#: Content actions are deliberately absent: they default to deny.
DEFAULT_MEMBER_ACTIONS: frozenset[PolicyAction] = frozenset(
    {
        PolicyAction.VIEW_METADATA,
        PolicyAction.DISCOVER_METADATA,
        PolicyAction.QUERY,
        PolicyAction.EXPORT_METADATA,
    }
)


@dataclass(frozen=True, slots=True)
class ServiceContext:
    """Everything a service needs that is not request-specific."""

    uow_factory: UnitOfWorkFactory
    object_store: ObjectStore
    clock: Clock
    id_factory: IdFactory
    policy_engine: PolicyEngine
    rights_policy: RightsPolicy
    model_transfer_policy: ModelTransferPolicy
    settings: Settings
    search_index: SearchIndex | None = None
    connectors: ConnectorRegistry | None = None
    parsers: ParserRegistry | None = None
    llm_provider: LLMProvider | None = None
    embedding_provider: EmbeddingProvider | None = None
    extractor: Extractor | None = None
    malware_scanner: MalwareScanner | None = None
    #: What this *deployment* may do, as distinct from what the caller may do.
    #: Defaults to a publisher so an installation that predates the setting
    #: keeps every capability it had, rather than silently losing one.
    installation: InstanceCapabilities = field(default_factory=InstanceCapabilities)

    def require_installation(self, capability: InstallationCapability) -> None:
        """Refuse an operation this deployment is not for.

        Raised as `InstallationDenied`, never `PolicyDenied`: no grant fixes it
        and no promotion fixes it, because the limit is the machine rather than
        the caller. A client that confused the two would offer somebody a
        permissions dialog for something permissions cannot change.
        """
        if not self.installation.has(capability):
            raise InstallationDenied(
                f"this installation does not {capability.value.replace('_', ' ')}",
                context={
                    "installation_role": self.installation.role.value,
                    "required_capability": capability.value,
                    "hint": "the capability belongs to the publisher installation",
                },
            )

    @property
    def audit(self) -> AuditTrail:
        return AuditTrail(id_factory=self.id_factory, clock=self.clock)

    def now(self) -> datetime:
        return self.clock()

    def new_id(self) -> UUID:
        return self.id_factory()

    def require_search_index(self) -> SearchIndex:
        if self.search_index is None:
            raise RuntimeError("this operation needs a search index in the service context")
        return self.search_index

    def require_connectors(self) -> ConnectorRegistry:
        if self.connectors is None:
            raise RuntimeError("this operation needs a connector registry in the service context")
        return self.connectors

    def require_parsers(self) -> ParserRegistry:
        if self.parsers is None:
            raise RuntimeError("this operation needs a parser registry in the service context")
        return self.parsers

    def require_llm(self) -> LLMProvider:
        if self.llm_provider is None:
            raise RuntimeError("this operation needs an LLM provider in the service context")
        return self.llm_provider

    def require_embeddings(self) -> EmbeddingProvider:
        if self.embedding_provider is None:
            raise RuntimeError("this operation needs an embedding provider in the service context")
        return self.embedding_provider


@dataclass(frozen=True, slots=True)
class ActorContext:
    """The resolved caller of an operation."""

    user_id: UUID
    workspace_id: UUID
    membership: Membership | None
    group_ids: tuple[UUID, ...] = ()
    grants: tuple[GrantSpec, ...] = ()
    subject_type: SubjectType = SubjectType.USER
    request_id: str | None = None
    workspace_default_actions: frozenset[PolicyAction] = field(
        default_factory=lambda: DEFAULT_MEMBER_ACTIONS
    )

    @property
    def subject(self) -> SubjectRef:
        return SubjectRef(
            subject_type=self.subject_type,
            subject_id=self.user_id,
            group_ids=self.group_ids,
        )

    def policy_input(self, *, now: datetime, policy_version_id: UUID | None = None) -> PolicyInput:
        return PolicyInput(
            workspace_id=self.workspace_id,
            subject=self.subject,
            membership=self.membership,
            grants=self.grants,
            now=now,
            policy_version_id=policy_version_id,
            workspace_default_actions=self.workspace_default_actions,
        )

    def has(self, capability: Capability) -> bool:
        return self.membership is not None and self.membership.has(capability)

    def require(self, capability: Capability) -> None:
        if not self.has(capability):
            raise PolicyDenied(
                f"missing capability {capability.value}",
                context={"capability": capability.value},
            )


async def resolve_actor(
    context: ServiceContext,
    *,
    user_id: UUID,
    workspace_id: UUID,
    request_id: str | None = None,
    subject_type: SubjectType = SubjectType.USER,
) -> ActorContext:
    """Load membership, groups, and grants for one caller."""
    async with context.uow_factory() as uow:
        membership = await uow.workspaces.get_membership(workspace_id, user_id)
        groups = await uow.workspaces.list_groups(workspace_id, user_id=user_id)
        group_ids = tuple(group.id for group in groups)
        # The caller's own grants, their groups', the workspace-wide ones, and
        # the ones written for whatever role their membership carries.
        grants = await uow.policies.list_grants(
            workspace_id,
            subject_ids=[user_id, workspace_id, *group_ids],
            roles=[membership.role.value] if membership is not None else [],
        )
    return ActorContext(
        user_id=user_id,
        workspace_id=workspace_id,
        membership=membership,
        group_ids=group_ids,
        grants=tuple(grants),
        request_id=request_id,
        subject_type=subject_type,
    )
