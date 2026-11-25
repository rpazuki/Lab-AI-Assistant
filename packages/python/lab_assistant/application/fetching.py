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

* a fetch that failed because its file could not be tied to its paper, once,
  when the identity rule has changed since (it now also accepts the paper's
  title, and a preprint of it): the stored file is checked again without being
  fetched again.

A paper whose only route is a person's upload is planned again when discovery
now records a licence that allows keeping a copy (a later run found an open
licence the first did not): it gets the open routes and goes back in the queue.
Until then it waits for an upload. The licence is checked against the rights
policy without writing anything first, so a press over thousands of upload-only
papers records a rights decision only for the ones that now pass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from lab_assistant.application.acquisition import (
    IDENTITY_RULE,
    PLAN_ACTIONS,
    AcquisitionService,
    manifestation_kind,
    route_ladder,
)
from lab_assistant.application.acquisition import IMPLEMENTATION_VERSION as FETCHER_VERSION
from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.contracts.errors import AcquisitionOutcome, ErrorClass
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.assets import AcquisitionRoute, ManifestationKind
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.states import RightsState, TaskState
from lab_assistant.domain.tasks import Task
from lab_assistant.domain.workspace import Capability
from lab_assistant.policy.rights import RightsRequest

__all__ = ["FetchQueue"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Outcomes that answer nothing about the paper: the source may answer next time.
_NO_ANSWER = frozenset(
    {
        AcquisitionOutcome.RATE_LIMITED_RETRYABLE,
        AcquisitionOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
    }
)


async def _recheckable(uow: Any, ws: UUID, task: Any, work_id: UUID) -> bool:
    """A fetch that failed on identity, whose stored file an older rule checked."""
    if task.error_class is not ErrorClass.IDENTITY_MISMATCH:
        return False
    assets = await uow.assets.list_assets(ws, work_id=work_id)
    return any(
        not asset.identity_verified and (asset.metadata or {}).get("identity_rule") != IDENTITY_RULE
        for asset in assets
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
        upload_only: list[tuple[UUID, UUID]] = []
        requeued = kept = tried = 0
        after: tuple[datetime, UUID] | None = None
        while True:
            async with context.uow_factory() as uow:
                page = await uow.tasks.page_tasks(
                    ws,
                    states=[
                        TaskState.AWAITING_USER.value,
                        TaskState.DEAD_LETTER.value,
                        TaskState.FAILED.value,
                    ],
                    stage=StageName.ACQUISITION.value,
                    limit=_PAGE,
                    after=after,
                )
                for task in page:
                    plan_id = task.inputs.get("plan_id")
                    plan = await uow.assets.get_plan(ws, UUID(str(plan_id))) if plan_id else None
                    if plan is None:
                        kept += 1
                        continue
                    if not any(route.is_automatic for route in plan.route_order):
                        upload_only.append((task.id, plan.id))
                        continue
                    if task.state is TaskState.FAILED and not await _recheckable(
                        uow, ws, task, plan.work_id
                    ):
                        tried += 1
                        continue
                    if task.state is TaskState.AWAITING_USER and _answered(
                        await uow.assets.list_attempts(ws, plan_id=plan.id)
                    ):
                        tried += 1
                        continue
                    await _requeue(uow, task, plan)
                    requeued += 1
                await uow.commit()
            if len(page) < _PAGE or page[-1].created_at is None:
                break
            after = (page[-1].created_at, page[-1].id)
        replanned = 0
        for task_id, plan_id in upload_only:
            if await self._replan(actor, task_id, plan_id):
                replanned += 1
            else:
                kept += 1
        requeued += replanned
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
                        "replanned": replanned,
                        "kept_for_a_person": kept,
                        "already_tried": tried,
                    },
                )
            )
            await uow.commit()
        return {
            "requeued": requeued,
            "replanned": replanned,
            "kept_for_a_person": kept,
            "already_tried": tried,
        }

    async def _replan(self, actor: ActorContext, task_id: UUID, plan_id: UUID) -> bool:
        """Give an upload-only plan the open routes, if discovery now records an open licence.

        Each licence any source gave is checked against the rights policy without
        writing anything; only one that allows keeping a copy is decided on the
        record, and only then does the plan change. Returns whether it did.
        """
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            plan = await uow.assets.get_plan(ws, plan_id)
            work = await uow.works.get(ws, plan.work_id) if plan is not None else None
            if plan is None or work is None or not plan.manifestation_ids:
                return False
            manifestation = await uow.assets.get_manifestation(ws, plan.manifestation_ids[0])
            assertions = await uow.observations.list_assertions(ws, work_id=plan.work_id)
        if manifestation is None:
            return False
        licences = {
            str(value): assertion.id
            for assertion in assertions
            if assertion.predicate.value == "licence"
            and isinstance(value := assertion.normalised_value_json or assertion.value_json, str)
        }
        location = next(
            (
                a.normalised_value_json or a.value_json
                for a in assertions
                if a.predicate.value == "full_text_location"
                and (a.normalised_value_json or a.value_json)
            ),
            None,
        )
        kind = manifestation_kind(location)
        route = (
            AcquisitionRoute.OPEN_ACCESS_API
            if kind is ManifestationKind.PMC_XML
            else AcquisitionRoute.OPEN_REPOSITORY
        )
        actions = plan.requested_actions or PLAN_ACTIONS
        for licence, assertion_id in sorted(licences.items()):
            verdict = context.rights_policy.evaluate(
                RightsRequest(
                    workspace_id=ws,
                    subject_type="manifestation",
                    subject_id=manifestation.id,
                    licence_expression=licence,
                    access_basis=str(location) if location else None,
                    route=route,
                    content_kind=work.work_kind.value,
                    requested_actions=actions,
                    evidence_assertion_ids=(assertion_id,),
                    now=context.now(),
                )
            )
            if (
                verdict.state in (RightsState.PROHIBITED, RightsState.UNKNOWN)
                or PolicyAction.STORE_CONTENT not in verdict.allowed_actions
            ):
                continue
            decision = await AcquisitionService(context).decide_rights(
                actor,
                subject_type="manifestation",
                subject_id=manifestation.id,
                licence_expression=licence,
                access_basis=str(location) if location else None,
                route=route,
                content_kind=work.work_kind.value,
                requested_actions=actions,
                evidence_assertion_ids=(assertion_id,),
            )
            routes = route_ladder(decision, kind, now=context.now())
            if not any(r.is_automatic for r in routes):
                continue
            async with context.uow_factory() as uow:
                plan = await uow.assets.get_plan(ws, plan_id)
                task = await uow.tasks.get(ws, task_id)
                if plan is None or task is None:
                    return False
                if (
                    kind is not ManifestationKind.ABSTRACT_ONLY
                    and manifestation.manifestation_kind
                    in (ManifestationKind.ABSTRACT_ONLY, ManifestationKind.UNKNOWN)
                ):
                    # Discovery now knows where the full text is.
                    await uow.assets.add_manifestation(
                        manifestation.model_copy(update={"manifestation_kind": kind})
                    )
                plan.route_order = tuple(routes)
                await _requeue(uow, task, plan, force_plan=True)
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=ws,
                        actor_type=ActorType.USER,
                        actor_id=actor.user_id,
                        action=AuditAction.ACQUISITION_REQUEUED,
                        subject_type="acquisition_plan",
                        subject_id=plan.id,
                        previous_hash=await uow.audit.latest_hash(ws),
                        reason_code="replanned_open_licence",
                        metadata={
                            "licence": licence,
                            "rights_decision_id": str(decision.id),
                            "routes": [r.value for r in routes],
                        },
                    )
                )
                await uow.commit()
            return True
        return False


async def _requeue(uow: Any, task: Any, plan: Any, *, force_plan: bool = False) -> None:
    """Put a fetch back in the queue as if new, and its plan back to planned."""
    task.state = TaskState.READY
    task.attempt = 0
    task.not_before = None
    task.human_action_required = None
    task.error_class = None
    task.error_detail = None
    task.completed_at = None
    task.version += 1
    await uow.tasks.update(task)
    if force_plan or plan.status != "planned":
        plan.status = "planned"
        plan.version += 1
        await uow.assets.update_plan(plan)
