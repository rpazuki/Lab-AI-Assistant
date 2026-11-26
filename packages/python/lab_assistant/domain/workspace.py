"""Users, workspaces, memberships, groups, and capabilities.

Roles are convenient bundles. Authorisation is capability and resource based, so
a reviewer or data steward can exist without being an administrator.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "BASE_ROLES",
    "Capability",
    "Group",
    "Invitation",
    "Membership",
    "MembershipStatus",
    "Role",
    "User",
    "UserExternalIdentity",
    "Workspace",
    "WorkspaceRole",
    "WorkspaceStatus",
    "normalise_email",
    "normalise_role_slug",
]


def normalise_email(email: str) -> str:
    """Lower-cased, trimmed form used for uniqueness and lookup.

    The display form is stored separately: a user's own capitalisation is theirs
    to keep, and identity providers are keyed by issuer+subject, never by email.
    """
    return email.strip().lower()


class Capability(StrEnum):
    ASK = "ask"
    SEARCH = "search"
    MANAGE_SCOPE = "manage_scope"
    RUN_DISCOVERY = "run_discovery"
    DECIDE_IDENTITY = "decide_identity"
    DECIDE_RIGHTS = "decide_rights"
    ACQUIRE = "acquire"
    UPLOAD_ASSET = "upload_asset"
    REVIEW_PARSE = "review_parse"
    RUN_EXTRACTION = "run_extraction"
    REVIEW_CLAIMS = "review_claims"
    MANAGE_RELEASE = "manage_release"
    APPROVE_RELEASE = "approve_release"
    EXPORT = "export"
    VIEW_ANALYTICS = "view_analytics"
    MANAGE_MEMBERS = "manage_members"
    MANAGE_POLICY = "manage_policy"
    VIEW_OPERATIONS = "view_operations"
    RUN_EVALUATION = "run_evaluation"


class Role(StrEnum):
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"
    DATA_STEWARD = "data_steward"
    ADMIN = "admin"
    SERVICE_ACCOUNT = "service_account"

    @property
    def capabilities(self) -> frozenset[Capability]:
        return ROLE_CAPABILITIES[self]


ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.RESEARCHER: frozenset(
        {
            Capability.ASK,
            Capability.SEARCH,
            Capability.UPLOAD_ASSET,
            Capability.VIEW_ANALYTICS,
        }
    ),
    Role.REVIEWER: frozenset(
        {
            Capability.ASK,
            Capability.SEARCH,
            Capability.UPLOAD_ASSET,
            Capability.VIEW_ANALYTICS,
            Capability.DECIDE_IDENTITY,
            Capability.REVIEW_PARSE,
            Capability.REVIEW_CLAIMS,
        }
    ),
    Role.DATA_STEWARD: frozenset(
        {
            Capability.ASK,
            Capability.SEARCH,
            Capability.UPLOAD_ASSET,
            Capability.VIEW_ANALYTICS,
            Capability.MANAGE_SCOPE,
            Capability.RUN_DISCOVERY,
            Capability.DECIDE_IDENTITY,
            Capability.DECIDE_RIGHTS,
            Capability.ACQUIRE,
            Capability.REVIEW_PARSE,
            Capability.RUN_EXTRACTION,
            Capability.REVIEW_CLAIMS,
            Capability.MANAGE_RELEASE,
            Capability.APPROVE_RELEASE,
            Capability.EXPORT,
            Capability.RUN_EVALUATION,
        }
    ),
    Role.ADMIN: frozenset(Capability),
    Role.SERVICE_ACCOUNT: frozenset(
        {
            Capability.RUN_DISCOVERY,
            Capability.ACQUIRE,
            Capability.RUN_EXTRACTION,
            Capability.MANAGE_RELEASE,
            Capability.RUN_EVALUATION,
        }
    ),
}


#: The built-in roles a workspace role may be based on.
#:
#: ``admin`` is left out because an admin is let through every rule without one,
#: so a role based on it could never be restricted by the very mechanism the role
#: exists for. ``service_account`` is left out because it is not a person.
BASE_ROLES: frozenset[Role] = frozenset({Role.RESEARCHER, Role.REVIEWER, Role.DATA_STEWARD})


def normalise_role_slug(value: str) -> str:
    """The stable key a role is known by, in rules and on memberships.

    Built-in role names and workspace role slugs share one namespace: a rule
    says ``reviewer`` or it says ``patent_holders`` and nothing downstream can
    tell which kind it came from, which is exactly what makes a workspace role
    usable everywhere a built-in one is. So a slug has the same shape as a
    built-in name, and a slug that *is* a built-in name is refused where roles
    are created.

    A slug never changes once the role exists, because it is written into grants
    and onto memberships. The display name is what an admin corrects later.
    """
    slug = value.strip().lower().replace(" ", "_")
    if not slug:
        raise ValueError("a role needs a name")
    if len(slug) > 48:
        raise ValueError("a role name is at most 48 characters")
    if not all(ch.isalnum() or ch in "-_" for ch in slug):
        raise ValueError("a role name uses letters, numbers, - and _ only")
    return slug


class WorkspaceRole(BaseModel):
    """A role an admin defined, for an audience the built-in roles do not name.

    Patented material and confidential data do not divide along "researcher" and
    "reviewer" lines: who may reach them is a decision about *these people*, and
    it outlives any one of them. A workspace role gives that decision a name, so
    a rule can be written once — "the patent holders may reach this scope" — and
    keep meaning what it meant as people join and leave.

    It carries no capabilities of its own. ``base_role`` says what its members
    may *do*, drawn from the bundles that already exist and are already tested,
    so defining a role can neither invent a capability set nor mint an admin by
    accident. What is new is the name, and the name is what rules are about.
    """

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    slug: str
    display_name: str
    description: str = ""
    base_role: Role = Role.RESEARCHER
    created_at: datetime
    created_by: UUID | None = None

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, value: str) -> str:
        return normalise_role_slug(value)

    @field_validator("base_role")
    @classmethod
    def _base_is_assignable(cls, value: Role) -> Role:
        if value not in BASE_ROLES:
            raise ValueError(f"a role cannot be based on {value.value}")
        return value

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.base_role.capabilities


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class MembershipStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    EXPIRED = "expired"


class User(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    email_normalized: str
    email_display: str
    hashed_password: str
    full_name: str | None = None
    is_active: bool = True
    #: Incrementing this invalidates every issued token for the user.
    auth_epoch: int = 1
    created_at: datetime
    updated_at: datetime | None = None
    last_login_at: datetime | None = None

    @field_validator("email_normalized")
    @classmethod
    def _normalised(cls, value: str) -> str:
        if value != normalise_email(value):
            raise ValueError("email_normalized must be lower-cased and trimmed")
        return value


class UserExternalIdentity(BaseModel):
    """OIDC readiness: the stable pair is issuer + subject, never email."""

    model_config = {"frozen": True}

    user_id: UUID
    issuer: str
    subject: str
    email_at_link: str | None = None
    linked_at: datetime | None = None


class Workspace(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    slug: str
    display_name: str
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    default_policy_bundle_id: UUID | None = None
    branding_config_version_id: UUID | None = None
    created_at: datetime
    version: int = 1

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, value: str) -> str:
        if not value or not all(ch.isalnum() or ch in "-_" for ch in value):
            raise ValueError("slug must be alphanumeric with - or _")
        return value.lower()


class Membership(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    user_id: UUID
    role: Role
    #: The workspace role this member was put in, if any: its slug. ``role``
    #: beside it still says what they may *do*; this says which audience they
    #: belong to, which is what resource rules are written about.
    role_name: str | None = None
    status: MembershipStatus = MembershipStatus.ACTIVE
    #: Capabilities granted beyond the role bundle.
    extra_capabilities: frozenset[Capability] = frozenset()
    #: Tokens this member may use for answers, in total. None follows the
    #: installation default (`chat_token_limit_default`).
    token_limit: int | None = None
    invited_by: UUID | None = None
    accepted_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def role_key(self) -> str:
        """What to call this member's role on screen and in a record."""
        return self.role_name or self.role.value

    @property
    def role_keys(self) -> frozenset[str]:
        """Every role name a rule may use to reach this member.

        Both names, deliberately. A workspace role *adds* an audience rather
        than replacing one: an admin who moves a researcher into "patent
        holders" is saying "and also the patent material", not "and nothing
        they could read before" — and the second reading would quietly strip
        someone of everything the moment they were put in a new role.

        Taking something away is said the other way round, with a ``deny`` rule
        naming the role, which the engine weighs before any allow.
        """
        names = {self.role.value}
        if self.role_name:
            names.add(self.role_name)
        return frozenset(names)

    def capabilities(self) -> frozenset[Capability]:
        if self.status is not MembershipStatus.ACTIVE:
            return frozenset()
        return self.role.capabilities | self.extra_capabilities

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    def is_current(self, now: datetime) -> bool:
        if self.status is not MembershipStatus.ACTIVE:
            return False
        return self.expires_at is None or now < self.expires_at


class Group(BaseModel):
    """A project, restricted dataset, or reviewer pool."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    slug: str
    display_name: str
    description: str = ""
    member_user_ids: frozenset[UUID] = frozenset()
    created_at: datetime | None = None

    def contains(self, user_id: UUID) -> bool:
        return user_id in self.member_user_ids


class Invitation(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    email_normalized: str
    role: Role
    #: The workspace role the invitation puts them in, if any: its slug.
    role_name: str | None = None
    #: Digest of the invitation token. The token itself is never stored.
    token_digest: str
    invited_by: UUID
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_open(self, now: datetime) -> bool:
        return self.accepted_at is None and self.revoked_at is None and now < self.expires_at


class ServiceAccount(BaseModel):
    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    name: str
    capabilities: frozenset[Capability] = Field(default_factory=frozenset)
    created_at: datetime | None = None
