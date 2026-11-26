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
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.rate_limit import RateLimiter, login_budget_key
from lab_assistant.contracts.canonical import sha256_text
from lab_assistant.contracts.connector import RatePolicy
from lab_assistant.contracts.errors import (
    ErrorClass,
    LabAssistantError,
    NotFound,
    PolicyDenied,
    StateTransitionError,
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
    BASE_ROLES,
    Capability,
    Group,
    Invitation,
    Membership,
    MembershipStatus,
    Role,
    User,
    Workspace,
    WorkspaceRole,
    normalise_email,
    normalise_role_slug,
)

__all__ = [
    "AccountService",
    "AuthenticationFailed",
    "InvitationState",
    "LoginRateLimited",
    "MemberView",
    "PasswordHasher",
    "PasswordVerification",
    "RoleChoice",
    "invitation_state",
]

#: Roles a person may hold. A service account is provisioned, never invited or
#: assigned to a person: its capabilities are for unattended workers.
_PERSON_ROLES = frozenset(role for role in Role if role is not Role.SERVICE_ACCOUNT)

#: What each built-in role is for, in one line, so the person choosing between
#: them and a role of their own can see what they would be based on.
_BUILT_IN_DESCRIPTIONS: dict[Role, str] = {
    Role.RESEARCHER: "Asks questions, searches, and uploads papers of their own.",
    Role.REVIEWER: "A researcher who also settles identity and reviews parses and claims.",
    Role.DATA_STEWARD: (
        "Runs the corpus: scopes, discovery, rights, acquisition, releases and exports."
    ),
    Role.ADMIN: "Everything, including members and policy, and every scope without a rule.",
}


def _role_text(value: str | Role) -> str:
    return value.value if isinstance(value, Role) else str(value).strip().lower()


@dataclass(frozen=True, slots=True)
class RoleChoice:
    """One role a member can be given: a built-in bundle, or one defined here.

    Both kinds appear in one list because that is how the choice is made — a
    person holds one role, and whether its capabilities ship with the product or
    were chosen by an admin is a detail of where it came from, not a different
    kind of thing to pick.
    """

    name: str
    display_name: str
    description: str
    base_role: Role
    capabilities: frozenset[Capability]
    members: int = 0
    id: UUID | None = None

    @property
    def custom(self) -> bool:
        """Whether an admin defined it here, rather than it shipping with the product."""
        return self.id is not None


