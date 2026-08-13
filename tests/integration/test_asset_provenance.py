"""Identical bytes, two acquisitions, two provenance chains.

``register_asset`` deduplicated on (workspace, sha256) and returned the first
matching row. The same PDF acquired for a second work therefore came back attached
to the *first* work, carrying the first work's rights decision — so a citation into
it pointed at the wrong paper, and the second acquisition's rights decision was
never applied to anything.

Bytes still deduplicate: the object store is content-addressed, and one copy is
stored. What is no longer deduplicated is the acquisition.
"""

from __future__ import annotations

from typing import Any

import pytest

from lab_assistant.application.acquisition import AcquisitionService
from lab_assistant.contracts.connector import Identifier, IdentifierScheme
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.domain.assets import (
    AcquisitionPlan,
    AcquisitionRoute,
    Manifestation,
)
from lab_assistant.domain.identity import Work, WorkKind
from lab_assistant.domain.states import RelevanceState
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor

SHARED_BYTES = b"<article><doi>10.1234/shared</doi><p>Yield 0.21 g/g.</p></article>"

REQUESTED = frozenset(
    {
        PolicyAction.STORE_CONTENT,
        PolicyAction.PROCESS_LOCAL,
        PolicyAction.INDEX,
        PolicyAction.QUERY,
    }
)


@pytest.fixture
async def acquisition() -> dict[str, Any]:
    harness = build_harness()
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    actor = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    context = harness.context

    async def make_work_and_plan(label: str) -> tuple[Work, Manifestation, AcquisitionPlan]:
        async with context.uow_factory() as uow:
            work = Work(
                id=context.new_id(),
                workspace_id=workspace.id,
                work_kind=WorkKind.ARTICLE,
                relevance=RelevanceState.INCLUDED,
                created_at=context.now(),
            )
            await uow.works.add(work)
            manifestation = Manifestation(
                id=context.new_id(),
                workspace_id=workspace.id,
                work_id=work.id,
                manifestation_kind="version_of_record",
                label=label,
            )
            await uow.assets.add_manifestation(manifestation)
            plan = AcquisitionPlan(
                id=context.new_id(),
                workspace_id=workspace.id,
                work_id=work.id,
                manifestation_ids=(manifestation.id,),
                requested_actions=REQUESTED,
                status="planned",
                created_at=context.now(),
            )
            await uow.assets.add_plan(plan)
            await uow.commit()
        return work, manifestation, plan

    return {
        "harness": harness,
        "workspace": workspace,
        "actor": actor,
        "service": AcquisitionService(context),
        "make": make_work_and_plan,
    }


async def _register(acquisition, label: str, *, licence: str = "cc-by-4.0"):
    work, manifestation, plan = await acquisition["make"](label)
    service: AcquisitionService = acquisition["service"]
    actor = acquisition["actor"]
    decision = await service.decide_rights(
        actor,
        subject_type="manifestation",
        subject_id=manifestation.id,
        licence_expression=licence,
        access_basis="open_access_repository",
        route=AcquisitionRoute.OPEN_ACCESS_API,
        requested_actions=REQUESTED,
    )
    asset = await service.register_asset(
        actor=actor,
        plan=plan,
        manifestation=manifestation,
        content=SHARED_BYTES,
        declared_media_type="application/xml",
        filename=f"{label}.xml",
        rights_decision=decision,
        route=AcquisitionRoute.OPEN_ACCESS_API,
        connector_id="europepmc",
        expected_identifiers=(
            Identifier(scheme=IdentifierScheme.DOI, value="10.1234/shared"),
        ),
    )
    return work, manifestation, decision, asset


class TestSharedBytesKeepTheirProvenance:
    async def test_two_works_get_two_assets_over_one_object(self, acquisition) -> None:
        first_work, _fm, first_rights, first_asset = await _register(acquisition, "first")
        second_work, _sm, second_rights, second_asset = await _register(acquisition, "second")

        assert first_asset.id != second_asset.id
        assert first_asset.work_id == first_work.id
        assert second_asset.work_id == second_work.id
        # Bytes deduplicate: one object, one digest, two acquisitions.
        assert first_asset.sha256 == second_asset.sha256
        assert first_asset.object_id == second_asset.object_id
        # Each acquisition keeps its own rights decision. Sharing the first work's
        # decision is how a metadata-only licence would end up applied to content
        # acquired under a different basis.
        assert first_asset.rights_decision_id == first_rights.id
        assert second_asset.rights_decision_id == second_rights.id
        assert first_rights.id != second_rights.id

    async def test_re_running_the_same_acquisition_is_idempotent(self, acquisition) -> None:
        work, manifestation, _rights, asset = await _register(acquisition, "repeat")
        service: AcquisitionService = acquisition["service"]
        actor = acquisition["actor"]
        async with acquisition["harness"].context.uow_factory() as uow:
            plan = (await uow.assets.list_plans(acquisition["workspace"].id))[-1]
            live_manifestation = await uow.assets.get_manifestation(
                acquisition["workspace"].id, manifestation.id
            )
        assert live_manifestation is not None
        again = await service.register_asset(
            actor=actor,
            plan=plan,
            manifestation=live_manifestation,
            content=SHARED_BYTES,
            declared_media_type="application/xml",
            filename="repeat.xml",
            rights_decision=await service.decide_rights(
                actor,
                subject_type="manifestation",
                subject_id=manifestation.id,
                licence_expression="cc-by-4.0",
                access_basis="open_access_repository",
                route=AcquisitionRoute.OPEN_ACCESS_API,
                requested_actions=REQUESTED,
            ),
            route=AcquisitionRoute.OPEN_ACCESS_API,
            connector_id="europepmc",
            expected_identifiers=(),
        )
        assert again.id == asset.id
        assert again.work_id == work.id

    async def test_every_acquisition_is_audited(self, acquisition) -> None:
        await _register(acquisition, "audited-one")
        await _register(acquisition, "audited-two")
        actions = [
            event.action.value for event in acquisition["harness"].db.audit_events
        ]
        # Two registrations, not one: the second used to vanish with its asset.
        assert actions.count("asset_registered") == 2

    async def test_the_lookup_can_still_ask_about_the_bytes_alone(self, acquisition) -> None:
        _work, _manifestation, _rights, asset = await _register(acquisition, "digest-lookup")
        async with acquisition["harness"].context.uow_factory() as uow:
            by_digest = await uow.assets.find_asset_by_digest(
                acquisition["workspace"].id, asset.sha256
            )
            scoped = await uow.assets.find_asset_by_digest(
                acquisition["workspace"].id, asset.sha256, work_id=asset.work_id
            )
            other_work = await uow.assets.find_asset_by_digest(
                acquisition["workspace"].id, asset.sha256, work_id=acquisition["workspace"].id
            )
        assert by_digest is not None
        assert scoped is not None and scoped.id == asset.id
        assert other_work is None
