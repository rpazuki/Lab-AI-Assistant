"""Access control per scope: who reaches which papers, and so which answers.

A scope is one thing however many versions it has, so access is granted on the
scope id. Admins are let through without a grant; everyone else needs one, for
their own account or for the role their membership carries.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_bulk_review import lab  # noqa: F401  (the fixture)
from test_scope_deletion import DeletionLab

from lab_assistant.application.scope_access import (
    ScopeAccessService,
    reachable_scope_ids,
    reachable_work_ids,
)
from lab_assistant.contracts.errors import PolicyDenied, ValidationFailure
from lab_assistant.contracts.policy import (
    Effect,
    GrantSpec,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import make_actor


@pytest.fixture
def scoped(lab) -> DeletionLab:  # noqa: F811
    return DeletionLab(lab.harness, lab.actor)


async def _grant(where, *, role: Role | None = None, user=None, scope_id, effect=Effect.ALLOW):
    context = where.harness.context
    async with context.uow_factory() as uow:
        await uow.policies.add_grant(
            GrantSpec(
                id=context.new_id(),
                workspace_id=where.actor.workspace_id,
                subject_type=SubjectType.ROLE if role else SubjectType.USER,
                subject_role=role.value if role else None,
                subject_id=None if role else user,
                resource_type=ResourceType.SCOPE,
                resource_id=scope_id,
                action=PolicyAction.QUERY,
                effect=effect,
                policy_basis="steward_decision",
            )
        )
        await uow.commit()


async def _actor(where, role: Role):
    """A caller carrying one role and the grants that name them or it.

    Built the way ``resolve_actor`` builds one — grants for the user, and grants
    for the role their membership carries — because a synthetic actor holding no
    grants would pass every case below by proving nothing.
    """
    ws = where.actor.workspace_id
    async with where.harness.context.uow_factory() as uow:
        grants = await uow.policies.list_grants(
            ws, subject_ids=[where.actor.user_id, ws], roles=[role.value]
        )
    return replace(
        make_actor(user_id=where.actor.user_id, workspace_id=ws, role=role),
        grants=tuple(grants),
    )


async def _reachable(where, role: Role):
    async with where.harness.context.uow_factory() as uow:
        return await reachable_work_ids(uow, where.harness.context, await _actor(where, role))


class TestWhoReachesAScope:
    async def test_with_no_scope_at_all_nobody_is_restricted(self, scoped) -> None:
        """An empty workspace restricts nothing: there is nothing to restrict."""
        assert await _reachable(scoped, Role.RESEARCHER) is None

    async def test_a_scope_with_no_grant_is_reached_by_nobody_but_an_admin(
        self, scoped: DeletionLab
    ) -> None:
        scope = await scoped.scope()
        run = await scoped.run(scope)
        paper = await scoped.screened("Yarrowia lipid factories")
        await scoped.found(run, paper)

        # `None` is "everything", which is what an admin gets without a grant.
        assert await _reachable(scoped, Role.ADMIN) is None
        assert await _reachable(scoped, Role.RESEARCHER) == set()

    async def test_a_role_grant_opens_one_scope_and_not_the_other(
        self, scoped: DeletionLab
    ) -> None:
        mine, theirs = await scoped.scope(), await scoped.scope()
        ours, hidden = await scoped.run(mine), await scoped.run(theirs, query="q-other")
        seen = await scoped.screened("Yarrowia lipid factories")
        unseen = await scoped.screened("Something else entirely")
        await scoped.found(ours, seen)
        await scoped.found(hidden, unseen)
        await _grant(scoped, role=Role.RESEARCHER, scope_id=mine.scope_id)

        assert await _reachable(scoped, Role.RESEARCHER) == {seen}
        # A reviewer holds a different role and was named by nothing.
        assert await _reachable(scoped, Role.REVIEWER) == set()

    async def test_every_version_of_a_scope_is_one_canopy(self, scoped: DeletionLab) -> None:
        """A grant is on the scope, so saving a new version never changes who reads it."""
        v1 = await scoped.scope()
        v2 = await scoped.scope(version=2, scope_id=v1.scope_id)
        theirs = await scoped.scope()
        first, second = await scoped.run(v1), await scoped.run(v2)
        hidden = await scoped.run(theirs, query="q-other")
        old_paper = await scoped.screened("Yarrowia lipid factories")
        new_paper = await scoped.screened("Yarrowia lipid factories, again")
        unseen = await scoped.screened("Something else entirely")
        await scoped.found(first, old_paper)
        await scoped.found(second, new_paper)
        await scoped.found(hidden, unseen)
        await _grant(scoped, role=Role.RESEARCHER, scope_id=v1.scope_id)

        # Granted once, on the canopy: both versions' papers, and only those.
        assert await _reachable(scoped, Role.RESEARCHER) == {old_paper, new_paper}

    async def test_reaching_every_scope_restricts_nothing_at_all(self, scoped: DeletionLab) -> None:
        """Not the same as "reaches every paper found by a scope".

        A paper that no scope found — an uploaded PDF, or one whose run was
        deleted — belongs to no canopy and so is restricted by none. Returning
        the set of scoped papers here would hide those from everyone but an
        admin, which is a loss nobody asked for.
        """
        only_scope = await scoped.scope()
        await scoped.found(await scoped.run(only_scope), await scoped.screened("Yarrowia"))
        await _grant(scoped, role=Role.RESEARCHER, scope_id=only_scope.scope_id)

        assert await _reachable(scoped, Role.RESEARCHER) is None

    async def test_an_explicit_deny_beats_a_role_grant_even_for_an_admin(
        self, scoped: DeletionLab
    ) -> None:
        """Admins are let through, but not past something written to stop them."""
        scope = await scoped.scope()
        await _grant(scoped, role=Role.ADMIN, scope_id=scope.scope_id, effect=Effect.DENY)

        async with scoped.harness.context.uow_factory() as uow:
            allowed = await reachable_scope_ids(
                uow, scoped.harness.context, await _actor(scoped, Role.ADMIN)
            )
        assert allowed == set()


class TestWhatAnAdminStillCannotReach:
    """The bypass waives the defaults, not the things written down.

    An admin needs no grant, which is what makes a workspace recoverable. It does
    not follow that an admin overrides a refusal someone recorded, nor an
    agreement with another institution.
    """

    async def test_an_explicit_deny_still_stops_them(self, scoped: DeletionLab) -> None:
        mine, theirs = await scoped.scope(), await scoped.scope()
        await _grant(scoped, role=Role.ADMIN, scope_id=mine.scope_id, effect=Effect.DENY)

        async with scoped.harness.context.uow_factory() as uow:
            allowed = await reachable_scope_ids(
                uow, scoped.harness.context, await _actor(scoped, Role.ADMIN)
            )

        assert allowed == {theirs.scope_id}


class TestHandingOutAccess:
    """Reading who may reach a scope, and changing it."""

    async def test_a_steward_may_read_the_rules_but_not_write_them(
        self, scoped: DeletionLab
    ) -> None:
        """Widening your own reach is an administrative act, not a curating one."""
        scope = await scoped.scope()
        service = ScopeAccessService(scoped.harness.context)
        steward = await _actor(scoped, Role.DATA_STEWARD)

        assert (await service.view(steward, scope.id))["rules"] == []
        with pytest.raises(PolicyDenied):
            await service.allow(steward, scope.id, role=Role.RESEARCHER)

    async def test_an_admin_grants_a_role_and_the_grant_takes_effect(
        self, scoped: DeletionLab
    ) -> None:
        mine, theirs = await scoped.scope(), await scoped.scope()
        paper = await scoped.screened("Yarrowia lipid factories")
        await scoped.found(await scoped.run(mine), paper)
        await scoped.found(await scoped.run(theirs, query="q-other"), await scoped.screened("Else"))
        service = ScopeAccessService(scoped.harness.context)
        admin = await _actor(scoped, Role.ADMIN)

        assert await _reachable(scoped, Role.RESEARCHER) == set()
        grant_id = await service.allow(admin, mine.id, role=Role.RESEARCHER)
        assert await _reachable(scoped, Role.RESEARCHER) == {paper}

        view = await service.view(admin, mine.id)
        assert [rule["subject_role"] for rule in view["rules"]] == ["researcher"]
        # Admins are not offered: the engine lets them through without a rule.
        assert "admin" not in [choice["name"] for choice in view["roles"]]

        await service.revoke(admin, mine.id, grant_id)
        assert await _reachable(scoped, Role.RESEARCHER) == set()

    async def test_a_rule_named_from_any_version_lands_on_the_canopy(
        self, scoped: DeletionLab
    ) -> None:
        v1 = await scoped.scope()
        v2 = await scoped.scope(version=2, scope_id=v1.scope_id)
        service = ScopeAccessService(scoped.harness.context)
        admin = await _actor(scoped, Role.ADMIN)

        await service.allow(admin, v2.id, role=Role.REVIEWER)

        # Asked from the other version, the same rule is there.
        assert [r["subject_role"] for r in (await service.view(admin, v1.id))["rules"]] == [
            "reviewer"
        ]

    async def test_a_rule_names_a_role_or_a_person_and_not_both(self, scoped: DeletionLab) -> None:
        scope = await scoped.scope()
        service = ScopeAccessService(scoped.harness.context)
        admin = await _actor(scoped, Role.ADMIN)

        with pytest.raises(ValidationFailure):
            await service.allow(admin, scope.id, role=Role.RESEARCHER, user_id=admin.user_id)
        with pytest.raises(ValidationFailure):
            await service.allow(admin, scope.id)
