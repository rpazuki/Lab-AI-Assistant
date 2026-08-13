"""Users, workspaces, memberships, invitations, and grants.

Ported deliberately from the predecessor, whose access model worked: internal
users only, admin provisioning or invitation, no self-registration, short-lived
tokens behind an ``httpOnly`` same-origin cookie. What is new is the workspace
dimension, reviewer and steward capabilities that do not require full admin, and
grants evaluated against resources.

Password hashing is adaptive and upgrades on login. ``auth_epoch`` revokes every
issued token for a user at once, which is what a credential incident needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.canonical import sha256_text
from lab_assistant.contracts.errors import (
    LabAssistantError,
    NotFound,
    PolicyDenied,
    ValidationFailure,
)
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.policy import (
    Effect,
    GrantSpec,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import (
    Capability,
    Group,
    Invitation,
    Membership,
    MembershipStatus,
    Role,
    User,
    Workspace,
    normalise_email,
)

__all__ = ["AccountService", "AuthenticationFailed", "PasswordHasher", "PasswordVerification"]


class AuthenticationFailed(LabAssistantError):
    """Login failed. The message never says which half was wrong."""

    from lab_assistant.contracts.errors import ErrorClass as _ErrorClass

    error_class = _ErrorClass.AUTHENTICATION_REQUIRED
    http_status = 401


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    ok: bool
    needs_rehash: bool = False


class PasswordHasher:
    """Argon2id where available, PBKDF2-SHA256 otherwise.

    Both schemes are verifiable, so a deployment can migrate hashes as users log
    in rather than forcing a reset for everyone. The scheme name is stored in the
    hash string, so nothing has to guess.
    """

    def __init__(self, *, scheme: str = "argon2id") -> None:
        self._scheme = scheme
        self._argon2 = None
        if scheme == "argon2id":
            try:
                from argon2 import PasswordHasher as Argon2Hasher

                self._argon2 = Argon2Hasher()
            except ModuleNotFoundError:  # pragma: no cover - optional dependency
                self._scheme = "pbkdf2_sha256"

    @property
    def scheme(self) -> str:
        return self._scheme

    def hash(self, password: str) -> str:
        if len(password) < 12:
            raise ValidationFailure("password must be at least 12 characters")
        if self._argon2 is not None:
            return self._argon2.hash(password)
        return self._pbkdf2_hash(password)

    def verify(self, password: str, stored: str) -> PasswordVerification:
        if stored.startswith("$argon2"):
            if self._argon2 is None:  # pragma: no cover - optional dependency
                return PasswordVerification(ok=False)
            try:
                self._argon2.verify(stored, password)
            except Exception:
                return PasswordVerification(ok=False)
            return PasswordVerification(
                ok=True, needs_rehash=self._argon2.check_needs_rehash(stored)
            )
        if stored.startswith("pbkdf2_sha256$"):
            ok = self._pbkdf2_verify(password, stored)
            # A legacy hash verifies, then upgrades to the current scheme.
            return PasswordVerification(ok=ok, needs_rehash=ok and self._argon2 is not None)
        return PasswordVerification(ok=False)

    @staticmethod
    def _pbkdf2_hash(password: str, *, iterations: int = 600_000) -> str:
        import base64
        import hashlib
        import secrets

        salt = secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return (
            "pbkdf2_sha256$"
            f"{iterations}$"
            f"{base64.b64encode(salt).decode()}$"
            f"{base64.b64encode(derived).decode()}"
        )

    @staticmethod
    def _pbkdf2_verify(password: str, stored: str) -> bool:
        import base64
        import hashlib
        import hmac

        try:
            _scheme, iterations, salt_b64, hash_b64 = stored.split("$")
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
            derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(derived, expected)


class AccountService:
    def __init__(self, context: ServiceContext, *, hasher: PasswordHasher | None = None) -> None:
        self._context = context
        self._hasher = hasher or PasswordHasher(scheme=context.settings.password_hash_scheme)

    # ── users ───────────────────────────────────────────────────────────────
    async def create_user(
        self,
        *,
        email: str,
        password: str,
        full_name: str | None = None,
        created_by: UUID | None = None,
    ) -> User:
        context = self._context
        normalised = normalise_email(email)
        async with context.uow_factory() as uow:
            if await uow.users.get_by_email(normalised) is not None:
                raise ValidationFailure("a user with this email already exists")
            user = User(
                id=context.new_id(),
                email_normalized=normalised,
                email_display=email.strip(),
                hashed_password=self._hasher.hash(password),
                full_name=full_name,
                created_at=context.now(),
            )
            await uow.users.add(user)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=None,
                    actor_type=ActorType.USER if created_by else ActorType.SYSTEM,
                    actor_id=created_by,
                    action=AuditAction.ADMIN_ACTION,
                    subject_type="user",
                    subject_id=user.id,
                    previous_hash=await uow.audit.latest_hash(None),
                    reason_code="user_provisioned",
                    metadata={"email_digest": sha256_text(normalised)[:16]},
                )
            )
            await uow.commit()
        return user

    async def authenticate(self, *, email: str, password: str) -> User:
        context = self._context
        normalised = normalise_email(email)
        async with context.uow_factory() as uow:
            user = await uow.users.get_by_email(normalised)
            verification = (
                self._hasher.verify(password, user.hashed_password)
                if user is not None
                else PasswordVerification(ok=False)
            )
            if user is None or not verification.ok or not user.is_active:
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=None,
                        actor_type=ActorType.SYSTEM,
                        actor_id=None,
                        action=AuditAction.LOGIN_FAILED,
                        subject_type="user",
                        subject_id=user.id if user else None,
                        previous_hash=await uow.audit.latest_hash(None),
                        reason_code="invalid_credentials" if user else "unknown_user",
                        metadata={"email_digest": sha256_text(normalised)[:16]},
                    )
                )
                await uow.commit()
                raise AuthenticationFailed("email or password is incorrect")

            if verification.needs_rehash:
                user.hashed_password = self._hasher.hash(password)
            user.last_login_at = context.now()
            user.updated_at = context.now()
            await uow.users.update(user)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=None,
                    actor_type=ActorType.USER,
                    actor_id=user.id,
                    action=AuditAction.LOGIN_SUCCEEDED,
                    subject_type="user",
                    subject_id=user.id,
                    previous_hash=await uow.audit.latest_hash(None),
                    metadata={"hash_upgraded": verification.needs_rehash},
                )
            )
            await uow.commit()
        return user

    async def revoke_sessions(self, *, user_id: UUID, actor_id: UUID | None = None) -> User:
        """Bump the auth epoch: every issued token for this user stops working."""
        context = self._context
        async with context.uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFound(f"user {user_id} not found")
            user.auth_epoch += 1
            user.updated_at = context.now()
            await uow.users.update(user)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=None,
                    actor_type=ActorType.USER if actor_id else ActorType.SYSTEM,
                    actor_id=actor_id,
                    action=AuditAction.SESSION_REVOKED,
                    subject_type="user",
                    subject_id=user.id,
                    previous_hash=await uow.audit.latest_hash(None),
                    metadata={"auth_epoch": user.auth_epoch},
                )
            )
            await uow.commit()
        return user

    async def set_password(self, *, user_id: UUID, password: str, actor_id: UUID | None) -> User:
        context = self._context
        async with context.uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFound(f"user {user_id} not found")
            user.hashed_password = self._hasher.hash(password)
            user.auth_epoch += 1  # a password change revokes existing tokens
            user.updated_at = context.now()
            await uow.users.update(user)
            await uow.commit()
        return user

    # ── workspaces ──────────────────────────────────────────────────────────
    async def create_workspace(
        self, *, slug: str, display_name: str, owner_user_id: UUID
    ) -> tuple[Workspace, Membership]:
        context = self._context
        async with context.uow_factory() as uow:
            if await uow.workspaces.get_by_slug(slug.lower()) is not None:
                raise ValidationFailure(f"workspace slug {slug!r} is taken")
            workspace = Workspace(
                id=context.new_id(),
                slug=slug,
                display_name=display_name,
                created_at=context.now(),
            )
            await uow.workspaces.add(workspace)
            membership = Membership(
                id=context.new_id(),
                workspace_id=workspace.id,
                user_id=owner_user_id,
                role=Role.ADMIN,
                status=MembershipStatus.ACTIVE,
                accepted_at=context.now(),
            )
            await uow.workspaces.add_membership(membership)
            await uow.commit()
        return workspace, membership

    async def add_member(
        self,
        actor: ActorContext,
        *,
        user_id: UUID,
        role: Role,
        extra_capabilities: frozenset[Capability] = frozenset(),
    ) -> Membership:
        actor.require(Capability.MANAGE_MEMBERS)
        context = self._context
        async with context.uow_factory() as uow:
            existing = await uow.workspaces.get_membership(actor.workspace_id, user_id)
            if existing is not None:
                existing.role = role
                existing.extra_capabilities = extra_capabilities
                existing.status = MembershipStatus.ACTIVE
                await uow.workspaces.update_membership(existing)
                membership = existing
            else:
                membership = Membership(
                    id=context.new_id(),
                    workspace_id=actor.workspace_id,
                    user_id=user_id,
                    role=role,
                    extra_capabilities=extra_capabilities,
                    status=MembershipStatus.ACTIVE,
                    invited_by=actor.user_id,
                    accepted_at=context.now(),
                )
                await uow.workspaces.add_membership(membership)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.MEMBERSHIP_CHANGED,
                    subject_type="membership",
                    subject_id=membership.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    metadata={
                        "user_id": str(user_id),
                        "role": role.value,
                        "extra_capabilities": sorted(c.value for c in extra_capabilities),
                    },
                )
            )
            await uow.commit()
        return membership

    async def invite(
        self, actor: ActorContext, *, email: str, role: Role, ttl_hours: int = 168
    ) -> tuple[Invitation, str]:
        """Create an invitation. The token is returned once and never stored."""
        actor.require(Capability.MANAGE_MEMBERS)
        import secrets

        context = self._context
        token = secrets.token_urlsafe(32)
        invitation = Invitation(
            id=context.new_id(),
            workspace_id=actor.workspace_id,
            email_normalized=normalise_email(email),
            role=role,
            token_digest=sha256_text(token),
            invited_by=actor.user_id,
            created_at=context.now(),
            expires_at=context.now() + timedelta(hours=ttl_hours),
        )
        async with context.uow_factory() as uow:
            await uow.workspaces.add_invitation(invitation)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.INVITATION_CREATED,
                    subject_type="invitation",
                    subject_id=invitation.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    metadata={
                        "email_digest": sha256_text(invitation.email_normalized)[:16],
                        "role": role.value,
                    },
                )
            )
            await uow.commit()
        return invitation, token

    async def accept_invitation(
        self, *, token: str, password: str, full_name: str | None = None
    ) -> tuple[User, Membership]:
        context = self._context
        digest = sha256_text(token)
        async with context.uow_factory() as uow:
            invitation = await uow.workspaces.get_invitation_by_digest(digest)
            if invitation is None or not invitation.is_open(context.now()):
                raise PolicyDenied("this invitation is not valid")
            user = await uow.users.get_by_email(invitation.email_normalized)
        if user is None:
            user = await self.create_user(
                email=invitation.email_normalized, password=password, full_name=full_name
            )
        async with context.uow_factory() as uow:
            membership = Membership(
                id=context.new_id(),
                workspace_id=invitation.workspace_id,
                user_id=user.id,
                role=invitation.role,
                status=MembershipStatus.ACTIVE,
                invited_by=invitation.invited_by,
                accepted_at=context.now(),
            )
            await uow.workspaces.add_membership(membership)
            accepted = invitation.model_copy(update={"accepted_at": context.now()})
            await uow.workspaces.update_invitation(accepted)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=invitation.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=user.id,
                    action=AuditAction.INVITATION_ACCEPTED,
                    subject_type="invitation",
                    subject_id=invitation.id,
                    previous_hash=await uow.audit.latest_hash(invitation.workspace_id),
                )
            )
            await uow.commit()
        return user, membership

    # ── groups and grants ───────────────────────────────────────────────────
    async def create_group(
        self, actor: ActorContext, *, slug: str, display_name: str, member_user_ids: frozenset[UUID]
    ) -> Group:
        actor.require(Capability.MANAGE_MEMBERS)
        context = self._context
        group = Group(
            id=context.new_id(),
            workspace_id=actor.workspace_id,
            slug=slug,
            display_name=display_name,
            member_user_ids=member_user_ids,
            created_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.workspaces.add_group(group)
            await uow.commit()
        return group

    async def grant(
        self,
        actor: ActorContext,
        *,
        subject_type: SubjectType,
        subject_id: UUID,
        resource_type: ResourceType,
        resource_id: UUID | None,
        action: PolicyAction,
        effect: Effect = Effect.ALLOW,
        policy_basis: str = "steward_decision",
    ) -> GrantSpec:
        actor.require(Capability.MANAGE_POLICY)
        context = self._context
        spec = GrantSpec(
            id=context.new_id(),
            workspace_id=actor.workspace_id,
            subject_type=subject_type,
            subject_id=subject_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            effect=effect,
            policy_basis=policy_basis,
        )
        async with context.uow_factory() as uow:
            await uow.policies.add_grant(spec)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.GRANT_CHANGED,
                    subject_type="grant",
                    subject_id=spec.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=policy_basis,
                    metadata={
                        "subject": f"{subject_type.value}:{subject_id}",
                        "resource": f"{resource_type.value}:{resource_id or '*'}",
                        "action": action.value,
                        "effect": effect.value,
                    },
                )
            )
            await uow.commit()
        return spec

    async def revoke_grant(self, actor: ActorContext, *, grant_id: UUID) -> None:
        actor.require(Capability.MANAGE_POLICY)
        context = self._context
        async with context.uow_factory() as uow:
            await uow.policies.remove_grant(actor.workspace_id, grant_id)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.GRANT_CHANGED,
                    subject_type="grant",
                    subject_id=grant_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="revoked",
                )
            )
            await uow.commit()
