"""Roles an admin defines, and what a rule naming one reaches.

The built-in four are capability bundles: what a person may *do*. They do not
say who may reach patented material, work under embargo, or a dataset held under
an agreement, because that is a decision about particular people and it outlives
any one of them.

A role defined here gives that decision a name. It carries no capabilities of its
own — ``base_role`` points at a built-in bundle — so the new thing is the name,
and the name is what a rule on a scope is written about.
"""

from __future__ import annotations

import pytest
from test_bulk_review import lab  # noqa: F401  (the fixture)
from test_scope_deletion import DeletionLab

from lab_assistant.application.context import resolve_actor
from lab_assistant.application.scope_access import ScopeAccessService, reachable_work_ids
from lab_assistant.contracts.errors import NotFound, PolicyDenied, ValidationFailure
from lab_assistant.contracts.policy import Effect
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import make_actor


@pytest.fixture
def scoped(lab) -> DeletionLab:  # noqa: F811
    return DeletionLab(lab.harness, lab.actor)


def _admin(where: DeletionLab):
    return make_actor(user_id=where.actor.user_id, workspace_id=where.ws, role=Role.ADMIN)


async def _person(where: DeletionLab, email: str, role: str | Role):
    """A real second member, resolved the way a request resolves its caller.

    Not ``make_actor``: that one carries no grants, so it would reach whatever
    the test expected by proving nothing. ``resolve_actor`` loads the grants for
    the person and for every role name their membership answers to, which is the
    thing under test.
    """
    accounts = where.harness.accounts
    user = await accounts.create_user(email=email, password="a-long-passphrase")
    await accounts.add_member(_admin(where), user_id=user.id, role=role)
    return await resolve_actor(where.harness.context, user_id=user.id, workspace_id=where.ws)


async def _reaches(where: DeletionLab, actor):
    async with where.harness.context.uow_factory() as uow:
        return await reachable_work_ids(uow, where.harness.context, actor)


class TestDefiningARole:
    async def test_an_admin_defines_one_and_it_joins_the_list(self, scoped: DeletionLab) -> None:
        accounts = scoped.harness.accounts
        admin = _admin(scoped)

        role = await accounts.create_role(
            admin,
            name="Patent holders",
            display_name="Patent holders",
            description="Named on the filings",
            base_role=Role.REVIEWER,
        )

        # The typed name becomes the slug rules are written about; the wording
        # the admin chose is kept beside it for the screen.
        assert role.slug == "patent_holders"
        assert role.display_name == "Patent holders"
        assert role.capabilities == Role.REVIEWER.capabilities

        listed = {choice.name: choice for choice in await accounts.list_roles(admin)}
        assert listed["patent_holders"].custom is True
        assert listed["patent_holders"].members == 0
        assert listed["researcher"].custom is False

    async def test_a_role_cannot_take_a_built_in_name(self, scoped: DeletionLab) -> None:
        """One namespace: a rule says ``reviewer`` and nothing can tell which it meant."""
        with pytest.raises(ValidationFailure, match="built-in"):
            await scoped.harness.accounts.create_role(_admin(scoped), name="reviewer")

    async def test_a_role_cannot_be_based_on_admin(self, scoped: DeletionLab) -> None:
        """Nothing holds an admin back, so a role based on one could not be held back."""
        with pytest.raises(ValidationFailure, match="cannot be based on admin"):
            await scoped.harness.accounts.create_role(
                _admin(scoped), name="patents", base_role=Role.ADMIN
            )

    async def test_two_roles_cannot_share_a_name(self, scoped: DeletionLab) -> None:
        accounts = scoped.harness.accounts
        await accounts.create_role(_admin(scoped), name="patent holders")
        with pytest.raises(ValidationFailure, match="already exists"):
            await accounts.create_role(_admin(scoped), name="Patent Holders")

    async def test_a_steward_cannot_define_one(self, scoped: DeletionLab) -> None:
        """Defining a role decides what rules *can* say, so it is an admin's call.

        Someone who could invent a name and then be given it would be deciding
        their own reach, which is the thing the rules exist to stop.
        """
        steward = make_actor(
            user_id=scoped.actor.user_id, workspace_id=scoped.ws, role=Role.DATA_STEWARD
        )
        with pytest.raises(PolicyDenied):
            await scoped.harness.accounts.create_role(steward, name="patents")


