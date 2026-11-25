"""The operations page's lists, a page at a time: tasks oldest first, audit newest first.

Over memory, SQLite and (with ``LAB_TEST_DATABASE_URL``) Postgres, through the
bulk-review fixture. Each walk follows ``next_cursor`` to the end and must visit
every row exactly once, in order, whatever the page size, including rows that
share a timestamp.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any
from uuid import UUID

from test_bulk_review import lab  # noqa: F401  (the fixture)

from lab_assistant.application.tasks import TaskQueue
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.states import TaskState
from lab_assistant.domain.tasks import Task


def _cursor(value: str | None) -> Any:
    if value is None:
        return None
    from datetime import datetime

    at, last = value.split("|", 1)
    return datetime.fromisoformat(at), UUID(last)


async def _add_tasks(lab, specs: list[tuple[StageName, TaskState, int]]) -> list[UUID]:  # noqa: F811
    context = lab.harness.context
    start = context.now()
    ids = []
    async with context.uow_factory() as uow:
        for stage, state, minutes in specs:
            task = Task(
                id=context.new_id(),
                workspace_id=lab.ws,
                stage=stage,
                state=state,
                input_fingerprint=str(context.new_id()),
                created_at=start + timedelta(minutes=minutes),
                human_action_required="approve" if state is TaskState.AWAITING_USER else None,
            )
            await uow.tasks.add(task)
            ids.append(task.id)
        await uow.commit()
    return ids


async def _walk_queue(lab, queue: TaskQueue, *, size: int, stage: str | None = None):  # noqa: F811
    orchestrator = lab.harness.orchestrator
    seen, after, total = [], None, None
    while True:
        page = await orchestrator.page_queue(
            workspace_id=lab.ws, queue=queue, stage=stage, limit=size, after=_cursor(after)
        )
        seen.extend(item["task_id"] for item in page["items"])
        total = page["total"]
        after = page["next_cursor"]
        if after is None:
            return seen, total


class TestTaskQueues:
    async def test_every_waiting_task_once_oldest_first(self, lab) -> None:  # noqa: F811
        # Two tasks share a minute, so the id breaks the tie.
        specs = [(StageName.RECONCILIATION, TaskState.READY, m) for m in (5, 1, 3, 3, 9, 7, 2)]
        specs.append((StageName.ACQUISITION, TaskState.LEASED, 0))
        specs.append((StageName.ACQUISITION, TaskState.SUCCEEDED, -10))  # not waiting
        ids = await _add_tasks(lab, specs)

        for size in (1, 3, 50):
            seen, total = await _walk_queue(lab, TaskQueue.WAITING, size=size)
            assert total == 8
            assert len(seen) == len(set(seen)) == 8
            assert seen[0] == str(ids[7])  # the leased acquisition task waited longest
            assert str(ids[8]) not in seen

        only, total = await _walk_queue(lab, TaskQueue.WAITING, size=2, stage="acquisition")
        assert (only, total) == ([str(ids[7])], 1)

    async def test_the_snapshot_counts_every_task_not_a_sample(self, lab) -> None:  # noqa: F811
        await _add_tasks(
            lab,
            [(StageName.RECONCILIATION, TaskState.READY, m) for m in range(60)]
            + [(StageName.ACQUISITION, TaskState.AWAITING_USER, m) for m in range(55)],
        )
        snapshot = await lab.harness.orchestrator.queue_snapshot(workspace_id=lab.ws)
        assert snapshot["counts"]["ready"] == 60
        stages = {row["stage"]: row["count"] for row in snapshot["waiting_by_stage"]}
        assert stages == {"reconciliation": 60}
        assert "reconciliation" in snapshot["oldest_pending_by_stage"]
        assert len(snapshot["awaiting_user"]) == 50  # the first page; the rest are paged
        seen, total = await _walk_queue(lab, TaskQueue.AWAITING_USER, size=20)
        assert total == len(seen) == 55


class TestAuditTrail:
    async def _append(self, lab, action: AuditAction, reason: str, **metadata: Any) -> None:  # noqa: F811
        context = lab.harness.context
        async with context.uow_factory() as uow:
            await uow.audit.append(
                context.audit.build(
                    workspace_id=lab.ws,
                    actor_type=ActorType.USER,
                    actor_id=lab.actor.user_id,
                    action=action,
                    subject_type="scope",
                    subject_id=context.new_id(),
                    previous_hash=await uow.audit.latest_hash(lab.ws),
                    reason_code=reason,
                    metadata=metadata,
                )
            )
            await uow.commit()

    async def _walk(self, lab, *, size: int, **filters: Any) -> tuple[list[str], int]:  # noqa: F811
        seen, before = [], None
        async with lab.harness.context.uow_factory() as uow:
            total = await uow.audit.count_events(lab.ws, **filters)
            while True:
                page = await uow.audit.page_events(lab.ws, limit=size, before=before, **filters)
                seen.extend(event.reason_code for event in page)
                if len(page) < size:
                    return seen, total
                before = (page[-1].occurred_at, page[-1].id)

    async def test_newest_first_every_event_once_and_filtered(self, lab) -> None:  # noqa: F811
        async with lab.harness.context.uow_factory() as uow:
            already = await uow.audit.count_events(lab.ws)
        for n in range(7):
            action = AuditAction.SCOPE_DELETED if n % 3 == 0 else AuditAction.SCOPE_APPROVED
            await self._append(lab, action, f"r{n}", note=f"Yarrowia batch-{n}")

        for size in (1, 2, 50):
            seen, total = await self._walk(lab, size=size)
            assert total == already + 7
            mine = [r for r in seen if r and r.startswith("r")]
            assert mine == [f"r{n}" for n in reversed(range(7))]

        deleted, total = await self._walk(lab, size=2, actions=["scope_deleted"])
        assert (deleted, total) == (["r6", "r3", "r0"], 3)
        found, total = await self._walk(lab, size=2, words=["yarrowia", "BATCH-4"])
        assert (found, total) == (["r4"], 1)
        none, total = await self._walk(lab, size=2, words=["100%"])
        assert (none, total) == ([], 0)

        async with lab.harness.context.uow_factory() as uow:
            counts = await uow.audit.count_events_by_action(lab.ws)
        assert counts["scope_deleted"] == 3


class TestSearchingTheLists:
    async def test_words_and_times_search_every_task_not_the_page(self, lab) -> None:  # noqa: F811
        ids = await _add_tasks(
            lab,
            [(StageName.ACQUISITION, TaskState.AWAITING_USER, m) for m in (0, 61, 125)]
            + [(StageName.RECONCILIATION, TaskState.AWAITING_USER, 0)],
        )
        orchestrator = lab.harness.orchestrator
        async with lab.harness.context.uow_factory() as uow:
            later = await uow.tasks.get(lab.ws, ids[2])
        stamp = later.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")

        async def search(*words: str) -> tuple[set[str], int]:
            page = await orchestrator.page_queue(
                workspace_id=lab.ws, queue=TaskQueue.AWAITING_USER, limit=1, words=words
            )
            return {item["task_id"] for item in page["items"]}, page["total"]

        assert (await search("ACQUISITION"))[1] == 3  # every match counted, one shown
        assert await search(stamp) == ({str(ids[2])}, 1)  # the time as the page shows it
        assert await search("approve", "reconciliation") == ({str(ids[3])}, 1)
        assert await search(str(ids[1])) == ({str(ids[1])}, 1)

    async def test_the_audit_log_searches_its_times(self, lab) -> None:  # noqa: F811
        await TestAuditTrail()._append(lab, AuditAction.SCOPE_APPROVED, "dated")
        async with lab.harness.context.uow_factory() as uow:
            [event] = await uow.audit.page_events(lab.ws, words=["dated"], limit=5)
            stamp = event.occurred_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
            found = await uow.audit.count_events(lab.ws, words=[stamp, "dated"])
        assert found == 1
