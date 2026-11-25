"""Deleting a whole scope: what goes, what stays, and what stops it.

Over memory, SQLite and (with ``LAB_TEST_DATABASE_URL``) Postgres, through the
bulk-review fixture: the deletion is set-based SQL in one adapter and dictionary
filtering in the other, and both must remove exactly the same rows.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from test_bulk_review import lab  # noqa: F401  (the fixture)
from test_scope_rules import ScopeLab

from lab_assistant.application.reconciliation import IdentityService
from lab_assistant.application.scope_deletion import ScopeDeletionHandler, ScopeDeletionService
from lab_assistant.contracts.errors import StateTransitionError, ValidationFailure
from lab_assistant.contracts.release import ReleaseItemRecord
from lab_assistant.contracts.stage import StageCommand, StageName, StageStatus
from lab_assistant.domain.observation import (
    ConnectorRunState,
    DiscoveryRun,
    DiscoveryRunStatus,
    Predicate,
    SourceAssertion,
    SourceObservation,
    SourceRequest,
)
from lab_assistant.domain.release import CorpusRelease, ReleaseItem
from lab_assistant.domain.scope import ScopeVersion
from lab_assistant.domain.states import ReleaseState, TaskState
from lab_assistant.domain.tasks import Task


class DeletionLab(ScopeLab):
    async def run(self, scope: ScopeVersion, *, query: str = "q-yarrowia") -> DiscoveryRun:
        context = self.harness.context
        run = DiscoveryRun(
            id=context.new_id(),
            workspace_id=self.ws,
            scope_version_id=scope.id,
            status=DiscoveryRunStatus.FROZEN,
            run_fingerprint=str(context.new_id()),
            requested_by=self.actor.user_id,
            started_at=context.now(),
            frozen_at=context.now(),
            connector_states=(
                ConnectorRunState(
                    connector_id="pubmed",
                    connector_version="1.0.0",
                    required=True,
                    query_fingerprint=query,
                ),
            ),
        )
        async with context.uow_factory() as uow:
            await uow.observations.add_run(run)
            await uow.commit()
        return run

    async def found(self, run: DiscoveryRun, work_id: UUID, title: str = "A title") -> None:
        """A page of ``run`` with one record, which reconciliation linked to ``work_id``."""
        context = self.harness.context
        request_id, observation_id = context.new_id(), context.new_id()
        now = context.now()
        async with context.uow_factory() as uow:
            await uow.observations.add_request(
                SourceRequest(
                    id=request_id,
                    workspace_id=self.ws,
                    discovery_run_id=run.id,
                    connector_id="pubmed",
                    connector_version="1.0.0",
                    request_fingerprint=str(request_id),
                    method="GET",
                    canonical_url_redacted="https://example.invalid/q",
                    requested_at=now,
                )
            )
            await uow.observations.add_observation(
                SourceObservation(
                    id=observation_id,
                    workspace_id=self.ws,
                    source_request_id=request_id,
                    source_record_locator=f"doi:10.1/{request_id}",
                    parser_id="p",
                    parser_version="1",
                    schema_version="1",
                    observed_at=now,
                )
            )
            await uow.observations.add_assertions(
                [
                    SourceAssertion(
                        id=context.new_id(),
                        workspace_id=self.ws,
                        source_observation_id=observation_id,
                        subject_hint=f"doi:10.1/{request_id}",
                        predicate=Predicate.TITLE,
                        value_json=title,
                        work_id=work_id,
                    )
                ]
            )
            await uow.commit()

    async def release(self, work_id: UUID, status: ReleaseState) -> CorpusRelease:
        context = self.harness.context
        release = CorpusRelease(
            id=context.new_id(),
            workspace_id=self.ws,
            name="Corpus",
            semantic_version="1.0.0",
            status=status,
            approved_by=None if status is ReleaseState.DRAFT else self.actor.user_id,
            created_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.releases.add(release)
            await uow.releases.add_items(
                [
                    ReleaseItem(
                        id=context.new_id(),
                        workspace_id=self.ws,
                        release_id=release.id,
                        record=ReleaseItemRecord(work_id=work_id),
                    )
                ]
            )
            await uow.commit()
        return release

    async def task(self, run: DiscoveryRun, state: TaskState) -> UUID:
        context = self.harness.context
        task = Task(
            id=context.new_id(),
            workspace_id=self.ws,
            stage=StageName.RECONCILIATION,
            state=state,
            input_fingerprint=str(context.new_id()),
            inputs={"discovery_run_id": str(run.id)},
            run_id=run.id,
            created_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.tasks.add(task)
            await uow.commit()
        return task.id

    async def exists(self, work_id: UUID) -> bool:
        async with self.harness.context.uow_factory() as uow:
            return await uow.works.get(self.ws, work_id) is not None

    @property
    def deletion(self) -> ScopeDeletionService:
        return ScopeDeletionService(self.harness.context, self.harness.orchestrator)


@pytest.fixture
def deleting(lab) -> DeletionLab:  # noqa: F811
    return DeletionLab(lab.harness, lab.actor)


class TestWhatGoes:
    async def test_every_version_run_and_paper_only_it_found_goes(
        self, deleting: DeletionLab
    ) -> None:
        v1 = await deleting.scope()
        v2 = await deleting.scope(version=2, scope_id=v1.scope_id)
        other = await deleting.scope()
        run1, run2 = await deleting.run(v1), await deleting.run(v2)
        other_run = await deleting.run(other, query="q-other")

        mine = await deleting.screened("Yarrowia lipid factories")
        merged = await deleting.screened("Yarrowia lipid factories, a duplicate")
        shared = await deleting.screened("Lipid yeasts")
        theirs = await deleting.screened("Something else")
        await deleting.found(run1, mine)
        await deleting.found(run2, merged)
        await deleting.found(run1, shared)
        await deleting.found(other_run, shared)
        await deleting.found(other_run, theirs)
        await deleting.pair(mine, merged)
        await IdentityService(deleting.harness.context).merge(
            deleting.actor, keep_work_id=mine, merge_work_id=merged, reason_code="same_paper"
        )
        settled = await deleting.task(run1, TaskState.SUCCEEDED)

        preview = await deleting.deletion.plan(deleting.actor, v2.id)
        assert preview.view()["deletable"] is True
        counts = preview.counts()
        assert (counts["versions"], counts["runs"], counts["papers"], counts["papers_kept"]) == (
            2,
            2,
            2,
            1,
        )
        assert await deleting.exists(mine)  # the preview changed nothing

        with pytest.raises(ValidationFailure):
            await deleting.deletion.delete(deleting.actor, v1.id, confirm_name="Yarrow")
        deleted = await deleting.deletion.delete(deleting.actor, v1.id, confirm_name="Yarrowia")
        assert deleted["papers"] == 2 and deleted["tasks"] == 1

        assert not await deleting.exists(mine)
        assert not await deleting.exists(merged)
        assert await deleting.exists(shared)
        assert await deleting.exists(theirs)
        async with deleting.harness.context.uow_factory() as uow:
            versions = {v.id for v in await uow.configs.list_scope_versions(deleting.ws)}
            runs = {r.id for r in await uow.observations.list_runs(deleting.ws)}
            kept = await uow.observations.list_assertions(deleting.ws, work_id=shared)
            pairs = await uow.works.list_duplicate_candidates(deleting.ws, work_id=mine)
            decisions = await uow.works.list_decisions(deleting.ws)
            tasks = {t.id for t in await uow.tasks.list_tasks(deleting.ws, limit=100)}
            summary = (await uow.works.get_summaries(deleting.ws, [shared]))[shared]
            events = await uow.audit.list_events(deleting.ws, action="scope_deleted")
        assert versions == {other.id}
        assert runs == {other_run.id}
        assert len(kept) == 1  # only the other scope's record of it
        assert pairs == []
        assert not any(mine in d.subject_work_ids for d in decisions)
        assert settled not in tasks
        assert summary.relevance_text is not None
        assert len(events) == 1 and events[0].metadata["counts"]["papers"] == 2

    async def test_a_draft_release_holding_its_papers_goes_with_it(
        self, deleting: DeletionLab
    ) -> None:
        scope = await deleting.scope()
        run = await deleting.run(scope)
        paper = await deleting.screened("Yarrowia lipid")
        await deleting.found(run, paper)
        draft = await deleting.release(paper, ReleaseState.DRAFT)

        await deleting.deletion.delete(deleting.actor, scope.id, confirm_name="Yarrowia")
        async with deleting.harness.context.uow_factory() as uow:
            assert await uow.releases.get(deleting.ws, draft.id) is None


class TestWhatStopsIt:
    async def test_an_approved_release_holding_its_papers(self, deleting: DeletionLab) -> None:
        scope = await deleting.scope()
        run = await deleting.run(scope)
        paper = await deleting.screened("Yarrowia lipid")
        await deleting.found(run, paper)
        await deleting.release(paper, ReleaseState.APPROVED)

        view = (await deleting.deletion.plan(deleting.actor, scope.id)).view()
        assert view["deletable"] is False
        assert "is approved and holds its papers" in view["blockers"][0]
        with pytest.raises(StateTransitionError):
            await deleting.deletion.delete(deleting.actor, scope.id, confirm_name="Yarrowia")
        assert await deleting.exists(paper)

    async def test_work_still_queued_for_it(self, deleting: DeletionLab) -> None:
        scope = await deleting.scope()
        run = await deleting.run(scope)
        await deleting.task(run, TaskState.PENDING)

        view = (await deleting.deletion.plan(deleting.actor, scope.id)).view()
        assert any("still queued or running" in blocker for blocker in view["blockers"])

    async def test_another_scope_searching_with_the_same_query(self, deleting: DeletionLab) -> None:
        scope, other = await deleting.scope(), await deleting.scope()
        await deleting.run(scope, query="same")
        await deleting.run(other, query="same")

        view = (await deleting.deletion.plan(deleting.actor, scope.id)).view()
        assert any("same query" in blocker for blocker in view["blockers"])


class TestDeletingAReleaseNeverPublished:
    async def test_an_approved_release_goes_with_its_finished_indexing(
        self, deleting: DeletionLab
    ) -> None:
        from lab_assistant.application.releases import ReleaseService

        context = deleting.harness.context
        scope = await deleting.scope()
        run = await deleting.run(scope)
        paper = await deleting.screened("Yarrowia lipid")
        await deleting.found(run, paper)
        approved = await deleting.release(paper, ReleaseState.APPROVED)
        indexing = Task(
            id=context.new_id(),
            workspace_id=deleting.ws,
            stage=StageName.INDEXING,
            state=TaskState.SUCCEEDED,
            input_fingerprint=str(context.new_id()),
            inputs={"release_id": str(approved.id)},
            created_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.tasks.add(indexing)
            await uow.commit()

        await ReleaseService(context).delete_draft(deleting.actor, release_id=approved.id)
        async with context.uow_factory() as uow:
            assert await uow.releases.get(deleting.ws, approved.id) is None
            assert await uow.tasks.get(deleting.ws, indexing.id) is None

    async def test_a_published_one_stays(self, deleting: DeletionLab) -> None:
        from lab_assistant.application.releases import ReleaseService

        context = deleting.harness.context
        paper = await deleting.screened("Yarrowia lipid")
        published = await deleting.release(paper, ReleaseState.APPROVED)
        async with context.uow_factory() as uow:
            stored = await uow.releases.get(deleting.ws, published.id)
            stored.status = ReleaseState.PUBLISHED
            stored.manifest_sha256 = "0" * 64
            stored.published_at = context.now()
            await uow.releases.update(stored)
            await uow.commit()
        with pytest.raises(StateTransitionError, match="withdraw a published one"):
            await ReleaseService(context).delete_draft(deleting.actor, release_id=published.id)

    async def test_an_unfinished_task_for_it_stops_it(self, deleting: DeletionLab) -> None:
        from lab_assistant.application.releases import ReleaseService

        context = deleting.harness.context
        paper = await deleting.screened("Yarrowia lipid")
        approved = await deleting.release(paper, ReleaseState.APPROVED)
        async with context.uow_factory() as uow:
            await uow.tasks.add(
                Task(
                    id=context.new_id(),
                    workspace_id=deleting.ws,
                    stage=StageName.INDEXING,
                    state=TaskState.LEASED,
                    input_fingerprint=str(context.new_id()),
                    inputs={"release_id": str(approved.id)},
                    created_at=context.now(),
                )
            )
            await uow.commit()
        with pytest.raises(StateTransitionError, match="still queued or running"):
            await ReleaseService(context).delete_draft(deleting.actor, release_id=approved.id)


class TestTheDeletionRunsInTheQueue:
    """Deleting reads the workspace twice over and purges in one transaction.

    On the internal corpus that is minutes, and a destructive press that the
    gateway cuts off leaves a person unable to tell whether it happened. The
    press confirms the name and queues; the handler does the rest.
    """

    async def test_the_press_confirms_the_name_and_queues_nothing_else(
        self, deleting: DeletionLab
    ) -> None:
        scope = await deleting.scope()
        run = await deleting.run(scope)
        paper = await deleting.screened("Yarrowia lipid factories")
        await deleting.found(run, paper)

        with pytest.raises(ValidationFailure):
            await deleting.deletion.start(deleting.actor, scope.id, confirm_name="Yarrow")

        task = await deleting.deletion.start(deleting.actor, scope.id, confirm_name="Yarrowia")

        assert task.stage is StageName.SCOPE_DELETION
        assert task.state is TaskState.READY
        # Queued, not done: nothing has gone yet.
        assert await deleting.exists(paper)

    async def test_a_second_press_joins_the_deletion_already_queued(
        self, deleting: DeletionLab
    ) -> None:
        """Two purges of one scope at once would fight over the same rows."""
        scope = await deleting.scope()
        task = await deleting.deletion.start(deleting.actor, scope.id, confirm_name="Yarrowia")

        again = await deleting.deletion.start(deleting.actor, scope.id, confirm_name="Yarrowia")

        assert again.id == task.id

    async def test_the_handler_purges_and_reports_the_counts(self, deleting: DeletionLab) -> None:
        scope = await deleting.scope()
        run = await deleting.run(scope)
        paper = await deleting.screened("Yarrowia lipid factories")
        await deleting.found(run, paper)
        task = await deleting.deletion.start(deleting.actor, scope.id, confirm_name="Yarrowia")

        result = await ScopeDeletionHandler(deleting.harness.context).execute(
            StageCommand(
                stage=StageName.SCOPE_DELETION,
                workspace_id=deleting.ws,
                actor_id=deleting.actor.user_id,
                inputs=dict(task.inputs),
            )
        )

        assert result.status is StageStatus.SUCCEEDED
        assert result.metrics["papers"] == 1
        assert result.metrics["versions"] == 1
        assert not await deleting.exists(paper)

    async def test_a_blocker_that_appeared_after_the_press_stops_the_handler(
        self, deleting: DeletionLab
    ) -> None:
        """The plan is read inside the purge's own transaction, not at the press.

        Between queuing and running, work can be queued for the scope's papers.
        A plan read at the press would be a promise about rows nobody holds.
        """
        scope = await deleting.scope()
        run = await deleting.run(scope)
        await deleting.found(run, await deleting.screened("Yarrowia lipid factories"))
        task = await deleting.deletion.start(deleting.actor, scope.id, confirm_name="Yarrowia")
        await deleting.task(run, TaskState.READY)

        with pytest.raises(StateTransitionError):
            await ScopeDeletionHandler(deleting.harness.context).execute(
                StageCommand(
                    stage=StageName.SCOPE_DELETION,
                    workspace_id=deleting.ws,
                    actor_id=deleting.actor.user_id,
                    inputs=dict(task.inputs),
                )
            )