class TestWhatARoleReaches:
    async def test_a_rule_on_it_opens_a_scope_to_its_members_and_nobody_else(
        self, scoped: DeletionLab
    ) -> None:
        confidential, ordinary = await scoped.scope(), await scoped.scope()
        # A third, opened to nobody: reaching *every* scope means unrestricted,
        # so without one the insider's answer would be "everything" and the
        # assertion would be about nothing.
        sealed = await scoped.scope()
        patented = await scoped.screened("A patented process")
        public = await scoped.screened("Something already published")
        await scoped.found(await scoped.run(confidential), patented)
        await scoped.found(await scoped.run(ordinary, query="q-other"), public)
        await scoped.found(await scoped.run(sealed, query="q-sealed"), await scoped.screened("X"))
        accounts, admin = scoped.harness.accounts, _admin(scoped)
        await accounts.create_role(admin, name="patent holders")
        await ScopeAccessService(scoped.harness.context).allow(
            admin, confidential.id, role="patent_holders"
        )
        await ScopeAccessService(scoped.harness.context).allow(
            admin, ordinary.id, role=Role.RESEARCHER
        )

        insider = await _person(scoped, "insider@lab.invalid", "patent_holders")
        outsider = await _person(scoped, "outsider@lab.invalid", Role.RESEARCHER)

        # The insider is also a researcher, so they keep the ordinary scope too.
        assert await _reaches(scoped, insider) == {patented, public}
        assert await _reaches(scoped, outsider) == {public}

    async def test_being_put_in_one_takes_nothing_away(self, scoped: DeletionLab) -> None:
        """A defined role *adds* an audience: both names reach its members.

        The other reading — the new name replacing the old one — would strip
        somebody of everything they could read the moment an admin moved them,
        silently, as a side effect of a change meant to give them more.
        """
        ordinary, other = await scoped.scope(), await scoped.scope()
        paper = await scoped.screened("Something already published")
        await scoped.found(await scoped.run(ordinary), paper)
        await scoped.found(await scoped.run(other, query="q-other"), await scoped.screened("Else"))
        admin = _admin(scoped)
        await scoped.harness.accounts.create_role(admin, name="patent holders")
        await ScopeAccessService(scoped.harness.context).allow(
            admin, ordinary.id, role=Role.RESEARCHER
        )

        moved = await _person(scoped, "moved@lab.invalid", "patent_holders")

        assert await _reaches(scoped, moved) == {paper}

    async def test_a_deny_on_it_takes_back_what_the_base_role_opened(
        self, scoped: DeletionLab
    ) -> None:
        """How a compartment is made: open to all, closed to one named group.

        A refusal is weighed before any allow, so the deny on the defined role
        beats the allow their base role carries.
        """
        shared, other = await scoped.scope(), await scoped.scope()
        paper = await scoped.screened("Under embargo until the filing")
        await scoped.found(await scoped.run(shared), paper)
        await scoped.found(await scoped.run(other, query="q-other"), await scoped.screened("Else"))
        service = ScopeAccessService(scoped.harness.context)
        admin = _admin(scoped)
        await scoped.harness.accounts.create_role(admin, name="contractors")
        await service.allow(admin, shared.id, role=Role.RESEARCHER)
        await service.allow(admin, shared.id, role="contractors", effect=Effect.DENY)

        contractor = await _person(scoped, "contractor@lab.invalid", "contractors")
        staff = await _person(scoped, "staff@lab.invalid", Role.RESEARCHER)

        assert await _reaches(scoped, contractor) == set()
        assert await _reaches(scoped, staff) == {paper}

    async def test_a_rule_cannot_name_a_role_that_does_not_exist(self, scoped: DeletionLab) -> None:
        """A rule nothing can match looks on screen exactly like access that works."""
        scope = await scoped.scope()
        with pytest.raises(ValidationFailure, match="not a role"):
            await ScopeAccessService(scoped.harness.context).allow(
                _admin(scoped), scope.id, role="patent_holders"
            )

    async def test_the_access_panel_offers_the_roles_defined_here(
        self, scoped: DeletionLab
    ) -> None:
        scope = await scoped.scope()
        admin = _admin(scoped)
        await scoped.harness.accounts.create_role(
            admin, name="patent holders", display_name="Patent holders"
        )

        view = await ScopeAccessService(scoped.harness.context).view(admin, scope.id)

        offered = {choice["name"]: choice for choice in view["roles"]}
        assert offered["patent_holders"] == {
            "name": "patent_holders",
            "label": "Patent holders",
            "custom": True,
        }
        assert offered["researcher"]["custom"] is False


class TestPuttingPeopleInARole:
    async def test_a_member_answers_to_both_names(self, scoped: DeletionLab) -> None:
        await scoped.harness.accounts.create_role(
            _admin(scoped), name="patent holders", base_role=Role.REVIEWER
        )

        member = await _person(scoped, "both@lab.invalid", "patent_holders")

        assert member.membership is not None
        assert member.membership.role is Role.REVIEWER
        assert member.membership.role_key == "patent_holders"
        assert member.membership.role_keys == {"reviewer", "patent_holders"}
        # Capabilities come from the base, so the role grants nothing by itself.
        assert member.membership.capabilities() == Role.REVIEWER.capabilities

    async def test_a_role_nobody_defined_is_refused(self, scoped: DeletionLab) -> None:
        user = await scoped.harness.accounts.create_user(
            email="nobody@lab.invalid", password="a-long-passphrase"
        )
        with pytest.raises(ValidationFailure, match="not a role"):
            await scoped.harness.accounts.add_member(
                _admin(scoped), user_id=user.id, role="patent_holders"
            )

    async def test_a_head_count_says_who_is_in_each(self, scoped: DeletionLab) -> None:
        accounts, admin = scoped.harness.accounts, _admin(scoped)
        await accounts.create_role(admin, name="patent holders")
        await _person(scoped, "one@lab.invalid", "patent_holders")

        listed = {choice.name: choice.members for choice in await accounts.list_roles(admin)}

        assert listed["patent_holders"] == 1
        # The owner of the workspace is its admin, and holds no defined role.
        assert listed["admin"] == 1


