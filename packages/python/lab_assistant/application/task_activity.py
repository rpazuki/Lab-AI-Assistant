"""What the workers are doing now, in words, and what limits how many run at once.

Read for the operations page. A running task is one a worker holds a live lease
on; one whose lease has run out is *stalled*: the worker stopped renewing it
(it crashed, or was restarted), and the next free worker of its class takes it
over. Nothing here is inferred from a missing heartbeat beyond that.

How many tasks run at once is set in two places, and the smaller wins:

* **workers** — each worker process runs one task at a time, and serves one
  class of stages (``network`` or ``compute``). Two network workers can run two
  network tasks at once;
* **stage limits** — ``stage_concurrency`` in the deployment's settings caps
  how many tasks of one stage are held at once, across every worker. A worker
  that finds a stage at its limit takes the next stage of its class instead.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_assistant.application.context import ServiceContext
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.states import TaskState
from lab_assistant.domain.tasks import Task

__all__ = ["RUNNING_LIMIT", "TaskActivityService"]

#: Running tasks listed at most; there are rarely more than the workers.
RUNNING_LIMIT = 200

#: What a task of each stage is doing, with ``{paper}``, ``{source}`` and
#: ``{scope}`` filled from its inputs where they name one.
_DOING: dict[StageName, str] = {
    StageName.SCOPE_FREEZE: "Freezing the scope {scope}",
    StageName.SCOPE_DELETION: "Deleting the scope {scope}",
    StageName.DISCOVERY: "Searching {source} for {scope}",
    StageName.RECONCILIATION: "Matching the records found to papers, for {scope}",
    StageName.ACQUISITION_PLANNING: "Planning how to fetch each included paper",
    StageName.ACQUISITION_SWEEP: "Queueing fetches for the included papers",
    StageName.ACQUISITION: "Fetching the full text of {paper}",
    StageName.PARSING: "Reading the text out of the file of {paper}",
    StageName.EXTRACTION: "Extracting values from {paper}",
    StageName.REVIEW: "Preparing review of {paper}",
    StageName.EXPORT: "Exporting",
    StageName.RELEASE_ASSEMBLY: "Assembling a release",
    StageName.INDEXING: "Indexing a release for Ask",
    StageName.EVALUATION: "Running an evaluation",
    StageName.PUBLICATION: "Publishing a release",
    StageName.REFRESH: "Refreshing what a change affects",
}


class TaskActivityService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def snapshot(self, *, workspace_id: UUID) -> dict[str, Any]:
        """The running tasks, each in words, and each stage's running, waiting and limit."""
        context = self._context
        now = context.now()
        settings = context.settings
        async with context.uow_factory() as uow:
            held = await uow.tasks.list_tasks(
                workspace_id, state=TaskState.LEASED.value, limit=RUNNING_LIMIT
            )
            ready = {
                stage: count
                for stage, count, _ in await uow.tasks.tasks_by_stage(
                    workspace_id, states=(TaskState.READY.value,)
                )
            }
            described = [await _describe(uow, workspace_id, task) for task in held]

        running = [
            {
                "task_id": str(task.id),
                "stage": task.stage.value,
                "worker_class": task.worker_class,
                "worker": task.lease_owner,
                "doing": doing,
                "paper": paper,
                "attempt": task.attempt,
                "max_attempts": task.max_attempts,
                "first_started_at": _iso(task.started_at),
                "heartbeat_at": _iso(task.heartbeat_at),
                "lease_expires_at": _iso(task.lease_expires_at),
                # The worker stopped renewing it: another will take it over.
                "stalled": task.lease_expires_at is not None and task.lease_expires_at <= now,
            }
            for task, (doing, paper) in zip(held, described, strict=True)
        ]
        running.sort(key=lambda row: (row["stalled"], row["first_started_at"] or ""))

        live: dict[str, int] = {}
        for row in running:
            if not row["stalled"]:
                live[row["stage"]] = live.get(row["stage"], 0) + 1
        limits = settings.stage_concurrency
        shown = set(limits) | set(live) | set(ready)
        stages = [
            {
                "stage": stage.value,
                "worker_class": stage.worker_class,
                "running": live.get(stage.value, 0),
                # A stage the settings do not name may hold one task at a time.
                "limit": limits.get(stage.value, 1),
                "ready": ready.get(stage.value, 0),
            }
            for stage in StageName
            if stage.value in shown
        ]
        return {
            "running": running,
            "stages": stages,
            # Only the busy workers are known: an idle one holds no task to show.
            "busy_workers": [
                {"worker": worker, "worker_class": worker_class}
                for worker, worker_class in sorted(
                    {
                        (row["worker"], row["worker_class"])
                        for row in running
                        if row["worker"] and not row["stalled"]
                    }
                )
            ],
            "lease_seconds": settings.worker_lease_seconds,
            "poll_seconds": settings.worker_poll_interval_seconds,
        }


async def _describe(uow: Any, workspace_id: UUID, task: Task) -> tuple[str, dict[str, Any] | None]:
    """The task in words, and the paper it is about, if one."""
    inputs = task.inputs or {}
    work_id = await _work_of(uow, workspace_id, inputs)
    paper: dict[str, Any] | None = None
    if work_id is not None:
        summaries = await uow.works.get_summaries(workspace_id, [work_id])
        dois = await uow.works.list_dois(workspace_id, [work_id])
        summary = summaries.get(work_id)
        paper = {
            "work_id": str(work_id),
            "title": summary.title if summary else None,
            "year": summary.year if summary else None,
            "dois": dois.get(work_id, []),
        }
    scope = await _scope_name(uow, workspace_id, inputs)
    template = _DOING.get(task.stage, task.stage.value.replace("_", " ").capitalize())
    doing = template.format(
        paper=f"“{paper['title']}”" if paper and paper["title"] else "a paper",
        source=str(inputs.get("connector_id") or "a source"),
        scope=f"“{scope}”" if scope else "a scope",
    )
    return doing, paper


async def _work_of(uow: Any, workspace_id: UUID, inputs: dict[str, Any]) -> UUID | None:
    """The paper a task's inputs lead to: directly, or by its plan, file or text."""
    try:
        if inputs.get("work_id"):
            return UUID(str(inputs["work_id"]))
        if inputs.get("plan_id"):
            plan = await uow.assets.get_plan(workspace_id, UUID(str(inputs["plan_id"])))
            return plan.work_id if plan else None
        if inputs.get("asset_id"):
            asset = await uow.assets.get_asset(workspace_id, UUID(str(inputs["asset_id"])))
            return asset.work_id if asset else None
        if inputs.get("rendition_id"):
            rendition = await uow.renditions.get_rendition(
                workspace_id, UUID(str(inputs["rendition_id"]))
            )
            return rendition.work_id if rendition else None
    except ValueError:
        return None
    return None


async def _scope_name(uow: Any, workspace_id: UUID, inputs: dict[str, Any]) -> str | None:
    """The name of the scope a task's inputs name, directly or by its discovery run."""
    try:
        version_id = inputs.get("scope_version_id")
        if not version_id and inputs.get("discovery_run_id"):
            run = await uow.observations.get_run(
                workspace_id, UUID(str(inputs["discovery_run_id"]))
            )
            version_id = run.scope_version_id if run else None
        if not version_id:
            return None
        version = await uow.configs.get_scope_version(workspace_id, UUID(str(version_id)))
    except ValueError:
        return None
    return version.name if version else None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
