"""Deleting what nothing depends on, and refusing the rest with a reason.

Each entity has one point after which deletion is refused: a frozen run whose
candidates exist, an approved release, a succeeded evaluation run, an accepted
invitation. Before that point a delete removes the entity, its own child rows and
the queue tasks that would otherwise act on something gone, and leaves an audit
event behind.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from lab_assistant.application.datasheets import DatasheetService
from lab_assistant.application.discovery import DiscoveryService, cursor_lineage_key
from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
from lab_assistant.application.scopes import ScopeService
from lab_assistant.contracts.errors import NotFound, PolicyDenied, StateTransitionError
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.scope import ConnectorPlanEntry
from lab_assistant.domain.states import ReleaseState, TaskState
from lab_assistant.domain.workspace import Role
from lab_assistant.evaluation.service import EvaluationService
from lab_assistant.evaluation.workbench import EvaluationWorkbench

QUERY = "yarrowia lipolytica lipid production"


async def _scope(harness, actor) -> Any:
    scopes = ScopeService(harness.context)
    draft = await scopes.create_version(
        actor,
        scope_id=None,
        name="Yarrowia lipids",
        scientific_objective="Lipid yields in engineered oleaginous yeast.",
        connector_plan=[
            ConnectorPlanEntry(
                connector_id="pubmed-fixture", connector_version="1.0.0", query=QUERY
            )
        ],
        intended_uses=("internal literature search",),
        concepts=("lipid production",),
    )
    return await scopes.approve(actor, scope_version_id=draft.id)


async def _run(harness, actor, scope, *, execute: bool = True):
    run, requests = await DiscoveryService(harness.context).start_run(
        actor, scope_version_id=scope.id
    )
    await harness.orchestrator.submit_requests(
        requests, workspace_id=actor.workspace_id, actor_id=actor.user_id, run_id=run.id
    )
    if execute:
        await harness.run_all_tasks()
    return run


async def _audited(harness, workspace_id, action: str) -> bool:
    async with harness.context.uow_factory() as uow:
        events = await uow.audit.list_events(workspace_id, action=action)
    return bool(events)


class TestDiscoveryRuns:
    async def test_an_unfrozen_run_goes_with_its_pages_tasks_and_cursor(
        self, harness, provisioned
    ) -> None:
        actor = provisioned["steward_actor"]
        workspace = provisioned["workspace"]
        scope = await _scope(harness, actor)
        run = await _run(harness, actor, scope)
        lineage = cursor_lineage_key(
            workspace_id=workspace.id,
            scope_version_id=scope.id,
            connector_id="pubmed-fixture",
            connector_version="1.0.0",
            query_fingerprint=run.connector_states[0].query_fingerprint,
            cursor_semantics_version="1",
        )
        async with harness.context.uow_factory() as uow:
            assert await uow.observations.list_requests(workspace.id, run.id)
            # Where an interrupted run would have left a multi-page search.
            await uow.observations.set_cursor(
                workspace.id, lineage_key=lineage, position="page-7", exhausted=False
            )
            await uow.commit()

        await DiscoveryService(harness.context).delete_run(actor, run_id=run.id)

        async with harness.context.uow_factory() as uow:
            assert await uow.observations.get_run(workspace.id, run.id) is None
            assert await uow.observations.list_requests(workspace.id, run.id) == []
            assert await uow.tasks.list_run_tasks(workspace.id, run.id) == []
            # Back to the beginning: the next run fetches what this one fetched.
            assert await uow.observations.get_cursor(workspace.id, lineage_key=lineage) is None
        assert await _audited(harness, workspace.id, "discovery_run_deleted")

    async def test_a_run_with_queued_work_is_refused(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        run = await _run(harness, actor, await _scope(harness, actor), execute=False)
        with pytest.raises(StateTransitionError, match="queued or running"):
            await DiscoveryService(harness.context).delete_run(actor, run_id=run.id)

    async def test_a_frozen_run_with_candidates_is_refused(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        workspace = provisioned["workspace"]
        run = await _run(harness, actor, await _scope(harness, actor))
        discovery = DiscoveryService(harness.context)
        _, requests = await discovery.freeze_run(actor, run_id=run.id, force_accept_degraded=True)
        await harness.orchestrator.submit_requests(
            requests, workspace_id=workspace.id, actor_id=actor.user_id, run_id=run.id
        )
        await harness.run_all_tasks()
        with pytest.raises(StateTransitionError, match="frozen"):
            await discovery.delete_run(actor, run_id=run.id)
        async with harness.context.uow_factory() as uow:
            assert await uow.observations.get_run(workspace.id, run.id) is not None

    async def test_an_earlier_run_waits_for_a_later_run_of_the_same_search(
        self, harness, provisioned
    ) -> None:
        """A later run reuses identical pages, so the earlier one's rows may be its."""
        actor = provisioned["steward_actor"]
        scope = await _scope(harness, actor)
        first = await _run(harness, actor, scope)
        harness.clock.advance(60)
        second = await _run(harness, actor, scope)
        discovery = DiscoveryService(harness.context)

        with pytest.raises(StateTransitionError, match="later discovery run"):
            await discovery.delete_run(actor, run_id=first.id)
        await discovery.delete_run(actor, run_id=second.id)
        await discovery.delete_run(actor, run_id=first.id)

    async def test_a_researcher_may_not_delete(self, harness, provisioned) -> None:
        run = await _run(
            harness,
            provisioned["steward_actor"],
            await _scope(harness, provisioned["steward_actor"]),
        )
        with pytest.raises(PolicyDenied, match="run_discovery"):
            await DiscoveryService(harness.context).delete_run(
                provisioned["researcher_actor"], run_id=run.id
            )


