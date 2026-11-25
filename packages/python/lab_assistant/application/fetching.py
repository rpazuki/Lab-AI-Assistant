"""Getting included papers fetched: planning what a person included, and trying again.

Planning used to run once, right after a discovery run was grouped into papers,
over the papers included at that moment. Everything a person included afterwards,
one at a time, by filter or by the scope's rules, was never planned, so it was
never fetched, and nothing said so. ``plan_included`` queues planning again; the
planner skips any paper that already has a plan, so asking twice costs a scan and
plans nothing twice.

``retry_waiting`` puts fetches back in the queue when trying again can change the
answer:

* a dead letter, always: it ran out of retries on a fault, not on the paper;
* a fetch waiting for a person, only if it has an automatic route and the fetcher
  in use has not had a definite answer from every source it asked. A source that
  was rate-limiting or down gave no answer, so that paper is tried again. Once
  every source has answered (not found, a sign-in page, a refusal), asking again
  would send the same requests for the same answer, so a second press leaves the
  paper waiting for a person.

A paper whose only route is a person's upload is never queued again here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from lab_assistant.application.acquisition import IMPLEMENTATION_VERSION as FETCHER_VERSION
from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import AcquisitionOutcome
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.states import TaskState
from lab_assistant.domain.tasks import Task
from lab_assistant.domain.workspace import Capability

__all__ = ["FetchQueue"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Outcomes that answer nothing about the paper: the source may answer next time.
_NO_ANSWER = frozenset(
    {
        AcquisitionOutcome.RATE_LIMITED_RETRYABLE,
        AcquisitionOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
    }
)


def _answered(attempts: list[Any]) -> bool:
    """Whether the current fetcher had a definite answer from every source it asked.

    The latest attempt per source decides: one that was rate-limited or down is
    no answer, so the paper is worth trying again.
    """

    def stamp(attempt: Any) -> tuple[datetime, int]:
        moment = attempt.requested_at or attempt.completed_at or _EPOCH
        return (moment if moment.tzinfo else moment.replace(tzinfo=UTC), attempt.attempt_number)

    latest: dict[str, Any] = {}
    for attempt in attempts:
        if attempt.worker_version != FETCHER_VERSION:
            continue
        current = latest.get(attempt.connector_id)
        if current is None or stamp(attempt) >= stamp(current):
            latest[attempt.connector_id] = attempt
    return bool(latest) and all(a.outcome not in _NO_ANSWER for a in latest.values())


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
        """Put waiting and dead-lettered fetches with an automatic route back in the queue.

        Dead letters are included because the ones a fetch leaves are a fault the
        fetch had, not the paper: retrying them after the fix is the point.
        Returns how many were queued again, how many can only be uploaded, and how
        many the current fetcher already tried and handed to a person.
        """
        self._context.require_installation(InstallationCapability.ACQUIRE_SOURCES)
        actor.require(Capability.ACQUIRE)
        context = self._context
        ws = actor.workspace_id
        requeued = kept = tried = 0
        after: tuple[datetime, UUID] | None = None
        while True:
            async with context.uow_factory() as uow:
                page = await uow.tasks.page_tasks(
                    ws,
                    states=[TaskState.AWAITING_USER.value, TaskState.DEAD_LETTER.value],
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
                    if task.state is TaskState.AWAITING_USER and _answered(
                        await uow.assets.list_attempts(ws, plan_id=plan.id)
                    ):
                        tried += 1
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
                    metadata={
                        "requeued": requeued,
                        "kept_for_a_person": kept,
                        "already_tried": tried,
                    },
                )
            )
            await uow.commit()
        return {"requeued": requeued, "kept_for_a_person": kept, "already_tried": tried}
