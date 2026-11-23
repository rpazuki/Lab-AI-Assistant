"""Getting included papers fetched: planning what a person included, and trying again.

Planning used to run once, right after a discovery run was grouped into papers,
over the papers included at that moment. Everything a person included afterwards,
one at a time, by filter or by the scope's rules, was never planned, so it was
never fetched, and nothing said so. ``plan_included`` queues planning again; the
planner skips any paper that already has a plan, so asking twice costs a scan and
plans nothing twice.

``retry_waiting`` puts fetches that ended waiting for a person back in the queue,
but only those with an automatic route left to try: a paper whose only route is a
person's upload would only end where it started.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.states import TaskState
from lab_assistant.domain.tasks import Task
from lab_assistant.domain.workspace import Capability

__all__ = ["FetchQueue"]

_PAGE = 1_000


class FetchQueue:
    """Queue planning for included papers, and retry fetches that can still succeed."""

    def __init__(self, context: ServiceContext, orchestrator: Any) -> None:
        self._context = context
        self._orchestrator = orchestrator

    async def plan_included(self, actor: ActorContext, *, reason: str) -> Task:
        """Queue acquisition planning for every included paper without a plan.

        A planning task that has not started yet will see these papers too, so it
        is returned instead of queuing a second one. One already running may have
        read the papers before this change, so a new one is queued behind it.
        """
        self._context.require_installation(InstallationCapability.ACQUIRE_SOURCES)
        ws = actor.workspace_id
        async with self._context.uow_factory() as uow:
            waiting = await uow.tasks.page_tasks(
                ws,
                states=[TaskState.READY.value, TaskState.PENDING.value],
                stage=StageName.ACQUISITION_PLANNING.value,
                limit=1,
            )
        if waiting:
            return waiting[0]
        return await self._orchestrator.submit(
            workspace_id=ws,
            actor_id=actor.user_id,
            stage=StageName.ACQUISITION_PLANNING,
            inputs={"included_by": reason},
            # Each request is its own task: the inputs alone would match the last
            # planning run and return it, finished, without planning anything.
            idempotency_key=f"plan-included:{self._context.new_id()}",
        )

    async def retry_waiting(self, actor: ActorContext) -> dict[str, int]:
        """Put waiting fetches with an automatic route back in the queue.

        Returns how many were queued again and how many stay with a person.
        """
        self._context.require_installation(InstallationCapability.ACQUIRE_SOURCES)
        actor.require(Capability.ACQUIRE)
        context = self._context
        ws = actor.workspace_id
        requeued = kept = 0
        after: tuple[datetime, UUID] | None = None
        while True:
            async with context.uow_factory() as uow:
                page = await uow.tasks.page_tasks(
                    ws,
                    states=[TaskState.AWAITING_USER.value],
                    stage=StageName.ACQUISITION.value,
                    limit=_PAGE,
                    after=after,
                )
                for task in page:
                    plan_id = task.inputs.get("plan_id")
                    plan = await uow.assets.get_plan(ws, UUID(str(plan_id))) if plan_id else None
                    if plan is None or not any(route.is_automatic for route in plan.route_order):
                        kept += 1
                        continue
                    task.state = TaskState.READY
                    task.attempt = 0
                    task.not_before = None
                    task.human_action_required = None
                    task.error_class = None
                    task.error_detail = None
                    task.completed_at = None
                    task.version += 1
                    await uow.tasks.update(task)
                    if plan.status != "planned":
                        plan.status = "planned"
                        plan.version += 1
                        await uow.assets.update_plan(plan)
                    requeued += 1
                await uow.commit()
            if len(page) < _PAGE or page[-1].created_at is None:
                break
            after = (page[-1].created_at, page[-1].id)
        async with context.uow_factory() as uow:
            await uow.audit.append(
                context.audit.build(
                    workspace_id=ws,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.ACQUISITION_REQUEUED,
                    subject_type="workspace",
                    subject_id=ws,
                    previous_hash=await uow.audit.latest_hash(ws),
                    reason_code="retry_waiting_fetches",
                    metadata={"requeued": requeued, "kept_for_a_person": kept},
                )
            )
            await uow.commit()
        return {"requeued": requeued, "kept_for_a_person": kept}
