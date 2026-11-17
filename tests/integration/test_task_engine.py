"""Task engine behaviour: idempotency, crash recovery, caps, dead letters.

These are the properties that let a worker be killed mid-stage without corrupting
counts, and that stop one slow stage starving another.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.stage import (
    StageCommand,
    StageName,
    StageResult,
    StageStatus,
    TaskRequest,
)
from lab_assistant.domain.states import TaskState
from lab_assistant.provenance.fingerprints import task_fingerprint
from lab_assistant.testing import build_harness


class CountingHandler:
    """A handler that records how often it actually ran."""

    def __init__(self, stage: StageName, *, fail_with: ErrorClass | None = None) -> None:
        self.stage_name = stage
        self.implementation_version = "counting/1"
        self.calls = 0
        self._fail_with = fail_with

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        self.calls += 1
        if self._fail_with is not None:
            raise LabAssistantError("configured failure").with_class(self._fail_with)
        return StageResult(stage=self.stage_name, status=StageStatus.SUCCEEDED)


@pytest.fixture
def harness():
    return build_harness()


class TestIdempotency:
    async def test_duplicate_submission_is_one_task(self, harness) -> None:
        handler = CountingHandler(StageName.DISCOVERY)
        harness.handlers.register(handler)
        workspace, actor = uuid4(), uuid4()
        first = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "pubmed"},
        )
        second = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "pubmed"},
        )
        assert first.id == second.id

    async def test_duplicate_delivery_of_a_finished_task_does_not_re_run(self, harness) -> None:
        handler = CountingHandler(StageName.DISCOVERY)
        harness.handlers.register(handler)
        workspace, actor = uuid4(), uuid4()
        task = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "pubmed"},
        )
        await harness.orchestrator.execute(task)
        async with harness.context.uow_factory() as uow:
            stored = await uow.tasks.get(workspace, task.id)
        assert stored is not None and stored.state is TaskState.SUCCEEDED
        # At-least-once delivery: the second delivery returns the stored result.
        await harness.orchestrator.execute(stored)
        assert handler.calls == 1

    async def test_different_inputs_are_different_tasks(self, harness) -> None:
        harness.handlers.register(CountingHandler(StageName.DISCOVERY))
        workspace, actor = uuid4(), uuid4()
        left = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "pubmed"},
        )
        right = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "crossref"},
        )
        assert left.id != right.id


class TestCrashRecovery:
    async def test_an_expired_lease_is_reclaimed_by_another_worker(self, harness) -> None:
        handler = CountingHandler(StageName.PARSING)
        harness.handlers.register(handler)
        workspace = uuid4()
        await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=uuid4(),
            stage=StageName.PARSING,
            inputs={"asset_id": str(uuid4())},
        )
        # Worker A claims the task and then dies without reporting.
        async with harness.context.uow_factory() as uow:
            claimed = await uow.tasks.claim_next(
                owner="worker-a",
                worker_class="compute",
                now=harness.clock(),
                stages=(StageName.PARSING,),
                lease_seconds=60,
                stage_concurrency={StageName.PARSING: 1},
            )
            await uow.commit()
        assert claimed is not None
        # Nothing else can claim it while the lease holds.
        async with harness.context.uow_factory() as uow:
            blocked = await uow.tasks.claim_next(
                owner="worker-b",
                worker_class="compute",
                now=harness.clock(),
                stages=(StageName.PARSING,),
                stage_concurrency={StageName.PARSING: 1},
            )
        assert blocked is None
        # After the lease expires, worker B picks it up.
        later = harness.clock() + timedelta(seconds=120)
        async with harness.context.uow_factory() as uow:
            reclaimed = await uow.tasks.claim_next(
                owner="worker-b",
                worker_class="compute",
                now=later,
                stages=(StageName.PARSING,),
                stage_concurrency={StageName.PARSING: 1},
            )
            await uow.commit()
        assert reclaimed is not None
        assert reclaimed.lease_owner == "worker-b"
        assert reclaimed.attempt == 2

    async def test_a_completed_task_frees_its_stage_slot(self, harness) -> None:
        """The lease counter must not leak, or the stage silently stops working."""
        harness.handlers.register(CountingHandler(StageName.INDEXING))
        workspace = uuid4()
        for index in range(3):
            await harness.orchestrator.submit(
                workspace_id=workspace,
                actor_id=uuid4(),
                stage=StageName.INDEXING,
                inputs={"release_id": str(index)},
            )
        executed = 0
        for _ in range(5):
            result = await harness.orchestrator.run_once(owner="w", worker_class="compute")
            if result.idle:
                break
            executed += result.executed
        assert executed == 3


class TestFailureHandling:
    async def test_retryable_failure_is_rescheduled(self, harness) -> None:
        harness.handlers.register(
            CountingHandler(StageName.DISCOVERY, fail_with=ErrorClass.RATE_LIMITED)
        )
        workspace = uuid4()
        task = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=uuid4(),
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "pubmed"},
        )
        await harness.orchestrator.execute(task)
        async with harness.context.uow_factory() as uow:
            stored = await uow.tasks.get(workspace, task.id)
        assert stored is not None
        assert stored.state is TaskState.READY
        assert stored.not_before is not None

    async def test_human_action_leaves_no_lease_and_is_not_a_failure(self, harness) -> None:
        harness.handlers.register(
            CountingHandler(StageName.ACQUISITION, fail_with=ErrorClass.ENTITLEMENT_REQUIRED)
        )
        workspace = uuid4()
        task = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=uuid4(),
            stage=StageName.ACQUISITION,
            inputs={"plan_id": str(uuid4())},
        )
        await harness.orchestrator.execute(task)
        async with harness.context.uow_factory() as uow:
            stored = await uow.tasks.get(workspace, task.id)
        assert stored is not None
        assert stored.state is TaskState.AWAITING_USER
        assert stored.lease_owner is None
        assert not stored.state.counts_as_failure

    async def test_dead_letter_can_be_retried_by_an_operator(self, harness) -> None:
        harness.handlers.register(
            CountingHandler(StageName.DISCOVERY, fail_with=ErrorClass.SOURCE_UNAVAILABLE)
        )
        workspace = uuid4()
        task = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=uuid4(),
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "pubmed"},
        )
        for _ in range(task.max_attempts + 1):
            async with harness.context.uow_factory() as uow:
                stored = await uow.tasks.get(workspace, task.id)
                assert stored is not None
                stored.state = TaskState.READY
                stored.not_before = None
                await uow.tasks.update(stored)
                await uow.commit()
            await harness.orchestrator.execute(stored)
        async with harness.context.uow_factory() as uow:
            stored = await uow.tasks.get(workspace, task.id)
        assert stored is not None and stored.state is TaskState.DEAD_LETTER
        revived = await harness.orchestrator.retry_dead_letter(
            workspace_id=workspace, task_id=task.id
        )
        assert revived.state is TaskState.READY
        assert revived.attempt == 0

    async def test_dead_letter_is_audited(self, harness) -> None:
        harness.handlers.register(
            CountingHandler(StageName.DISCOVERY, fail_with=ErrorClass.INTERNAL)
        )
        workspace = uuid4()
        task = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=uuid4(),
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "pubmed"},
        )
        for _ in range(task.max_attempts + 1):
            async with harness.context.uow_factory() as uow:
                stored = await uow.tasks.get(workspace, task.id)
                assert stored is not None
                stored.state = TaskState.READY
                stored.not_before = None
                await uow.tasks.update(stored)
                await uow.commit()
            await harness.orchestrator.execute(stored)
        async with harness.context.uow_factory() as uow:
            events = await uow.audit.list_events(workspace)
        assert any(event.action.value == "task_dead_lettered" for event in events)


class TestConcurrencyCaps:
    async def test_stage_cap_is_enforced_in_the_store_not_by_worker_count(self, harness) -> None:
        """A long call in one stage must not starve another queue."""
        harness.handlers.register(CountingHandler(StageName.EXTRACTION))
        workspace = uuid4()
        for index in range(3):
            await harness.orchestrator.submit(
                workspace_id=workspace,
                actor_id=uuid4(),
                stage=StageName.EXTRACTION,
                inputs={"rendition_id": str(index)},
            )
        now = harness.clock()
        claimed = []
        for owner in ("a", "b", "c"):
            async with harness.context.uow_factory() as uow:
                task = await uow.tasks.claim_next(
                    owner=owner,
                    worker_class="network",
                    now=now,
                    stages=(StageName.EXTRACTION,),
                    stage_concurrency={StageName.EXTRACTION: 2},
                )
                await uow.commit()
            if task is not None:
                claimed.append(task)
        assert len(claimed) == 2


class TestDependenciesAndCancellation:
    async def test_dependent_task_waits_then_unblocks(self, harness) -> None:
        harness.handlers.register(CountingHandler(StageName.PARSING))
        harness.handlers.register(CountingHandler(StageName.EXTRACTION))
        workspace, actor = uuid4(), uuid4()
        first = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.PARSING,
            inputs={"asset_id": "a"},
        )
        second = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.EXTRACTION,
            inputs={"rendition_id": "r"},
            depends_on=(first.id,),
        )
        assert second.state is TaskState.BLOCKED
        await harness.orchestrator.execute(first)
        async with harness.context.uow_factory() as uow:
            unblocked = await uow.tasks.get(workspace, second.id)
        assert unblocked is not None and unblocked.state is TaskState.READY

    async def test_cancelling_a_run_stops_scheduling_but_keeps_finished_output(
        self, harness
    ) -> None:
        harness.handlers.register(CountingHandler(StageName.DISCOVERY))
        workspace, actor, run = uuid4(), uuid4(), uuid4()
        done = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "pubmed"},
            run_id=run,
        )
        pending = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=actor,
            stage=StageName.DISCOVERY,
            inputs={"connector_id": "crossref"},
            run_id=run,
        )
        await harness.orchestrator.execute(done)
        cancelled = await harness.orchestrator.cancel_run(workspace_id=workspace, run_id=run)
        assert cancelled == 1
        async with harness.context.uow_factory() as uow:
            finished = await uow.tasks.get(workspace, done.id)
            stopped = await uow.tasks.get(workspace, pending.id)
        assert finished is not None and finished.state is TaskState.SUCCEEDED
        assert stopped is not None and stopped.state is TaskState.CANCELLED


class TestQueueSnapshot:
    async def test_snapshot_reads_durable_state(self, harness) -> None:
        harness.handlers.register(
            CountingHandler(StageName.ACQUISITION, fail_with=ErrorClass.ENTITLEMENT_REQUIRED)
        )
        workspace = uuid4()
        task = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=uuid4(),
            stage=StageName.ACQUISITION,
            inputs={"plan_id": str(uuid4())},
        )
        await harness.orchestrator.execute(task)
        snapshot = await harness.orchestrator.queue_snapshot(workspace_id=workspace)
        assert snapshot["counts"]["awaiting_user"] == 1
        assert snapshot["awaiting_user"][0]["stage"] == "acquisition"

    async def test_follow_on_tasks_are_submitted_from_results(self, harness) -> None:
        class ChainingHandler(CountingHandler):
            async def execute(self, command: StageCommand) -> StageResult:
                self.calls += 1
                return StageResult(
                    stage=self.stage_name,
                    status=StageStatus.SUCCEEDED,
                    next_tasks=(TaskRequest(stage=StageName.PARSING, inputs={"asset_id": "a"}),),
                )

        harness.handlers.register(ChainingHandler(StageName.ACQUISITION))
        harness.handlers.register(CountingHandler(StageName.PARSING))
        workspace = uuid4()
        task = await harness.orchestrator.submit(
            workspace_id=workspace,
            actor_id=uuid4(),
            stage=StageName.ACQUISITION,
            inputs={"plan_id": "p"},
        )
        await harness.orchestrator.execute(task)
        async with harness.context.uow_factory() as uow:
            tasks = await uow.tasks.list_tasks(workspace)
        assert {t.stage for t in tasks} == {StageName.ACQUISITION, StageName.PARSING}
