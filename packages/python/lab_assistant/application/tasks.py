"""The durable task orchestrator.

Submission is idempotent by fingerprint: a duplicate submission returns the
existing task. Execution is idempotent by handler contract: a duplicate delivery
of a completed task returns its stored result without re-running side effects.

Concurrency is capped **per stage**, in the database, not by how many worker
containers exist. That is the mechanism that stops a long provider call in one
stage starving another queue, and it works with two deployments rather than six.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ServiceContext
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import (
    StageCommand,
    StageHandler,
    StageName,
    StageResult,
    StageStatus,
    TaskRequest,
)
from lab_assistant.domain.states import TaskState
from lab_assistant.domain.tasks import Task
from lab_assistant.provenance.fingerprints import task_fingerprint

__all__ = ["HandlerRegistry", "TaskOrchestrator", "WorkerLoopResult"]


def _legacy_actor_id(task: Task) -> UUID:
    """Recover the actor for a row written before ``tasks.actor_id`` existed.

    Older submissions smuggled the actor through ``inputs``. That is read here for
    compatibility and nowhere else. When neither source has an actor the task is
    refused rather than attributed to the workspace: a worker audit row saying "the
    workspace did this" is worse than a visible failure, because it is wrong and
    looks fine.
    """
    smuggled = task.inputs.get("actor_id")
    if smuggled:
        return UUID(str(smuggled))
    raise LabAssistantError(
        f"task {task.id} has no actor; re-submit it so its audit trail names a person",
        context={"task_id": str(task.id), "stage": task.stage.value},
    ).with_class(ErrorClass.INTERNAL)


class HandlerRegistry:
    """Stage name -> handler. A missing handler raises; it never no-ops."""

    def __init__(self, handlers: dict[StageName, StageHandler] | None = None) -> None:
        self._handlers: dict[StageName, StageHandler] = dict(handlers or {})

    def register(self, handler: StageHandler) -> None:
        self._handlers[handler.stage_name] = handler

    def get(self, stage: StageName) -> StageHandler:
        try:
            return self._handlers[stage]
        except KeyError as exc:
            raise LookupError(f"no handler registered for stage {stage.value}") from exc

    def stages(self) -> tuple[StageName, ...]:
        return tuple(sorted(self._handlers, key=lambda stage: stage.value))

    def stages_for(self, worker_class: str) -> tuple[StageName, ...]:
        return tuple(stage for stage in self.stages() if stage.worker_class == worker_class)


@dataclass(frozen=True, slots=True)
class WorkerLoopResult:
    executed: int
    succeeded: int
    failed: int
    awaiting_user: int
    idle: bool


class TaskOrchestrator:
    def __init__(self, context: ServiceContext, registry: HandlerRegistry) -> None:
        self._context = context
        self._registry = registry

    async def submit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        stage: StageName,
        inputs: dict[str, Any],
        run_id: UUID | None = None,
        depends_on: tuple[UUID, ...] = (),
        not_before: datetime | None = None,
        idempotency_key: str | None = None,
        policy_version_id: UUID | None = None,
    ) -> Task:
        context = self._context
        handler = self._registry.get(stage)
        command = StageCommand(
            stage=stage,
            workspace_id=workspace_id,
            actor_id=actor_id,
            inputs=inputs,
            run_id=run_id,
            idempotency_key=idempotency_key,
            policy_version_id=policy_version_id,
        )
        fingerprint = task_fingerprint(
            command, implementation_version=handler.implementation_version
        )
        async with context.uow_factory() as uow:
            existing = await uow.tasks.find_by_fingerprint(workspace_id, fingerprint)
            if existing is not None:
                return existing
            task = Task(
                id=context.new_id(),
                workspace_id=workspace_id,
                actor_id=actor_id,
                stage=stage,
                state=TaskState.BLOCKED if depends_on else TaskState.READY,
                input_fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                inputs=inputs,
                run_id=run_id,
                depends_on=depends_on,
                not_before=not_before,
                created_at=context.now(),
            )
            await uow.tasks.add(task)
            await uow.commit()
        return task

    async def submit_requests(
        self,
        requests: tuple[TaskRequest, ...],
        *,
        workspace_id: UUID,
        actor_id: UUID,
        run_id: UUID | None = None,
    ) -> list[Task]:
        return [
            await self.submit(
                workspace_id=workspace_id,
                actor_id=actor_id,
                stage=request.stage,
                inputs=dict(request.inputs),
                run_id=run_id,
                depends_on=request.depends_on,
                not_before=request.not_before,
                idempotency_key=request.idempotency_key,
            )
            for request in requests
        ]

    async def run_once(
        self, *, owner: str, worker_class: str, lease_seconds: int | None = None
    ) -> WorkerLoopResult:
        """Claim and execute at most one task. The worker loop calls this."""
        context = self._context
        lease = lease_seconds or context.settings.worker_lease_seconds
        stage_caps = {
            stage: context.settings.stage_concurrency.get(stage.value, 1)
            for stage in self._registry.stages_for(worker_class)
        }
        async with context.uow_factory() as uow:
            task = await uow.tasks.claim_next(
                owner=owner,
                worker_class=worker_class,
                now=context.now(),
                stages=self._registry.stages_for(worker_class),
                lease_seconds=lease,
                stage_concurrency=stage_caps,
            )
            await uow.commit()
        if task is None:
            return WorkerLoopResult(0, 0, 0, 0, idle=True)

        result = await self.execute(task, owner=owner)
        return WorkerLoopResult(
            executed=1,
            succeeded=1 if result.status.is_success else 0,
            failed=1 if result.status in (StageStatus.FAILED, StageStatus.QUARANTINED) else 0,
            awaiting_user=1 if result.status is StageStatus.AWAITING_USER else 0,
            idle=False,
        )

    async def execute(self, task: Task, *, owner: str = "inline") -> StageResult:
        """Execute one task, recording its outcome durably.

        A task that is not already leased is claimed first, so retry accounting is
        the same whether the caller came through ``run_once`` or executed inline.
        Without this, an inline execution would fail forever at attempt zero and
        never reach the dead-letter queue.
        """
        context = self._context
        handler = self._registry.get(task.stage)
        if task.state is not TaskState.LEASED and task.state is not TaskState.SUCCEEDED:
            task.claim(
                owner=owner,
                now=context.now(),
                lease_seconds=context.settings.worker_lease_seconds,
            )
            async with context.uow_factory() as uow:
                await uow.tasks.update(task)
                await uow.commit()
        command = StageCommand(
            stage=task.stage,
            workspace_id=task.workspace_id,
            # The stored actor, not a guess. There is deliberately no fallback to
            # `workspace_id`: attributing a person's action to the workspace makes
            # the audit trail wrong in a way nobody notices until an incident.
            actor_id=task.actor_id or _legacy_actor_id(task),
            inputs=task.inputs,
            run_id=task.run_id,
            idempotency_key=task.idempotency_key,
        )
        # A completed task's stored result is returned rather than recomputed.
        if task.state is TaskState.SUCCEEDED and task.result:
            return StageResult.model_validate(task.result)

        try:
            result = await handler.execute(command)
        except LabAssistantError as exc:
            await self._record_failure(task, error_class=exc.error_class, detail=exc.detail)
            return StageResult(
                stage=task.stage,
                status=StageStatus.FAILED,
                error_class=exc.error_class,
                error_detail=exc.detail,
            )
        except Exception as exc:  # unexpected: dead-letter after retries
            await self._record_failure(
                task, error_class=ErrorClass.INTERNAL, detail=f"{type(exc).__name__}: {exc}"
            )
            raise

        async with context.uow_factory() as uow:
            stored = await uow.tasks.get(task.workspace_id, task.id)
            current = stored or task
            if result.status.is_success or result.status is StageStatus.DEGRADED:
                current.succeed(
                    now=context.now(),
                    result=result.model_dump(mode="json"),
                    fingerprint=current.input_fingerprint,
                )
                if result.status is StageStatus.DEGRADED:
                    current.state = TaskState.DEGRADED
            elif result.status is StageStatus.AWAITING_USER:
                current.state = TaskState.AWAITING_USER
                current.human_action_required = result.human_action_required
                current.lease_owner = None
                current.lease_expires_at = None
                current.result = result.model_dump(mode="json")
            elif result.status is StageStatus.QUARANTINED:
                current.state = TaskState.QUARANTINED
                current.result = result.model_dump(mode="json")
            elif result.status is StageStatus.BLOCKED:
                current.state = TaskState.BLOCKED
                current.result = result.model_dump(mode="json")
            elif result.status is StageStatus.CANCELLED:
                current.cancel(now=context.now())
            else:
                current.fail(
                    now=context.now(),
                    error_class=result.error_class or ErrorClass.INTERNAL,
                    detail=result.error_detail or "stage failed",
                    retry_after_seconds=result.retry_after_seconds,
                    human_action=result.human_action_required,
                )
            await uow.tasks.update(current)
            if current.state is TaskState.DEAD_LETTER:
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=task.workspace_id,
                        actor_type=ActorType.WORKER,
                        actor_id=None,
                        action=AuditAction.TASK_DEAD_LETTERED,
                        subject_type="task",
                        subject_id=task.id,
                        previous_hash=await uow.audit.latest_hash(task.workspace_id),
                        reason_code=(current.error_class.value if current.error_class else None),
                        metadata={"stage": task.stage.value, "attempts": current.attempt},
                    )
                )
            if current.state is TaskState.SUCCEEDED:
                for unblocked in await uow.tasks.release_dependencies(task.workspace_id, task.id):
                    await uow.tasks.update(unblocked)
            await uow.commit()

        if result.next_tasks:
            await self.submit_requests(
                result.next_tasks,
                workspace_id=task.workspace_id,
                actor_id=command.actor_id,
                run_id=task.run_id,
            )
        return result

    async def _record_failure(self, task: Task, *, error_class: ErrorClass, detail: str) -> None:
        context = self._context
        async with context.uow_factory() as uow:
            stored = await uow.tasks.get(task.workspace_id, task.id)
            current = stored or task
            current.fail(now=context.now(), error_class=error_class, detail=detail)
            await uow.tasks.update(current)
            if current.state is TaskState.DEAD_LETTER:
                # Audited here as well as on the result path: a handler that raises
                # must not exhaust its retries invisibly.
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=task.workspace_id,
                        actor_type=ActorType.WORKER,
                        actor_id=None,
                        action=AuditAction.TASK_DEAD_LETTERED,
                        subject_type="task",
                        subject_id=task.id,
                        previous_hash=await uow.audit.latest_hash(task.workspace_id),
                        reason_code=error_class.value,
                        metadata={
                            "stage": task.stage.value,
                            "attempts": current.attempt,
                            "detail": detail[:200],
                        },
                    )
                )
            await uow.commit()

    async def cancel_run(self, *, workspace_id: UUID, run_id: UUID) -> int:
        """Stop scheduling new work for a run; completed outputs stay valid."""
        context = self._context
        cancelled = 0
        async with context.uow_factory() as uow:
            for task in await uow.tasks.list_tasks(workspace_id, limit=1000):
                if task.run_id != run_id or task.state.is_terminal:
                    continue
                task.cancel(now=context.now())
                await uow.tasks.update(task)
                cancelled += 1
            await uow.commit()
        return cancelled

    async def retry_dead_letter(self, *, workspace_id: UUID, task_id: UUID) -> Task:
        context = self._context
        async with context.uow_factory() as uow:
            task = await uow.tasks.get(workspace_id, task_id)
            if task is None:
                raise LabAssistantError(f"task {task_id} not found")
            task.state = TaskState.READY
            task.attempt = 0
            task.not_before = None
            task.error_class = None
            task.error_detail = None
            task.version += 1
            await uow.tasks.update(task)
            await uow.commit()
        return task

    async def queue_snapshot(self, *, workspace_id: UUID) -> dict[str, Any]:
        """Durable stage state for the operations page.

        Progress is read from task rows. Completion is never inferred from a
        missing worker heartbeat.
        """
        context = self._context
        async with context.uow_factory() as uow:
            counts = await uow.tasks.count_by_state(workspace_id)
            tasks = await uow.tasks.list_tasks(workspace_id, limit=500)
        oldest: dict[str, str] = {}
        awaiting: list[dict[str, Any]] = []
        for task in tasks:
            if task.state in (TaskState.READY, TaskState.LEASED) and task.created_at:
                key = task.stage.value
                current = oldest.get(key)
                stamp = task.created_at.isoformat()
                if current is None or stamp < current:
                    oldest[key] = stamp
            if task.state is TaskState.AWAITING_USER:
                awaiting.append(
                    {
                        "task_id": str(task.id),
                        "stage": task.stage.value,
                        "action": task.human_action_required,
                        "inputs": task.inputs,
                    }
                )
        return {
            "counts": counts,
            "oldest_pending_by_stage": oldest,
            "awaiting_user": awaiting,
            "dead_letters": [
                {"task_id": str(t.id), "stage": t.stage.value, "error": t.error_detail}
                for t in tasks
                if t.state is TaskState.DEAD_LETTER
            ],
        }
