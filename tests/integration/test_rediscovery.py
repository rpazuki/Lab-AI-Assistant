"""Running a scope's discovery again: a page the source answers differently is kept.

The VM's second run of a scope failed for DataCite with
``duplicate key value violates unique constraint "request_fingerprint"``: the same
request as the first run (same connector version, query and page), but the
source's answer had changed. A page is stored once and shared while its bytes are
the same; one that changed is a new observation, stored for the new run.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from lab_assistant.adapters.connectors.base import compile_query_fingerprint, make_response
from lab_assistant.adapters.db.engine import create_schema, make_engine
from lab_assistant.adapters.db.repositories import sql_uow_factory
from lab_assistant.application.discovery import DiscoveryHandler, DiscoveryService
from lab_assistant.config import LabEnv, Settings
from lab_assistant.contracts.connector import CompiledQuery, Cursor, RawPage
from lab_assistant.contracts.errors import SourceOutcome, StateTransitionError
from lab_assistant.contracts.stage import StageCommand, StageName
from lab_assistant.domain.observation import ConnectorRunState, DiscoveryRun, DiscoveryRunStatus
from lab_assistant.domain.scope import ScopeVersion
from lab_assistant.domain.states import TaskState
from lab_assistant.domain.tasks import Task
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor, reset_database

BACKENDS = ["memory", "sqlite"] + (["postgres"] if os.environ.get("LAB_TEST_DATABASE_URL") else [])
URL = "https://api.datacite.org/dois?query=yarrowia&page%5Bnumber%5D=1&page%5Bsize%5D=50"


def body(total: int) -> bytes:
    return b'{"records": [{"locator": "doi:10.5/x", "assertions": []}], "total": %d}' % total


@pytest.fixture(params=BACKENDS)
async def lab(request, tmp_path) -> Any:
    engine = None
    if request.param == "memory":
        harness = build_harness()
    else:
        settings = Settings(
            env=LabEnv.LOCAL,
            database_url=(
                os.environ["LAB_TEST_DATABASE_URL"]
                if request.param == "postgres"
                else "sqlite+aiosqlite:///:memory:"
            ),
            object_store_root=tmp_path / "objects",
        )
        engine = make_engine(settings)
        await create_schema(engine, dimensions=(64,))
        await reset_database(engine)
        harness = build_harness(uow_factory=sql_uow_factory(engine))
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    actor = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    context = harness.context
    scope = ScopeVersion(
        id=context.new_id(),
        workspace_id=workspace.id,
        scope_id=context.new_id(),
        version=1,
        name="Yarrowia",
        scientific_objective="Yarrowia lipolytica",
        created_by=actor.user_id,
        created_at=context.now(),
    )
    async with context.uow_factory() as uow:
        await uow.configs.add_scope_version(scope)
        await uow.commit()
    try:
        yield harness, actor, scope
    finally:
        if engine is not None:
            await engine.dispose()


async def _run(harness, actor, scope, *, status=DiscoveryRunStatus.RUNNING) -> DiscoveryRun:
    context = harness.context
    run = DiscoveryRun(
        id=context.new_id(),
        workspace_id=actor.workspace_id,
        scope_version_id=scope.id,
        status=status,
        run_fingerprint=f"run-{context.new_id()}",
        requested_by=actor.user_id,
        started_at=context.now(),
        connector_states=(
            ConnectorRunState(
                connector_id="datacite",
                connector_version="1.0.0",
                required=True,
                query_fingerprint="q",
                outcome=SourceOutcome.FAILED_PERMANENT,
                degraded_reason="duplicate key",
            ),
        ),
    )
    async with context.uow_factory() as uow:
        await uow.observations.add_run(run)
        await uow.commit()
    return run


async def _store(harness, actor, scope, run, content: bytes) -> tuple[int, int]:
    compiled = CompiledQuery(
        connector_id="datacite",
        connector_version="1.0.0",
        query_text="yarrowia",
        query_fingerprint=compile_query_fingerprint(
            connector_id="datacite", connector_version="1.0.0", query="yarrowia", parameters={}
        ),
    )
    page = RawPage(
        response=make_response(method="GET", url_redacted=URL, status=200, body=content),
        cursor_before=Cursor(
            connector_id="datacite", query_fingerprint=compiled.query_fingerprint, position="1"
        ),
        cursor_after=Cursor(
            connector_id="datacite", query_fingerprint=compiled.query_fingerprint, position="2"
        ),
    )
    return await DiscoveryHandler(harness.context)._store_page(
        command=StageCommand(
            stage=StageName.DISCOVERY,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            inputs={},
        ),
        run=run,
        scope=scope,
        connector_id="datacite",
        connector_version="1.0.0",
        compiled=compiled,
        page=page,
    )


async def _requests(harness, actor, run) -> list[Any]:
    async with harness.context.uow_factory() as uow:
        return await uow.observations.list_requests(actor.workspace_id, run.id)


class TestTheSamePageInALaterRun:
    async def test_a_changed_answer_is_kept_for_the_new_run(self, lab) -> None:
        harness, actor, scope = lab
        first = await _run(harness, actor, scope)
        await _store(harness, actor, scope, first, body(2120))

        second = await _run(harness, actor, scope)
        await _store(harness, actor, scope, second, body(2127))  # the source changed
        [old] = await _requests(harness, actor, first)
        [new] = await _requests(harness, actor, second)
        assert old.request_fingerprint != new.request_fingerprint
        assert new.declared_total is None or new.record_count == 1

        # A retry of the second run that gets yet other bytes keeps its first answer.
        assert (await _store(harness, actor, scope, second, body(2130)))[0] == 1
        assert len(await _requests(harness, actor, second)) == 1

    async def test_an_identical_answer_is_still_shared(self, lab) -> None:
        harness, actor, scope = lab
        first = await _run(harness, actor, scope)
        await _store(harness, actor, scope, first, body(2120))
        second = await _run(harness, actor, scope)
        assert (await _store(harness, actor, scope, second, body(2120)))[0] == 1
        assert await _requests(harness, actor, second) == []

    async def test_a_retry_in_the_same_run_with_other_bytes_keeps_the_first(self, lab) -> None:
        harness, actor, scope = lab
        run = await _run(harness, actor, scope)
        await _store(harness, actor, scope, run, body(2120))
        await _store(harness, actor, scope, run, body(2121))
        assert len(await _requests(harness, actor, run)) == 1


class TestRetryingOneSource:
    async def task(self, harness, actor, run, state: TaskState) -> Task:
        context = harness.context
        task = Task(
            id=context.new_id(),
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            stage=StageName.DISCOVERY,
            state=state,
            input_fingerprint=str(context.new_id()),
            inputs={"connector_id": "datacite", "discovery_run_id": str(run.id)},
            run_id=run.id,
            attempt=3,
            created_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.tasks.add(task)
            await uow.commit()
        return task

    async def test_a_degraded_source_runs_again_and_the_run_is_running(self, lab) -> None:
        harness, actor, scope = lab
        run = await _run(harness, actor, scope, status=DiscoveryRunStatus.DEGRADED)
        task = await self.task(harness, actor, run, TaskState.DEGRADED)
        again = await DiscoveryService(harness.context).retry_source(
            actor, run_id=run.id, connector_id="datacite"
        )
        assert again.id == task.id
        async with harness.context.uow_factory() as uow:
            stored = await uow.tasks.get(actor.workspace_id, task.id)
            reopened = await uow.observations.get_run(actor.workspace_id, run.id)
        assert stored.state is TaskState.READY and stored.attempt == 0
        assert reopened.status is DiscoveryRunStatus.RUNNING
        [state] = reopened.connector_states
        assert state.outcome is None and state.degraded_reason is None

    async def test_a_frozen_run_or_a_running_source_is_refused(self, lab) -> None:
        harness, actor, scope = lab
        service = DiscoveryService(harness.context)
        frozen = await _run(harness, actor, scope, status=DiscoveryRunStatus.FROZEN)
        await self.task(harness, actor, frozen, TaskState.DEGRADED)
        with pytest.raises(StateTransitionError):
            await service.retry_source(actor, run_id=frozen.id, connector_id="datacite")
        running = await _run(harness, actor, scope)
        await self.task(harness, actor, running, TaskState.LEASED)
        with pytest.raises(StateTransitionError):
            await service.retry_source(actor, run_id=running.id, connector_id="datacite")