class TestChangingARole:
    async def test_correcting_the_wording_leaves_what_it_reaches_alone(
        self, scoped: DeletionLab
    ) -> None:
        """An admin may reword a role in use, and the slug must not move with it.

        Every rule that names the role and every membership that holds it stores
        the slug. If a correction moved it, the rule would stay on screen looking
        live while matching nobody — which is the one failure that would not show
        until somebody could not read what they were meant to.
        """
        confidential = await scoped.scope()
        # A second scope, opened to nobody: reaching every scope means
        # unrestricted, and the answer would then be "everything" rather than
        # the set under test.
        sealed = await scoped.scope()
        patented = await scoped.screened("A patented process")
        await scoped.found(await scoped.run(confidential), patented)
        await scoped.found(await scoped.run(sealed, query="q-sealed"), await scoped.screened("X"))
        accounts, admin = scoped.harness.accounts, _admin(scoped)
        role = await accounts.create_role(admin, name="patent holders", description="first pass")
        service = ScopeAccessService(scoped.harness.context)
        await service.allow(admin, confidential.id, role="patent_holders")
        insider = await _person(scoped, "insider@lab.invalid", "patent_holders")

        changed = await accounts.update_role(
            admin,
            role_id=role.id,
            display_name="Patent inventors",
            description="Named on the EP filings",
        )

        assert changed.slug == "patent_holders"
        assert changed.display_name == "Patent inventors"
        assert changed.description == "Named on the EP filings"
        assert changed.base_role is role.base_role
        after = await resolve_actor(
            scoped.harness.context, user_id=insider.user_id, workspace_id=scoped.ws
        )
        assert await _reaches(scoped, after) == {patented}
        # The rule still holds the slug, and the menus offer the new wording
        # under it, so the two readings cannot drift apart.
        view = await service.view(admin, confidential.id)
        assert [rule["subject_role"] for rule in view["rules"]] == ["patent_holders"]
        assert {
            "name": "patent_holders",
            "label": "Patent inventors",
            "custom": True,
        } in view["roles"]

    async def test_changing_what_it_is_based_on_moves_the_people_in_it(
        self, scoped: DeletionLab
    ) -> None:
        """Capabilities come from the base, which a membership stores beside the slug.

        Leaving the members behind would hold them to the old bundle while the
        role said otherwise, which is the kind of disagreement nobody would find
        until it mattered.
        """
        accounts, admin = scoped.harness.accounts, _admin(scoped)
        role = await accounts.create_role(admin, name="patent holders")
        member = await _person(scoped, "moved@lab.invalid", "patent_holders")

        await accounts.update_role(admin, role_id=role.id, base_role=Role.DATA_STEWARD)

        after = await resolve_actor(
            scoped.harness.context, user_id=member.user_id, workspace_id=scoped.ws
        )
        assert after.membership is not None
        assert after.membership.role is Role.DATA_STEWARD
        assert after.membership.role_key == "patent_holders"

    async def test_a_role_someone_holds_cannot_be_removed(self, scoped: DeletionLab) -> None:
        """Taking the name away underneath them would move them with no record of where to."""
        accounts, admin = scoped.harness.accounts, _admin(scoped)
        role = await accounts.create_role(admin, name="patent holders")
        await _person(scoped, "held@lab.invalid", "patent_holders")

        with pytest.raises(ValidationFailure, match="still in"):
            await accounts.delete_role(admin, role_id=role.id)

    async def test_removing_one_takes_its_rules_with_it(self, scoped: DeletionLab) -> None:
        """A rule naming a role nobody can hold is access that looks live and is not."""
        scope = await scoped.scope()
        accounts, admin = scoped.harness.accounts, _admin(scoped)
        role = await accounts.create_role(admin, name="patent holders")
        service = ScopeAccessService(scoped.harness.context)
        await service.allow(admin, scope.id, role="patent_holders")
        await service.allow(admin, scope.id, role=Role.RESEARCHER)

        removed = await accounts.delete_role(admin, role_id=role.id)

        assert removed == 1
        left = [rule["subject_role"] for rule in (await service.view(admin, scope.id))["rules"]]
        assert left == ["researcher"]
        assert [choice.name for choice in await accounts.list_roles(admin) if choice.custom] == []

    async def test_removing_one_that_was_never_there_is_a_not_found(
        self, scoped: DeletionLab
    ) -> None:
        with pytest.raises(NotFound):
            await scoped.harness.accounts.delete_role(_admin(scoped), role_id=scoped.ws)
