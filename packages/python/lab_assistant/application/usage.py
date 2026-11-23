"""Chat usage: each member's token allowance, and admin oversight of chats.

Carried over from the predecessor. Every answer records the tokens it used,
question and answer together, and a member may use up to their limit in total.
The limit is per membership: an admin sets it per member, and a member without
one has the installation default. Once the allowance is used up, asking is
refused until an admin raises it. Nothing resets it on a schedule, as before.

Admins may read members' conversations when ``chat_admin_access`` is on. That is
a deliberate policy of this installation, not a side effect: members are told on
the Ask page, and every read leaves an audit event naming who read whose
conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import BudgetExceeded, NotFound, PolicyDenied
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.domain.chat import ChatMessage, ChatSession, UsageTotals
from lab_assistant.domain.workspace import Capability, Membership

__all__ = ["Allowance", "TokenAllowanceExhausted", "UsageService"]


class TokenAllowanceExhausted(BudgetExceeded):
    """The member has used all the tokens their allowance permits."""


@dataclass(frozen=True, slots=True)
class Allowance:
    token_limit: int
    used_tokens: int
    #: True when the limit is the member's own, not the installation default.
    custom: bool

    @property
    def remaining(self) -> int:
        return max(0, self.token_limit - self.used_tokens)

    @property
    def reached(self) -> bool:
        return self.used_tokens >= self.token_limit

    def as_dict(self) -> dict[str, object]:
        return {
            "token_limit": self.token_limit,
            "used_tokens": self.used_tokens,
            "remaining_tokens": self.remaining,
            "limit_reached": self.reached,
            "custom_limit": self.custom,
        }


class UsageService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def limit_for(self, membership: Membership | None) -> tuple[int, bool]:
        """The member's limit, and whether it is their own rather than the default."""
        if membership is not None and membership.token_limit is not None:
            return membership.token_limit, True
        return self._context.settings.chat_token_limit_default, False

    async def allowance(self, actor: ActorContext) -> Allowance:
        """The caller's own allowance. Any member may ask about their own."""
        async with self._context.uow_factory() as uow:
            usage = await uow.chat.usage_by_user(actor.workspace_id, user_ids=[actor.user_id])
        limit, custom = self.limit_for(actor.membership)
        used = usage.get(actor.user_id, UsageTotals()).total_tokens
        return Allowance(token_limit=limit, used_tokens=used, custom=custom)

    async def require_allowance(self, actor: ActorContext) -> None:
        """Refuse a new question once the allowance is used up."""
        allowance = await self.allowance(actor)
        if allowance.reached:
            raise TokenAllowanceExhausted(
                "you have used your token allowance; ask an admin of this workspace to raise it",
                context=allowance.as_dict(),
            )

    async def members_usage(self, actor: ActorContext) -> dict[UUID, UsageTotals]:
        actor.require(Capability.MANAGE_MEMBERS)
        async with self._context.uow_factory() as uow:
            return await uow.chat.usage_by_user(actor.workspace_id)

    # ── oversight ───────────────────────────────────────────────────────────
    def _require_oversight(self, actor: ActorContext) -> None:
        actor.require(Capability.MANAGE_MEMBERS)
        if not self._context.settings.chat_admin_access:
            raise PolicyDenied("this installation does not let admins read members' chats")

    async def member_sessions(
        self,
        actor: ActorContext,
        *,
        user_id: UUID,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> tuple[list[ChatSession], int]:
        """A member's conversations, deleted ones included and marked as such.

        Newest first, one page, with how many there are. Listing titles is already
        reading: a title is the member's first question. So every page is audited
        like a conversation.
        """
        self._require_oversight(actor)
        context = self._context
        async with context.uow_factory() as uow:
            if await uow.workspaces.get_membership(actor.workspace_id, user_id) is None:
                raise NotFound(f"member {user_id} not found")
            sessions = await uow.chat.list_sessions(
                actor.workspace_id, user_id, limit=limit, before=before
            )
            total = await uow.chat.count_sessions(actor.workspace_id, user_id)
            await self._audit(uow, actor, subject_id=user_id, subject_type="user", scope="list")
            await uow.commit()
        return sessions, total

    async def member_session(
        self, actor: ActorContext, *, user_id: UUID, session_id: UUID
    ) -> tuple[ChatSession, list[ChatMessage]]:
        self._require_oversight(actor)
        context = self._context
        async with context.uow_factory() as uow:
            session = await uow.chat.get_session(actor.workspace_id, session_id)
            if session is None or session.user_id != user_id:
                raise NotFound(f"session {session_id} not found")
            messages = await uow.chat.list_messages(actor.workspace_id, session_id)
            await self._audit(
                uow,
                actor,
                subject_id=session_id,
                subject_type="chat_session",
                scope="read",
                member=user_id,
            )
            await uow.commit()
        return session, messages

    async def _audit(
        self,
        uow,
        actor: ActorContext,
        *,
        subject_id: UUID,
        subject_type: str,
        scope: str,
        member: UUID | None = None,
    ) -> None:
        await uow.audit.append(
            self._context.audit.build(
                workspace_id=actor.workspace_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                action=AuditAction.MEMBER_CHAT_READ,
                subject_type=subject_type,
                subject_id=subject_id,
                previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                reason_code=f"admin_oversight_{scope}",
                metadata={"member_user_id": str(member or subject_id)},
            )
        )
