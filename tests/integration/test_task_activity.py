"""The operations page's "Running now": each held task in words, and the limits.

Over memory, SQLite and (with ``LAB_TEST_DATABASE_URL``) Postgres, through the
bulk-review fixture.
"""

from __future__ import annotations

from datetime import timedelta

from test_bulk_review import lab  # noqa: F401  (the fixture)
from test_scope_deletion import DeletionLab

from lab_assistant.application.task_activity import TaskActivityService
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.assets import AcquisitionPlan
from lab_assistant.domain.states import TaskState
from lab_assistant.domain.tasks import Task


class TestRunningNow:
    async def test_each_held_task_in_words_with_its_paper_and_the_stage_limits(
        self,
        lab,  # noqa: F811
    ) -> None:
        context = lab.harness.context
        now = context.now()
        work_id = await lab.paper("Lipid bodies in Yarrowia", doi="10.1016/j.ymben.2024.1")
        scopes = DeletionLab(lab.harness, lab.actor)
        scope = await scopes.scope()
        run = await scopes.run(scope)
        plan = AcquisitionPlan(
            id=context.new_id(), workspace_id=lab.ws, work_id=work_id, created_at=now
        )

        def task(stage: StageName, state: TaskState, inputs: dict, **lease) -> Task:
            return Task(
                id=context.new_id(),
                workspace_id=lab.ws,
                stage=stage,
                state=state,
                input_fingerprint=str(context.new_id()),
                inputs=inputs,
                created_at=now,
                **lease,
            )

        live = {
            "lease_owner": "host-a/network",
            "attempt": 2,
            "started_at": now - timedelta(hours=1),
            "heartbeat_at": now,
            "lease_expires_at": now + timedelta(minutes=5),
        }
        fetching = task(StageName.ACQUISITION, TaskState.LEASED, {"plan_id": str(plan.id)}, **live)
        searching = task(
            StageName.DISCOVERY,
            TaskState.LEASED,
            {"connector_id": "pubmed", "discovery_run_id": str(run.id)},
            **live,
        )
        # Its worker went quiet: the lease ran out a minute ago.
        stalled = task(
            StageName.PARSING,
            TaskState.LEASED,
            {"asset_id": str(context.new_id())},
            lease_owner="host-b/compute",
            attempt=1,
            started_at=now - timedelta(hours=2),
            heartbeat_at=now - timedelta(minutes=11),
            lease_expires_at=now - timedelta(minutes=1),
        )
        waiting = [task(StageName.ACQUISITION, TaskState.READY, {}) for _ in range(3)]
        async with context.uow_factory() as uow:
            await uow.assets.add_plan(plan)
            for row in (fetching, searching, stalled, *waiting):
                await uow.tasks.add(row)
            await uow.commit()

        view = await TaskActivityService(context).snapshot(workspace_id=lab.ws)

        by_id = {row["task_id"]: row for row in view["running"]}
        assert len(by_id) == 3
        fetch = by_id[str(fetching.id)]
        assert fetch["doing"] == "Fetching the full text of “Lipid bodies in Yarrowia”"
        assert fetch["paper"]["dois"] == ["10.1016/j.ymben.2024.1"]
        assert (fetch["worker"], fetch["attempt"], fetch["stalled"]) == ("host-a/network", 2, False)
        assert by_id[str(searching.id)]["doing"] == f"Searching pubmed for “{scope.name}”"
        # A file that cannot be found still says what the task does.
        parse = by_id[str(stalled.id)]
        assert parse["doing"] == "Reading the text out of the file of a paper"
        assert parse["stalled"] is True
        # Live tasks first, the stalled one last.
        assert view["running"][-1]["task_id"] == str(stalled.id)

        stages = {row["stage"]: row for row in view["stages"]}
        limits = context.settings.stage_concurrency
        assert stages["acquisition"] == {
            "stage": "acquisition",
            "worker_class": "network",
            "running": 1,
            "limit": limits["acquisition"],
            "ready": 3,
        }
        # A stalled task is not counted against its stage's limit.
        assert stages["parsing"]["running"] == 0
        assert view["busy_workers"] == [{"worker": "host-a/network", "worker_class": "network"}]
        assert view["lease_seconds"] == context.settings.worker_lease_seconds

    async def test_nothing_running(self, lab) -> None:  # noqa: F811
        view = await TaskActivityService(lab.harness.context).snapshot(workspace_id=lab.ws)
        assert view["running"] == []
        assert view["busy_workers"] == []
        assert all(row["running"] == 0 for row in view["stages"])