class InvitationState(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


def invitation_state(invitation: Invitation, now: datetime) -> InvitationState:
    if invitation.accepted_at is not None:
        return InvitationState.ACCEPTED
    if invitation.revoked_at is not None:
        return InvitationState.REVOKED
    if now >= invitation.expires_at:
        return InvitationState.EXPIRED
    return InvitationState.OPEN


@dataclass(frozen=True, slots=True)
class MemberView:
    """A membership with the account it belongs to, for the members page."""

    membership: Membership
    user: User


class AuthenticationFailed(LabAssistantError):
    """Login failed. The message never says which half was wrong."""

    error_class = ErrorClass.AUTHENTICATION_REQUIRED
    http_status = 401


class LoginRateLimited(LabAssistantError):
    """Too many sign-in attempts for this account or origin.

    Distinct from :class:`AuthenticationFailed` because the caller needs to know to
    wait rather than to re-check their password — and it still says nothing about
    whether the account exists.
    """

    error_class = ErrorClass.RATE_LIMITED
    http_status = 429


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

    async def authenticate(
        self, *, email: str, password: str, client_signal: str | None = None
    ) -> User:
        """Verify credentials, rate-limited and audited.

        Two buckets, both store-backed so they hold across worker processes: one per
        account and one per client signal (the caller's network origin). An
        in-process dictionary would reset on deploy and would not be shared, which
        makes it a speed bump rather than a control.

        The buckets are keyed by **digest**, never by the address itself: a rate
        limiter is not a reason to write plaintext emails into a shared table.
        """
        context = self._context
        normalised = normalise_email(email)
        await self._throttle_login(normalised, client_signal=client_signal)
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
                # The same message for an unknown account and a wrong password. The
                # audit row records which it was; the caller learns neither.
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
        # A successful sign-in clears the account's bucket, so someone who simply
        # mistyped their password twice is not locked out of their own account.
        await self._reset_login_budget(login_budget_key(normalised))
        return user

    async def _throttle_login(self, normalised_email: str, *, client_signal: str | None) -> None:
        limiter = RateLimiter(self._context)
        per_minute = max(1, self._context.settings.login_rate_limit_per_minute)
        policy = RatePolicy(
            requests_per_second=per_minute / 60.0,
            burst=per_minute,
            # Concurrency is not the control here; the rate is.
            max_concurrent=per_minute,
        )
        keys = [login_budget_key(normalised_email)]
        if client_signal:
            keys.append(login_budget_key(client_signal, kind="login-origin"))
        for key in keys:
            try:
                await limiter.acquire(key, policy=policy)
            except LabAssistantError as exc:
                if exc.error_class is not ErrorClass.RATE_LIMITED:
                    raise
                raise LoginRateLimited(
                    "too many sign-in attempts; try again shortly",
                    context={
                        "retry_after_seconds": (exc.context or {}).get("retry_after_seconds", 60)
                    },
                ) from exc
            # The slot is accounting for an attempt that has already happened, not a
            # long-running call: release it immediately so concurrency is not the cap.
            await limiter.release(key)

    async def _reset_login_budget(self, key: str) -> None:
        async with self._context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(key, for_update=True)
            if budget is None:
                return
            budget.tokens = budget.burst
            budget.next_permitted_at = None
            budget.updated_at = self._context.now()
            await uow.tasks.upsert_rate_budget(budget)
            await uow.commit()

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
        """Replace a password, signing the person out everywhere, and audit it.

        The audit row records that the password changed and who changed it, never
        anything derived from the password itself.
        """
        context = self._context
        async with context.uow_factory() as uow:
            user = await uow.users.get(user_id)
            if user is None:
                raise NotFound(f"user {user_id} not found")
            user.hashed_password = self._hasher.hash(password)
            user.auth_epoch += 1  # a password change revokes existing tokens
            user.updated_at = context.now()
            await uow.users.update(user)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=None,
                    actor_type=ActorType.USER if actor_id else ActorType.SYSTEM,
                    actor_id=actor_id,
                    action=AuditAction.PASSWORD_CHANGED,
                    subject_type="user",
                    subject_id=user.id,
                    previous_hash=await uow.audit.latest_hash(None),
                    metadata={"auth_epoch": user.auth_epoch},
                )
            )
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
        role: str | Role,
        extra_capabilities: frozenset[Capability] = frozenset(),
    ) -> Membership:
        actor.require(Capability.MANAGE_MEMBERS)
        context = self._context
        async with context.uow_factory() as uow:
            base, role_name = await self._resolve_role(uow, actor.workspace_id, role)
            existing = await uow.workspaces.get_membership(actor.workspace_id, user_id)
            if existing is not None:
                existing.role = base
                existing.role_name = role_name
                existing.extra_capabilities = extra_capabilities
                existing.status = MembershipStatus.ACTIVE
                await uow.workspaces.update_membership(existing)
                membership = existing
            else:
                membership = Membership(
                    id=context.new_id(),
                    workspace_id=actor.workspace_id,
                    user_id=user_id,
                    role=base,
                    role_name=role_name,
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
                        "role": membership.role_key,
                        "base_role": base.value,
                        "extra_capabilities": sorted(c.value for c in extra_capabilities),
                    },
                )
            )
            await uow.commit()
        return membership

    async def invite(
        self, actor: ActorContext, *, email: str, role: str | Role, ttl_hours: int = 168
    ) -> tuple[Invitation, str]:
        """Create an invitation. The token is returned once and never stored.

        An earlier open invitation for the same address is revoked, so a lost link
        is replaced rather than left valid beside its successor. An address that is
        already an active member is refused: changing their role is a membership
        change, not a second invitation.
        """
        actor.require(Capability.MANAGE_MEMBERS)
        if not 1 <= ttl_hours <= 24 * 30:
            raise ValidationFailure("an invitation lasts between 1 hour and 30 days")
        import secrets

        context = self._context
        normalised = normalise_email(email)
        token = secrets.token_urlsafe(32)
        now = context.now()
        async with context.uow_factory() as uow:
            # Resolving the role needs the workspace's own roles, so it happens
            # here rather than before the transaction opens.
            base, role_name = await self._resolve_role(uow, actor.workspace_id, role)
            self._require_assignable(actor, base)
            invitation = Invitation(
                id=context.new_id(),
                workspace_id=actor.workspace_id,
                email_normalized=normalised,
                role=base,
                role_name=role_name,
                token_digest=sha256_text(token),
                invited_by=actor.user_id,
                created_at=now,
                expires_at=now + timedelta(hours=ttl_hours),
            )
            existing_user = await uow.users.get_by_email(normalised)
            if existing_user is not None:
                membership = await uow.workspaces.get_membership(
                    actor.workspace_id, existing_user.id
                )
                if membership is not None and membership.status is MembershipStatus.ACTIVE:
                    raise ValidationFailure(
                        "this address is already an active member; change their role instead"
                    )
            superseded = 0
            for earlier in await uow.workspaces.list_invitations(actor.workspace_id):
                if earlier.email_normalized == normalised and earlier.is_open(now):
                    await uow.workspaces.update_invitation(
                        earlier.model_copy(update={"revoked_at": now})
                    )
                    superseded += 1
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
                        "role": role_name or base.value,
                        "superseded": superseded,
                    },
                )
            )
            await uow.commit()
        return invitation, token

    async def list_invitations(self, actor: ActorContext) -> list[Invitation]:
        """Every invitation of the workspace, newest first."""
        actor.require(Capability.MANAGE_MEMBERS)
        async with self._context.uow_factory() as uow:
            invitations = await uow.workspaces.list_invitations(actor.workspace_id)
        return sorted(invitations, key=lambda item: item.created_at, reverse=True)

    async def revoke_invitation(self, actor: ActorContext, *, invitation_id: UUID) -> Invitation:
        actor.require(Capability.MANAGE_MEMBERS)
        context = self._context
        async with context.uow_factory() as uow:
            invitation = next(
                (
                    item
                    for item in await uow.workspaces.list_invitations(actor.workspace_id)
                    if item.id == invitation_id
                ),
                None,
            )
            if invitation is None:
                raise NotFound(f"invitation {invitation_id} not found")
            if not invitation.is_open(context.now()):
                raise StateTransitionError(
                    "only an open invitation can be revoked",
                    context={"state": invitation_state(invitation, context.now()).value},
                )
            revoked = invitation.model_copy(update={"revoked_at": context.now()})
            await uow.workspaces.update_invitation(revoked)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ADMIN_ACTION,
                    subject_type="invitation",
                    subject_id=invitation.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="invitation_revoked",
                )
            )
            await uow.commit()
        return revoked

    async def delete_invitation(self, actor: ActorContext, *, invitation_id: UUID) -> None:
        """Remove a revoked or expired invitation from the list.

        An open one is revoked first, so that a link already sent stops working
        through a step that says so; an accepted one is how a member joined.
        """
        actor.require(Capability.MANAGE_MEMBERS)
        context = self._context
        async with context.uow_factory() as uow:
            invitation = next(
                (
                    item
                    for item in await uow.workspaces.list_invitations(actor.workspace_id)
                    if item.id == invitation_id
                ),
                None,
            )
            if invitation is None:
                raise NotFound(f"invitation {invitation_id} not found")
            state = invitation_state(invitation, context.now())
            if state not in (InvitationState.REVOKED, InvitationState.EXPIRED):
                raise StateTransitionError(
                    "only a revoked or expired invitation can be deleted"
                    + ("; revoke it first" if state is InvitationState.OPEN else ""),
                    context={"state": state.value},
                )
            await uow.workspaces.delete_invitation(actor.workspace_id, invitation_id)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.INVITATION_DELETED,
                    subject_type="invitation",
                    subject_id=invitation.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=f"{state.value}_invitation_deleted",
                    metadata={"role": invitation.role.value, "state": state.value},
                )
            )
            await uow.commit()

    async def preview_invitation(self, *, token: str) -> tuple[Invitation, Workspace]:
        """What an open invitation offers, for the page that accepts it.

        Answers only for an open invitation, and says the same thing for a token
        that never existed as for one that expired or was revoked.
        """
        context = self._context
        async with context.uow_factory() as uow:
            invitation = await uow.workspaces.get_invitation_by_digest(sha256_text(token))
            workspace = await uow.workspaces.get(invitation.workspace_id) if invitation else None
        if invitation is None or workspace is None or not invitation.is_open(context.now()):
            raise PolicyDenied("this invitation is not valid")
        return invitation, workspace

    async def accept_invitation(
        self,
        *,
        token: str,
        password: str,
        full_name: str | None = None,
        client_signal: str | None = None,
    ) -> tuple[User, Membership]:
        """Redeem an invitation token.

        Throttled per origin like login: this is the other unauthenticated endpoint,
        and a token guess costs the server an argon2 hash whether or not it is right.

        Someone who already has an account proves it with their existing password:
        the token says the invitation reached their inbox, not that whoever holds
        it may set a new password on an account that already exists.
        """
        context = self._context
        await self._throttle_login(
            f"invitation:{sha256_text(token)[:16]}", client_signal=client_signal
        )
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
        elif not self._hasher.verify(password, user.hashed_password).ok or not user.is_active:
            raise AuthenticationFailed(
                "this address already has an account; enter its existing password"
            )
        async with context.uow_factory() as uow:
            existing = await uow.workspaces.get_membership(invitation.workspace_id, user.id)
            if existing is not None:
                membership = existing.model_copy(
                    update={
                        "role": invitation.role,
                        "role_name": invitation.role_name,
                        "status": MembershipStatus.ACTIVE,
                        "accepted_at": context.now(),
                    }
                )
                await uow.workspaces.update_membership(membership)
            else:
                membership = Membership(
                    id=context.new_id(),
                    workspace_id=invitation.workspace_id,
                    user_id=user.id,
                    role=invitation.role,
                    role_name=invitation.role_name,
                    status=MembershipStatus.ACTIVE,
                    invited_by=invitation.invited_by,
                    accepted_at=context.now(),
                )
                await uow.workspaces.add_membership(membership)
            accepted = invitation.model_copy(update={"accepted_at": context.now()})
            await uow.workspaces.update_invitation(accepted)
            # Accepting signs the person in, so it is their first sign-in too.
            signed_in = await uow.users.get(user.id)
            if signed_in is not None:
                signed_in.last_login_at = context.now()
                signed_in.updated_at = context.now()
                await uow.users.update(signed_in)
                user = signed_in
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

    # ── members ─────────────────────────────────────────────────────────────
    async def list_members(self, actor: ActorContext) -> list[MemberView]:
        """Every membership of the workspace with its account, by email."""
        actor.require(Capability.MANAGE_MEMBERS)
        async with self._context.uow_factory() as uow:
            memberships = await uow.workspaces.list_memberships(actor.workspace_id)
            views: list[MemberView] = []
            for membership in memberships:
                user = await uow.users.get(membership.user_id)
                if user is not None:
                    views.append(MemberView(membership=membership, user=user))
        return sorted(views, key=lambda view: view.user.email_normalized)

    async def current_workspaces(self, user_id: UUID) -> list[Workspace]:
        """The workspaces where this user's membership is current, for sign-in."""
        context = self._context
        async with context.uow_factory() as uow:
            current: list[Workspace] = []
            for workspace in await uow.workspaces.list_for_user(user_id):
                membership = await uow.workspaces.get_membership(workspace.id, user_id)
                if membership is not None and membership.is_current(context.now()):
                    current.append(workspace)
        return current

    async def update_member(
        self,
        actor: ActorContext,
        *,
        user_id: UUID,
        role: str | Role | None = None,
        status: MembershipStatus | None = None,
        token_limit: int | None = None,
        reset_token_limit: bool = False,
    ) -> MemberView:
        """Change a member's role, or suspend or reinstate them.

        Suspending also ends every session the person holds, so it takes effect
        on their next request rather than when their token expires. Two guards:
        nobody changes their own membership, and only an admin grants or takes
        away admin. Together they keep the workspace from losing its last admin,
        since whoever makes a change is an admin who stays one.
        """
        actor.require(Capability.MANAGE_MEMBERS)
        if status is not None and status not in (
            MembershipStatus.ACTIVE,
            MembershipStatus.SUSPENDED,
        ):
            raise ValidationFailure("a membership is either active or suspended")
        if token_limit is not None and token_limit < 0:
            raise ValidationFailure("a token limit cannot be negative")
        # An admin may set their own token limit; role and status are another
        # admin's call, so nobody locks themselves out by accident.
        if user_id == actor.user_id and (role is not None or status is not None):
            raise PolicyDenied("you cannot change your own membership; ask another admin")
        context = self._context
        async with context.uow_factory() as uow:
            # One name in, a bundle and an audience out. Resolved before the
            # membership is read so an unknown role is refused as a bad request
            # rather than after a lookup that implies it was the member's fault.
            requested: tuple[Role, str | None] | None = None
            if role is not None:
                requested = await self._resolve_role(uow, actor.workspace_id, role)
                self._require_assignable(actor, requested[0])
            membership = await uow.workspaces.get_membership(actor.workspace_id, user_id)
            user = await uow.users.get(user_id)
            if membership is None or user is None:
                raise NotFound(f"member {user_id} not found")
            if membership.role is Role.ADMIN:
                self._require_assignable(actor, Role.ADMIN)
            new_role, new_name = requested or (membership.role, membership.role_name)
            new_status = status or membership.status
            new_limit = (
                None
                if reset_token_limit
                else token_limit
                if token_limit is not None
                else membership.token_limit
            )
            updated = membership.model_copy(
                update={
                    "role": new_role,
                    "role_name": new_name,
                    "status": new_status,
                    "token_limit": new_limit,
                }
            )
            await uow.workspaces.update_membership(updated)
            suspended = (
                new_status is MembershipStatus.SUSPENDED
                and membership.status is not MembershipStatus.SUSPENDED
            )
            if suspended:
                user.auth_epoch += 1
                user.updated_at = context.now()
                await uow.users.update(user)
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
                        "role": updated.role_key,
                        "base_role": new_role.value,
                        "previous_role": membership.role_key,
                        "status": new_status.value,
                        "previous_status": membership.status.value,
                        "token_limit": new_limit,
                        "previous_token_limit": membership.token_limit,
                        "sessions_revoked": suspended,
                    },
                )
            )
            await uow.commit()
        return MemberView(membership=updated, user=user)

    @staticmethod
    def _require_assignable(actor: ActorContext, role: Role) -> None:
        if role not in _PERSON_ROLES:
            raise ValidationFailure(f"{role.value} is not a role a person can hold")
        if role is Role.ADMIN and (
            actor.membership is None or actor.membership.role is not Role.ADMIN
        ):
            raise PolicyDenied("only an admin can grant or change the admin role")

    # ── roles ───────────────────────────────────────────────────────────────
    @staticmethod
    async def _resolve_role(
        uow: Any, workspace_id: UUID, value: str | Role
    ) -> tuple[Role, str | None]:
        """One role name as the pair a membership stores: a bundle and an audience.

        A built-in name resolves to itself and no audience. A role an admin
        defined resolves to the bundle it was based on, plus its own slug — the
        name rules are written about. Callers pass one name because a person
        holds one role; the split is storage, not a second choice.
        """
        text = _role_text(value)
        for role in Role:
            if role.value == text:
                return role, None
        defined = await uow.workspaces.get_role(workspace_id, text)
        if defined is None:
            raise ValidationFailure(f"{text!r} is not a role in this workspace")
        return defined.base_role, defined.slug

    async def list_roles(self, actor: ActorContext) -> list[RoleChoice]:
        """Every role a member can be given, built-in ones first, with head counts.

        Gated on ``manage_scope`` rather than ``manage_members``: a steward who
        is writing a rule about a scope needs to read the names it can be
        written about, and the names are not themselves a member's business.
        """
        actor.require(Capability.MANAGE_SCOPE)
        ws = actor.workspace_id
        async with self._context.uow_factory() as uow:
            defined = await uow.workspaces.list_roles(ws)
            memberships = await uow.workspaces.list_memberships(ws)
        held: dict[str, int] = {}
        for membership in memberships:
            held[membership.role_key] = held.get(membership.role_key, 0) + 1
        choices = [
            RoleChoice(
                name=role.value,
                display_name=role.value.replace("_", " "),
                description=_BUILT_IN_DESCRIPTIONS.get(role, ""),
                base_role=role,
                capabilities=role.capabilities,
                members=held.get(role.value, 0),
            )
            for role in (Role.RESEARCHER, Role.REVIEWER, Role.DATA_STEWARD, Role.ADMIN)
        ]
        choices.extend(
            RoleChoice(
                name=role.slug,
                display_name=role.display_name,
                description=role.description,
                base_role=role.base_role,
                capabilities=role.capabilities,
                members=held.get(role.slug, 0),
                id=role.id,
            )
            for role in defined
        )
        return choices

    async def create_role(
        self,
        actor: ActorContext,
        *,
        name: str,
        display_name: str | None = None,
        description: str = "",
        base_role: Role = Role.RESEARCHER,
    ) -> WorkspaceRole:
        """Define a role of this workspace's own.

        ``manage_policy``, which only an admin holds, rather than
        ``manage_members``: a role is the name a rule is written about, so
        defining one is deciding what the rules *can* say. Someone who could
        invent a name and then be given it would be deciding their own reach.
        """
        actor.require(Capability.MANAGE_POLICY)
        try:
            slug = normalise_role_slug(name)
        except ValueError as error:
            raise ValidationFailure(str(error)) from error
        if slug in {role.value for role in Role}:
            raise ValidationFailure(f"{slug!r} is a built-in role; choose another name")
        if base_role not in BASE_ROLES:
            allowed = ", ".join(sorted(role.value for role in BASE_ROLES))
            raise ValidationFailure(f"a role cannot be based on {base_role.value}; use {allowed}")
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            if await uow.workspaces.get_role(ws, slug) is not None:
                raise ValidationFailure(f"a role named {slug!r} already exists")
            role = WorkspaceRole(
                id=context.new_id(),
                workspace_id=ws,
                slug=slug,
                display_name=(display_name or name).strip() or slug,
                description=description.strip(),
                base_role=base_role,
                created_at=context.now(),
                created_by=actor.user_id,
            )
            await uow.workspaces.add_role(role)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ROLE_DEFINED,
                    subject_type="workspace_role",
                    subject_id=role.id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="role_created",
                    metadata={"role": role.slug, "base_role": base_role.value},
                )
            )
            await uow.commit()
        return role

    async def update_role(
        self,
        actor: ActorContext,
        *,
        role_id: UUID,
        display_name: str | None = None,
        description: str | None = None,
        base_role: Role | None = None,
    ) -> WorkspaceRole:
        """Correct a role's wording, or change what its members may do.

        The slug is not among the arguments and never changes: it is written
        into every rule that names the role and onto every membership that holds
        it, so renaming it would silently detach both.
        """
        actor.require(Capability.MANAGE_POLICY)
        if base_role is not None and base_role not in BASE_ROLES:
            allowed = ", ".join(sorted(role.value for role in BASE_ROLES))
            raise ValidationFailure(f"a role cannot be based on {base_role.value}; use {allowed}")
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            existing = next(
                (role for role in await uow.workspaces.list_roles(ws) if role.id == role_id), None
            )
            if existing is None:
                raise NotFound(f"role {role_id} not found")
            updated = existing.model_copy(
                update={
                    "display_name": (display_name or existing.display_name).strip(),
                    "description": (
                        existing.description if description is None else description.strip()
                    ),
                    "base_role": base_role or existing.base_role,
                }
            )
            await uow.workspaces.add_role(updated)
            if updated.base_role is not existing.base_role:
                # Capabilities come from the base, and a membership stores the
                # base beside the slug, so the people already in the role have
                # to be moved with it or they would keep the old bundle.
                for membership in await uow.workspaces.list_memberships(ws):
                    if membership.role_name == existing.slug:
                        await uow.workspaces.update_membership(
                            membership.model_copy(update={"role": updated.base_role})
                        )
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ROLE_DEFINED,
                    subject_type="workspace_role",
                    subject_id=updated.id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="role_changed",
                    metadata={
                        "role": updated.slug,
                        "base_role": updated.base_role.value,
                        "previous_base_role": existing.base_role.value,
                    },
                )
            )
            await uow.commit()
        return updated

    async def delete_role(self, actor: ActorContext, *, role_id: UUID) -> int:
        """Remove a role nobody holds, and every rule that named it.

        Refused while anyone is in it: moving them somewhere else is a decision
        about those people, and taking the name away underneath them would make
        it silently, with no record of where they ended up. Rules naming a role
        nobody can hold cannot match anything, so they go with it rather than
        lingering as access that looks live and is not. The count is returned so
        the caller can say how many went.
        """
        actor.require(Capability.MANAGE_POLICY)
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            existing = next(
                (role for role in await uow.workspaces.list_roles(ws) if role.id == role_id), None
            )
            if existing is None:
                raise NotFound(f"role {role_id} not found")
            members = await uow.workspaces.list_memberships(ws)
            held = [m for m in members if m.role_name == existing.slug]
            if held:
                people = "person is" if len(held) == 1 else "people are"
                raise ValidationFailure(
                    f"{len(held)} {people} still in {existing.display_name!r}; "
                    "move them to another role first"
                )
            rules = await uow.policies.list_grants(ws, subject_ids=[], roles=[existing.slug])
            for rule in rules:
                await uow.policies.remove_grant(ws, rule.id)
            await uow.workspaces.delete_role(ws, existing.id)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ROLE_DELETED,
                    subject_type="workspace_role",
                    subject_id=existing.id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    metadata={"role": existing.slug, "rules_removed": len(rules)},
                )
            )
            await uow.commit()
        return len(rules)

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