class TestExtractionRuns:
    async def _tasks(self, harness, actor, states: list[TaskState]) -> Any:
        from lab_assistant.domain.tasks import Task

        run_id = uuid4()
        async with harness.context.uow_factory() as uow:
            for index, state in enumerate(states):
                await uow.tasks.add(
                    Task(
                        id=uuid4(),
                        workspace_id=actor.workspace_id,
                        actor_id=actor.user_id,
                        stage=StageName.EXTRACTION,
                        state=state,
                        input_fingerprint=f"extraction-{run_id}-{index}",
                        inputs={"batch": index, "template": "yield@1.0.0"},
                        run_id=run_id,
                        created_at=harness.context.now(),
                    )
                )
            await uow.commit()
        return run_id

    async def test_a_run_that_only_failed_is_deleted(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        run_id = await self._tasks(harness, actor, [TaskState.DEAD_LETTER, TaskState.CANCELLED])
        removed = await DatasheetService(harness.context).delete_run(actor, run_id=run_id)
        assert removed == 2
        async with harness.context.uow_factory() as uow:
            assert await uow.tasks.list_run_tasks(actor.workspace_id, run_id) == []

    @pytest.mark.parametrize("kept", [TaskState.SUCCEEDED, TaskState.READY])
    async def test_a_run_with_a_succeeded_or_waiting_batch_is_refused(
        self, harness, provisioned, kept
    ) -> None:
        actor = provisioned["steward_actor"]
        run_id = await self._tasks(harness, actor, [TaskState.FAILED, kept])
        with pytest.raises(StateTransitionError, match="every batch failed"):
            await DatasheetService(harness.context).delete_run(actor, run_id=run_id)

    async def test_an_unknown_run_is_not_found(self, harness, provisioned) -> None:
        with pytest.raises(NotFound):
            await DatasheetService(harness.context).delete_run(
                provisioned["steward_actor"], run_id=uuid4()
            )


def _template() -> dict[str, Any]:
    from lab_assistant.fixtures.archive import _template_payload

    return {**_template_payload(), "template_id": "lipid-yield", "semantic_version": "1.0.0"}


class TestTemplates:
    async def test_an_unused_draft_is_deleted(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        service = DatasheetService(harness.context)
        await service.register_template(actor, _template())
        await service.delete_template(actor, template_id="lipid-yield", version="1.0.0")
        assert await service.list_templates(actor) == []

    async def test_an_approved_version_is_refused(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        service = DatasheetService(harness.context)
        await service.register_template(actor, _template())
        await service.approve_template(actor, template_id="lipid-yield", version="1.0.0")
        with pytest.raises(StateTransitionError, match="only a draft"):
            await service.delete_template(actor, template_id="lipid-yield", version="1.0.0")

    async def test_a_draft_a_run_named_is_refused(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        service = DatasheetService(harness.context)
        await service.register_template(actor, _template())
        await TestExtractionRuns()._tasks(harness, actor, [TaskState.FAILED])
        async with harness.context.uow_factory() as uow:
            for task in await uow.tasks.list_tasks(actor.workspace_id, limit=100):
                task.inputs = {**task.inputs, "template": "lipid-yield@1.0.0"}
                await uow.tasks.update(task)
            await uow.commit()
        with pytest.raises(StateTransitionError, match="used this template"):
            await service.delete_template(actor, template_id="lipid-yield", version="1.0.0")


class TestReleases:
    async def test_a_draft_is_deleted_and_a_published_one_refused(
        self, harness, provisioned
    ) -> None:
        actor = provisioned["steward_actor"]
        service = ReleaseService(harness.context)
        draft, _, _ = await service.draft(
            actor, ReleaseDraftRequest(name="corpus", semantic_version="0.1.0")
        )
        await service.delete_draft(actor, release_id=draft.id)
        async with harness.context.uow_factory() as uow:
            assert await uow.releases.get(actor.workspace_id, draft.id) is None
        assert await _audited(harness, actor.workspace_id, "release_draft_deleted")

        kept, _, _ = await service.draft(
            actor, ReleaseDraftRequest(name="corpus", semantic_version="0.2.0")
        )
        # Published, it is what answers cite: it stays, and is withdrawn instead.
        async with harness.context.uow_factory() as uow:
            await uow.releases.update(
                kept.model_copy(
                    update={
                        "status": ReleaseState.PUBLISHED,
                        "manifest_sha256": kept.manifest_sha256 or "0" * 64,
                        "published_at": harness.context.now(),
                    }
                )
            )
            await uow.commit()
        with pytest.raises(StateTransitionError, match="only one that was never published"):
            await service.delete_draft(actor, release_id=kept.id)

    async def test_a_draft_another_release_builds_on_is_refused(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        service = ReleaseService(harness.context)
        parent, _, _ = await service.draft(
            actor, ReleaseDraftRequest(name="corpus", semantic_version="0.1.0")
        )
        await service.draft(
            actor,
            ReleaseDraftRequest(
                name="corpus", semantic_version="0.2.0", parent_release_id=parent.id
            ),
        )
        with pytest.raises(StateTransitionError, match="built on this one"):
            await service.delete_draft(actor, release_id=parent.id)


BENCHMARK = {
    "benchmark_id": "lab-retrieval",
    "version": "1.0.0",
    "kind": "retrieval",
    "description": "Retrieval set for the deletion tests.",
    "origin": "written for this test",
    "licence": "internal",
    "permitted_use": "testing",
    "sensitivity": "internal",
    "cases": [{"case_id": "lipid-yield", "question": "Lipid yield?", "relevant": ["doi:10.1/x"]}],
    "thresholds": {"recall@5": 0.5},
}


class TestEvaluation:
    async def _run_row(self, harness, actor, status: str) -> Any:
        run_id = uuid4()
        async with harness.context.uow_factory() as uow:
            await uow.evaluations.add_run(
                {
                    "id": run_id,
                    "workspace_id": actor.workspace_id,
                    "benchmark": "lab-retrieval@1.0.0",
                    "benchmark_checksum": "c" * 64,
                    "status": status,
                }
            )
            await uow.commit()
        return run_id

    @pytest.mark.parametrize("status", ["failed", "cancelled"])
    async def test_a_run_with_no_result_is_deleted(self, harness, provisioned, status) -> None:
        actor = provisioned["steward_actor"]
        run_id = await self._run_row(harness, actor, status)
        await EvaluationWorkbench(harness.context).delete_run(actor, run_id=run_id)
        async with harness.context.uow_factory() as uow:
            assert await uow.evaluations.get_run(actor.workspace_id, run_id) is None

    @pytest.mark.parametrize("status", ["succeeded", "queued", "running"])
    async def test_a_run_with_a_result_or_still_going_is_refused(
        self, harness, provisioned, status
    ) -> None:
        actor = provisioned["steward_actor"]
        run_id = await self._run_row(harness, actor, status)
        with pytest.raises(StateTransitionError, match="failed or cancelled"):
            await EvaluationWorkbench(harness.context).delete_run(actor, run_id=run_id)

    async def test_an_unused_benchmark_version_is_deleted(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        await EvaluationService(harness.context).register_benchmark(actor, BENCHMARK)
        await EvaluationWorkbench(harness.context).delete_benchmark(
            actor, benchmark_id="lab-retrieval", version="1.0.0"
        )
        async with harness.context.uow_factory() as uow:
            assert (
                await uow.evaluations.get_benchmark(actor.workspace_id, "lab-retrieval", "1.0.0")
                is None
            )

    async def test_a_benchmark_a_run_used_is_refused(self, harness, provisioned) -> None:
        actor = provisioned["steward_actor"]
        await EvaluationService(harness.context).register_benchmark(actor, BENCHMARK)
        await self._run_row(harness, actor, "failed")
        with pytest.raises(StateTransitionError, match="measured against"):
            await EvaluationWorkbench(harness.context).delete_benchmark(
                actor, benchmark_id="lab-retrieval", version="1.0.0"
            )


class TestInvitations:
    async def test_open_is_refused_revoked_and_expired_are_deleted(
        self, harness, provisioned
    ) -> None:
        actor = provisioned["steward_actor"]
        accounts = harness.accounts
        invitation, _ = await accounts.invite(actor, email="new@lab.invalid", role=Role.RESEARCHER)
        with pytest.raises(StateTransitionError, match="revoke it first"):
            await accounts.delete_invitation(actor, invitation_id=invitation.id)
        await accounts.revoke_invitation(actor, invitation_id=invitation.id)
        await accounts.delete_invitation(actor, invitation_id=invitation.id)

        stale, _ = await accounts.invite(
            actor, email="late@lab.invalid", role=Role.RESEARCHER, ttl_hours=1
        )
        harness.clock.advance(7200)
        await accounts.delete_invitation(actor, invitation_id=stale.id)
        async with harness.context.uow_factory() as uow:
            assert await uow.workspaces.list_invitations(actor.workspace_id) == []
        assert await _audited(harness, actor.workspace_id, "invitation_deleted")
