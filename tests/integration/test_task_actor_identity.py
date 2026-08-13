"""A worker's audit rows name the person who asked.

The audit found ``actor_id`` accepted by ``submit()`` and then discarded: execution
rebuilt it from ``inputs`` and otherwise fell back to the workspace uuid. Every
worker-side audit row for a steward's decision could therefore say the workspace did
it — wrong, and wrong in a way that looks fine until an incident.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from lab_assistant.application.tasks import HandlerRegistry, TaskOrchestrator
from lab_assistant.contracts.stage import StageCommand, StageName, StageResult, StageStatus
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor


class _RecordingHandler:
    """Captures the command it was given, so the actor can be asserted."""

    stage_name = StageName.INDEXING
    implementation_version = "test-1"

    def __init__(self) -> None:
        self.commands: list[StageCommand] = []

    def fingerprint(self, command: StageCommand) -> str:
        from lab_assistant.provenance.fingerprints import task_fingerprint

        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        self.commands.append(command)
        return StageResult(stage=self.stage_name, status=StageStatus.SUCCEEDED)


@pytest.fixture
async def wired() -> dict[str, Any]:
    harness = build_harness()
    user = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
    )
    handler = _RecordingHandler()
    orchestrator = TaskOrchestrator(harness.context, HandlerRegistry({StageName.INDEXING: handler}))
    return {
        "harness": harness,
        "user": user,
        "workspace": workspace,
        "handler": handler,
        "orchestrator": orchestrator,
        "actor": make_actor(user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN),
    }


class TestTaskActorIdentity:
    async def test_the_actor_is_persisted_and_handed_to_the_handler(self, wired) -> None:
        task = await wired["orchestrator"].submit(
            workspace_id=wired["workspace"].id,
            actor_id=wired["user"].id,
            stage=StageName.INDEXING,
            # Deliberately *not* in inputs: that was the smuggling route.
            inputs={"release_id": str(uuid4())},
        )
        assert task.actor_id == wired["user"].id

        async with wired["harness"].context.uow_factory() as uow:
            stored = await uow.tasks.get(wired["workspace"].id, task.id)
        assert stored is not None and stored.actor_id == wired["user"].id

        await wired["orchestrator"].execute(stored)
        command = wired["handler"].commands[-1]
        assert command.actor_id == wired["user"].id
        assert command.actor_id != wired["workspace"].id

    async def test_a_task_with_no_actor_at_all_fails_loudly(self, wired) -> None:
        """No fallback to the workspace: a wrong audit row is worse than an error."""
        from lab_assistant.contracts.errors import LabAssistantError
        from lab_assistant.domain.tasks import Task

        orphan = Task(
            id=wired["harness"].context.new_id(),
            workspace_id=wired["workspace"].id,
            actor_id=None,
            stage=StageName.INDEXING,
            input_fingerprint="orphan-task",
            inputs={},
        )
        async with wired["harness"].context.uow_factory() as uow:
            await uow.tasks.add(orphan)
            await uow.commit()
        with pytest.raises(LabAssistantError, match="has no actor"):
            await wired["orchestrator"].execute(orphan)

    async def test_a_legacy_row_with_the_actor_in_inputs_still_runs(self, wired) -> None:
        """Rows written before the column existed keep working, read from inputs."""
        from lab_assistant.domain.tasks import Task

        legacy = Task(
            id=wired["harness"].context.new_id(),
            workspace_id=wired["workspace"].id,
            actor_id=None,
            stage=StageName.INDEXING,
            input_fingerprint="legacy-task",
            inputs={"actor_id": str(wired["user"].id)},
        )
        async with wired["harness"].context.uow_factory() as uow:
            await uow.tasks.add(legacy)
            await uow.commit()
        await wired["orchestrator"].execute(legacy)
        assert wired["handler"].commands[-1].actor_id == wired["user"].id

    async def test_two_stewards_asking_for_the_same_work_share_one_task(self, wired) -> None:
        """The fingerprint excludes the actor, on purpose.

        Otherwise the same discovery run would be queued once per person who asked,
        and the source would see double the request volume.
        """
        second = await wired["harness"].accounts.create_user(
            email="second@lab.invalid", password="a-long-passphrase"
        )
        inputs = {"release_id": "8f14e45f-ceea-467a-9c0e-6bd9bd6e0f1f"}
        first_task = await wired["orchestrator"].submit(
            workspace_id=wired["workspace"].id,
            actor_id=wired["user"].id,
            stage=StageName.INDEXING,
            inputs=inputs,
        )
        same_task = await wired["orchestrator"].submit(
            workspace_id=wired["workspace"].id,
            actor_id=second.id,
            stage=StageName.INDEXING,
            inputs=inputs,
        )
        assert same_task.id == first_task.id
        # The first requester stays recorded; the audit trail is not rewritten by a
        # duplicate delivery.
        assert same_task.actor_id == wired["user"].id

    async def test_follow_on_tasks_inherit_the_submitting_actor(self, wired) -> None:
        from lab_assistant.contracts.stage import TaskRequest

        class _Chaining(_RecordingHandler):
            async def execute(self, command: StageCommand) -> StageResult:
                self.commands.append(command)
                return StageResult(
                    stage=self.stage_name,
                    status=StageStatus.SUCCEEDED,
                    next_tasks=(TaskRequest(stage=StageName.INDEXING, inputs={"step": "second"}),),
                )

        handler = _Chaining()
        orchestrator = TaskOrchestrator(
            wired["harness"].context, HandlerRegistry({StageName.INDEXING: handler})
        )
        task = await orchestrator.submit(
            workspace_id=wired["workspace"].id,
            actor_id=wired["user"].id,
            stage=StageName.INDEXING,
            inputs={"step": "first"},
        )
        await orchestrator.execute(task)
        async with wired["harness"].context.uow_factory() as uow:
            tasks = await uow.tasks.list_tasks(wired["workspace"].id)
        assert len(tasks) == 2
        assert {t.actor_id for t in tasks} == {wired["user"].id}
