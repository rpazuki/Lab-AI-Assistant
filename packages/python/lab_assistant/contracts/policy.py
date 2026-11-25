"""Policy contract: actions, subjects, and decision records.

Rights are not one licence string. A decision answers "may this subject perform
this action on this resource in this workspace, and on what evidence".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "CONTENT_ACTIONS",
    "Effect",
    "GrantSpec",
    "PolicyAction",
    "PolicyDecision",
    "ResourceRef",
    "SubjectRef",
    "SubjectType",
]


class PolicyAction(StrEnum):
    """Every action the policy engine can be asked about.

    Split deliberately: permission to store metadata is not permission to store
    content, and permission to process content locally is not permission to
    send it to an external model.
    """

    # discovery / metadata
    DISCOVER_METADATA = "discover_metadata"
    STORE_METADATA = "store_metadata"
    VIEW_METADATA = "view_metadata"
    # content
    STORE_CONTENT = "store_content"
    PROCESS_LOCAL = "process_local"
    VIEW_CONTENT = "view_content"
    DISPLAY_EXCERPT = "display_excerpt"
    DISPLAY_FULL_CONTENT = "display_full_content"
    # model transfer
    SEND_TO_EXTERNAL_MODEL = "send_to_external_model"
    CREATE_EMBEDDINGS_LOCAL = "create_embeddings_local"
    CREATE_EMBEDDINGS_EXTERNAL = "create_embeddings_external"
    # serving
    INDEX = "index"
    QUERY = "query"
    # collaboration and export
    SHARE_WITH_GROUP = "share_with_group"
    EXPORT_METADATA = "export_metadata"
    EXPORT_CLAIMS = "export_claims"
    EXPORT_EVIDENCE = "export_evidence"
    EXPORT_CONTENT = "export_content"
    #: Hold a copy outside this installation: in the shared archive, and on the
    #: readers that import from it. Its own action because the architecture
    #: says so explicitly — permission to *store* content here is not
    #: permission to replicate it to other machines, and reading
    #: `store_content` as both is how licensed text ends up in every reader's
    #: database. It covers the payloads and everything that is their text in
    #: another shape: renditions, search chunks and the vectors made from them.
    REPLICATE_CONTENT = "replicate_content"
    # lifecycle control
    REVIEW = "review"
    APPROVE_RELEASE = "approve_release"
    ADMINISTER = "administer"
    RETAIN = "retain"
    DELETE = "delete"


#: Actions that default to deny even for workspace members (data model §4.2).
CONTENT_ACTIONS: frozenset[PolicyAction] = frozenset(
    {
        PolicyAction.STORE_CONTENT,
        PolicyAction.PROCESS_LOCAL,
        PolicyAction.VIEW_CONTENT,
        PolicyAction.DISPLAY_EXCERPT,
        PolicyAction.DISPLAY_FULL_CONTENT,
        PolicyAction.SEND_TO_EXTERNAL_MODEL,
        PolicyAction.CREATE_EMBEDDINGS_EXTERNAL,
        PolicyAction.EXPORT_EVIDENCE,
        PolicyAction.EXPORT_CONTENT,
        PolicyAction.REPLICATE_CONTENT,
    }
)


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_REVIEW = "require_review"


class SubjectType(StrEnum):
    USER = "user"
    GROUP = "group"
    WORKSPACE = "workspace"
    SERVICE_ACCOUNT = "service_account"
    #: Everyone holding one role. A membership has exactly one, so a role grant
    #: is the shortest way to say "reviewers may ask about this scope" without
    #: naming each reviewer, and it keeps saying it as people come and go.
    ROLE = "role"


class ResourceType(StrEnum):
    WORKSPACE = "workspace"
    WORK = "work"
    ASSET = "asset"
    RENDITION = "rendition"
    CLAIM = "claim"
    RELEASE = "release"
    EXPORT = "export"
    TEMPLATE = "template"
    SCOPE = "scope"
    CHAT_SESSION = "chat_session"


class SubjectRef(BaseModel):
    model_config = {"frozen": True}

    subject_type: SubjectType
    subject_id: UUID
    group_ids: tuple[UUID, ...] = ()

    def matches(self, other_type: SubjectType, other_id: UUID) -> bool:
        if other_type is self.subject_type and other_id == self.subject_id:
            return True
        return other_type is SubjectType.GROUP and other_id in self.group_ids


class ResourceRef(BaseModel):
    model_config = {"frozen": True}

    resource_type: ResourceType
    resource_id: UUID | None = None
    #: Group restrictions attached to the resource, if any.
    restricted_to_groups: tuple[UUID, ...] = ()
    #: Sensitivity label carried from the rights decision.
    sensitivity: str = "internal"


class GrantSpec(BaseModel):
    """One normalised grant row (data model §4.2)."""

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    subject_type: SubjectType
    #: Who it speaks about, for every subject type but ``role``.
    subject_id: UUID | None = None
    #: The role it speaks about, for ``subject_type=role`` only. A role has no
    #: id of its own — it is a name on a membership — so it is named, not
    #: referenced, and the pair is mutually exclusive.
    subject_role: str | None = None
    resource_type: ResourceType
    resource_id: UUID | None
    action: PolicyAction
    effect: Effect
    policy_basis: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _one_subject(self) -> GrantSpec:
        if self.subject_type is SubjectType.ROLE:
            if self.subject_role is None or self.subject_id is not None:
                raise ValueError("a role grant names a role and no subject id")
        elif self.subject_id is None or self.subject_role is not None:
            raise ValueError(f"a {self.subject_type.value} grant names a subject id, not a role")
        return self

    def is_current(self, now: datetime) -> bool:
        started = self.valid_from is None or now >= self.valid_from
        not_expired = self.valid_until is None or now < self.valid_until
        return started and not_expired


class PolicyDecision(BaseModel):
    """The auditable output of one policy evaluation."""

    model_config = {"frozen": True}

    workspace_id: UUID
    subject: SubjectRef
    resource: ResourceRef
    action: PolicyAction
    effect: Effect
    reason_code: str
    policy_version_id: UUID | None = None
    matched_grant_ids: tuple[UUID, ...] = ()
    conditions: tuple[str, ...] = ()
    decided_at: datetime | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW
