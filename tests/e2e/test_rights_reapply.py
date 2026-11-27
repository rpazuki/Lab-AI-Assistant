# ruff: noqa: F811  -- `world` is the fixture shared from test_reader_import.
"""Decisions made before replication was allowed, re-decided, reach a mirror.

A corpus decided before ``LAB_RIGHTS_REPLICATE_OPEN_CONTENT`` was turned on has
no ``replicate_content`` anywhere, so its snapshots carry records and no text and
a mirror can answer nothing. ``RightsReapplyService`` applies the current policy
to those decisions again; the next publish carries the text, and a mirror that
already imported the release gains it on its next sync.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.application.rights_reapply import REAPPLIED_NOTE, RightsReapplyService
from lab_assistant.contracts.errors import InstallationDenied
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.domain.states import RightsState
from lab_assistant.fixtures.archive import MEASURED
from tests.e2e.test_reader_import import world  # noqa: F401

REPLICATE = PolicyAction.REPLICATE_CONTENT
QUERY = "lipid yield engineered strain"


async def _decided_before_replication(producer, workspace_id) -> int:
    """Take replicate_content off every decision, as one made before the setting."""
    async with producer.context.uow_factory() as uow:
        decisions = await uow.policies.list_rights_decisions(workspace_id)
        for decision in decisions:
            await uow.policies.add_rights_decision(
                decision.model_copy(
                    update={"allowed_actions": decision.allowed_actions - {REPLICATE}}
                )
            )
        await uow.commit()
    return len(decisions)


async def _sync(world):
    await world.publish()
    report = await world.importer().import_snapshot(world.reader_ws.id, await world.fetch())
    assert report.ok, report.problems
    return report


async def _found(world) -> set[str]:
    result = await RetrievalService(world.reader.context).retrieve(world.reader_actor, query=QUERY)
    return {block.work_title for block in result.evidence}


class TestReapplyingReachesAMirror:
    async def test_old_decisions_send_no_text_until_reapplied(self, world) -> None:
        ws = world.producer_ws.id
        assert await _decided_before_replication(world.producer, ws)

        first = await _sync(world)
        assert first.counts.get("projection_chunk", 0) == 0
        assert MEASURED not in await _found(world)

        service = RightsReapplyService(world.producer.context)
        dry = await service.reapply(world.producer_actor)
        assert dry.widened > 0 and not dry.applied
        async with world.producer.context.uow_factory() as uow:
            unchanged = await uow.policies.list_rights_decisions(ws)
        assert not any(REPLICATE in d.allowed_actions for d in unchanged)

        applied = await service.reapply(world.producer_actor, apply=True)
        assert applied.widened == dry.widened
        async with world.producer.context.uow_factory() as uow:
            after = await uow.policies.list_rights_decisions(ws)
        widened = [d for d in after if REPLICATE in d.allowed_actions]
        assert len(widened) == applied.widened
        assert {d.note for d in widened} == {REAPPLIED_NOTE}
        assert {d.decided_by for d in widened} == {world.producer_actor.user_id}

        # The mirror already holds this release: the next sync brings its text.
        second = await _sync(world)
        assert second.counts.get("projection_chunk", 0) > 0
        assert MEASURED in await _found(world)

        # Nothing is left to do, and a second run says so.
        again = await service.reapply(world.producer_actor, apply=True)
        assert again.widened == 0
        assert again.skipped["already_granted"] == applied.widened

    async def test_it_never_revives_or_reaches_past_a_review_date(self, world) -> None:
        ws = world.producer_ws.id
        await _decided_before_replication(world.producer, ws)
        context = world.producer.context
        async with context.uow_factory() as uow:
            decisions = sorted(
                await uow.policies.list_rights_decisions(ws), key=lambda d: str(d.id)
            )
            prohibited, lapsed = decisions[0], decisions[1]
            await uow.policies.add_rights_decision(
                prohibited.model_copy(update={"status": RightsState.PROHIBITED})
            )
            await uow.policies.add_rights_decision(
                lapsed.model_copy(update={"review_due_at": context.now() - timedelta(days=1)})
            )
            await uow.commit()
        report = await RightsReapplyService(context).reapply(world.producer_actor, apply=True)
        assert report.skipped["not_permitted"] >= 1
        assert report.skipped["past_review_date"] >= 1
        async with context.uow_factory() as uow:
            kept = {d.id: d for d in await uow.policies.list_rights_decisions(ws)}
        assert REPLICATE not in kept[prohibited.id].allowed_actions
        assert kept[prohibited.id].status is RightsState.PROHIBITED
        assert REPLICATE not in kept[lapsed.id].allowed_actions

    async def test_a_mirror_cannot_re_decide(self, world) -> None:
        with pytest.raises(InstallationDenied):
            await RightsReapplyService(world.reader.context).reapply(world.reader_actor)

    async def test_a_payload_taken_down_on_the_mirror_stays_down(self, world) -> None:
        ws = world.producer_ws.id
        await _decided_before_replication(world.producer, ws)
        await _sync(world)
        reader = world.reader.context
        async with reader.uow_factory() as uow:
            withheld = [
                o
                for o in await uow.objects.list_objects(world.reader_ws.id, limit=500)
                if o.tombstoned
            ]
            taken_down = withheld[0]
            await uow.objects.tombstone(taken_down.id, reason="takedown_notice")
            await uow.commit()

        await RightsReapplyService(world.producer.context).reapply(world.producer_actor, apply=True)
        await _sync(world)
        async with reader.uow_factory() as uow:
            after = await uow.objects.get(taken_down.id)
            others = [await uow.objects.get(o.id) for o in withheld[1:]]
        assert after.tombstoned and after.tombstone_reason == "takedown_notice"
        # The rest were withheld only because of the old decisions: held now.
        assert any(not o.tombstoned and o.storage_key for o in others)
