"""Who may reach which scope, and so which papers.

A scope is one thing to a person, however many versions it has: "the Yarrowia
literature", not "Yarrowia v1, v2, v3". So access is granted on the **scope id**,
the canopy over every version, and a grant keeps meaning the same thing when a
steward saves the next version.

One action answers the question — ``query`` on ``resource_type=scope``. It means
"may this person reach the papers this scope found at all". What they may then do
with a paper they can reach — see its full content, export it — is still decided
per work, where it already was, so this adds a gate rather than moving one.

**How a paper is tied to a scope.** It is not stored on the paper. A paper
belongs to the scopes whose discovery runs found it, which the source assertions
already record, and ``works_found_by_runs`` already answers. Deriving it each time
costs one query and cannot go stale; a column on the paper would have to be
written by every path that creates one and corrected by every merge, and the
first path that forgot would silently widen or narrow who can read what.

**Admins** are let through by the policy engine itself, without a grant, so they
never reach the filtering here: ``reachable_work_ids`` returns ``None``, meaning
"every paper", and callers skip the filter entirely rather than build a set of
the whole corpus.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import NotFound, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.policy import (
    Effect,
    GrantSpec,
    PolicyAction,
    ResourceRef,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import Capability, Role

__all__ = ["ScopeAccessService", "reachable_scope_ids", "reachable_work_ids"]


async def reachable_scope_ids(
    uow: Any, context: ServiceContext, actor: ActorContext
) -> set[UUID] | None:
    """The scopes this caller may reach, or ``None`` when every one of them is.

    ``None`` is not the same as the empty set, and the difference is the whole
    point: no scope at all means no paper, while "all of them" means the caller
    is unrestricted and nothing needs filtering.

    Unrestricted is deliberately not "every paper some scope found". A paper no
    scope found — one uploaded by hand, or one whose discovery run was deleted —
    sits under no canopy and so is restricted by none. Filtering to the scoped
    set would hide those from everyone but an admin, which is a loss nobody asked
    for and one nothing on screen would explain.
    """
    versions = await uow.configs.list_scope_versions(actor.workspace_id)
    all_scope_ids = {version.scope_id for version in versions}
    # Evaluated with the workspace defaults emptied. `query` is a default member
    # action, so with them in place every member would reach every scope and a
    # grant could only ever take access away — the opposite of what was asked
    # for. Reaching a scope is granted, never assumed; an admin still gets
    # through, because the engine decides that before any of this.
    policy_input = actor.policy_input(now=context.now()).model_copy(
        update={"workspace_default_actions": frozenset()}
    )
    allowed = {
        scope_id
        for scope_id in all_scope_ids
        if context.policy_engine.allows(
            policy_input,
            action=PolicyAction.QUERY,
            resource=ResourceRef(resource_type=ResourceType.SCOPE, resource_id=scope_id),
        )
    }
    return None if allowed == all_scope_ids else allowed


async def reachable_work_ids(
    uow: Any, context: ServiceContext, actor: ActorContext
) -> set[UUID] | None:
    """Every paper this caller may reach, or ``None`` when that is all of them.

    A paper found by several scopes is reachable through any one of them, which
    is what ``works_found_by_runs`` returning both its sets gives: the papers only
    these runs found, and the papers they share with others.
    """
    scope_ids = await reachable_scope_ids(uow, context, actor)
    if scope_ids is None:
        return None
    if not scope_ids:
        return set()
    ws = actor.workspace_id
    versions = {
        version.id
        for version in await uow.configs.list_scope_versions(ws)
        if version.scope_id in scope_ids
    }
    runs = [
        run
        for run in await uow.observations.list_runs(ws, limit=100_000)
        if run.scope_version_id in versions
    ]
    if not runs:
        return set()
    only, shared = await uow.purge.works_found_by_runs(ws, [run.id for run in runs])
    return only | shared


class ScopeAccessService:
    """Reading and changing who may reach a scope.

    Changing it needs ``MANAGE_POLICY``, which only an admin holds: handing out
    access is an administrative act, not a curating one, and a data steward who
    could widen their own reach would make the grants decorative.

    Every call names a scope **version**, because that is what the pages have a
    link to, and every call acts on that version's ``scope_id``: one canopy, one
    set of rules, whichever version you came from.
    """

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    @staticmethod
    async def _role_name(uow: Any, workspace_id: UUID, role: str | Role) -> str:
        """The name a rule may hold: a built-in role, or one defined here."""
        text = role.value if isinstance(role, Role) else str(role).strip().lower()
        if any(built_in.value == text for built_in in Role):
            return text
        if await uow.workspaces.get_role(workspace_id, text) is None:
            raise ValidationFailure(f"{text!r} is not a role in this workspace")
        return text

    async def _canopy(self, uow: Any, workspace_id: UUID, scope_version_id: UUID) -> Any:
        version = await uow.configs.get_scope_version(workspace_id, scope_version_id)
        if version is None:
            raise NotFound(f"scope version {scope_version_id} not found")
        return version

    async def view(self, actor: ActorContext, scope_version_id: UUID) -> dict[str, Any]:
        """Who may reach this scope, and who holds no rule about it."""
        actor.require(Capability.MANAGE_SCOPE)
        ws = actor.workspace_id
        async with self._context.uow_factory() as uow:
            version = await self._canopy(uow, ws, scope_version_id)
            grants = await uow.policies.list_resource_grants(
                ws, resource_type=ResourceType.SCOPE.value, resource_id=version.scope_id
            )
            members = await uow.workspaces.list_memberships(ws)
            defined = await uow.workspaces.list_roles(ws)
        by_user = {m.user_id: m for m in members}
        return {
            "scope_id": str(version.scope_id),
            "name": version.name,
            "rules": [
                {
                    "id": str(grant.id),
                    "subject_type": grant.subject_type.value,
                    "subject_role": grant.subject_role,
                    "subject_id": str(grant.subject_id) if grant.subject_id else None,
                    "effect": grant.effect.value,
                    "policy_basis": grant.policy_basis,
                }
                for grant in sorted(grants, key=lambda g: (g.subject_type.value, str(g.id)))
            ],
            # Admins are left out: the engine lets them through without a rule, so
            # offering to write one for them would promise something it does not do.
            # The roles an admin defined here come last, because a rule about one
            # of those is the usual way confidential material is kept apart.
            "roles": [
                {"name": role.value, "label": role.value.replace("_", " "), "custom": False}
                for role in Role
                if role is not Role.ADMIN
            ]
            + [{"name": role.slug, "label": role.display_name, "custom": True} for role in defined],
            "members": [
                {
                    "user_id": str(m.user_id),
                    "role": m.role_key,
                    "reaches_everything": m.role is Role.ADMIN,
                }
                for m in sorted(by_user.values(), key=lambda m: str(m.user_id))
            ],
        }

    async def allow(
        self,
        actor: ActorContext,
        scope_version_id: UUID,
        *,
        role: str | Role | None = None,
        user_id: UUID | None = None,
        effect: Effect = Effect.ALLOW,
    ) -> UUID:
        """Write one rule about this scope, for a role or for one person.

        The role is named, not referenced: a built-in one, or one an admin
        defined here. An unknown name is refused rather than written, because a
        rule nothing can match looks on screen exactly like access that works.
        """
        actor.require(Capability.MANAGE_POLICY)
        if (role is None) == (user_id is None):
            raise ValidationFailure("name either a role or a person, not both and not neither")
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            named = await self._role_name(uow, ws, role) if role is not None else None
            version = await self._canopy(uow, ws, scope_version_id)
            grant = GrantSpec(
                id=context.new_id(),
                workspace_id=ws,
                subject_type=SubjectType.ROLE if named else SubjectType.USER,
                subject_role=named,
                subject_id=None if named else user_id,
                resource_type=ResourceType.SCOPE,
                resource_id=version.scope_id,
                action=PolicyAction.QUERY,
                effect=effect,
                policy_basis="steward_decision",
            )
            await uow.policies.add_grant(grant)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.GRANT_CHANGED,
                    subject_type="scope",
                    subject_id=version.scope_id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="scope_access_granted",
                    metadata={
                        "grant_id": str(grant.id),
                        "effect": effect.value,
                        "role": named,
                        "user_id": str(user_id) if user_id else None,
                        "scope_name": version.name,
                    },
                )
            )
            await uow.commit()
        return grant.id

    async def revoke(self, actor: ActorContext, scope_version_id: UUID, grant_id: UUID) -> None:
        """Take one rule away. What it granted goes with it, and nothing else does."""
        actor.require(Capability.MANAGE_POLICY)
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            version = await self._canopy(uow, ws, scope_version_id)
            existing = await uow.policies.list_resource_grants(
                ws, resource_type=ResourceType.SCOPE.value, resource_id=version.scope_id
            )
            if not any(grant.id == grant_id for grant in existing):
                raise NotFound(f"no rule {grant_id} on this scope")
            await uow.policies.remove_grant(ws, grant_id)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.GRANT_CHANGED,
                    subject_type="scope",
                    subject_id=version.scope_id,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="scope_access_revoked",
                    metadata={"grant_id": str(grant_id), "scope_name": version.name},
                )
            )
            await uow.commit()
