"""The connector rate budget, enforced rather than declared.

`RateBudget` existed, connectors declared `rate_policy`, and live discovery called
`connector.discover(request)` directly — so the limiter was architectural state, not
a limit. Scaling to two workers doubled the request rate at the source, which is the
failure the stored budget was designed to prevent.

Everything here is offline: the "requests" are pages from a fixture connector, and
what is asserted is the accounting around them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from lab_assistant.application.rate_limit import RateLimiter, budget_key, host_of
from lab_assistant.contracts.connector import RatePolicy
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.testing import build_harness

KEY = budget_key(connector_id="pubmed", host="eutils.ncbi.nlm.nih.gov")


@pytest.fixture
def limiter_harness():
    """A bare harness: these tests exercise the accounting, not a connector."""
    return build_harness()


async def _pages(count: int) -> AsyncIterator[int]:
    for index in range(count):
        yield index


@pytest.fixture
async def provisioned_two_page(jats_bytes):
    """A two-page discovery run whose budget allows exactly one page.

    Page one is fetched and stored; the second acquisition finds an empty bucket with
    a long wait and defers the task. That is the case where "the cursor is preserved"
    and "the task retries" have to both be true.
    """
    from lab_assistant.application.discovery import DiscoveryService, cursor_lineage_key
    from lab_assistant.application.scopes import ScopeService
    from lab_assistant.domain.scope import ConnectorPlanEntry
    from lab_assistant.domain.tasks import RateBudget
    from lab_assistant.domain.workspace import Role
    from lab_assistant.testing import build_harness, fake_source, make_actor, record, source_page

    query = "two page query"
    first = source_page(
        query=query,
        records=[
            record(
                locator="doi:10.1234/page-one",
                identifiers=[("doi", "10.1234/page-one")],
                title="Page one work",
            )
        ],
        cursor_out="page-2",
        declared_total=2,
    )
    second = source_page(
        query=query,
        records=[
            record(
                locator="doi:10.1234/page-two",
                identifiers=[("doi", "10.1234/page-two")],
                title="Page two work",
            )
        ],
        cursor_in="page-2",
        declared_total=2,
    )
    harness = build_harness(
        connectors={"pubmed-fixture": fake_source("pubmed-fixture", [first, second])}
    )
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    actor = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)

    key = budget_key(connector_id="pubmed-fixture", host=None)
    async with harness.context.uow_factory() as uow:
        await uow.tasks.upsert_rate_budget(
            RateBudget(
                key=key,
                # One token, and a refill so slow the second page cannot wait for it
                # inline: exactly the shape of a source with a small burst allowance.
                tokens=1.0,
                refill_per_second=0.001,
                burst=1.0,
                max_concurrent=1,
                updated_at=harness.context.now(),
            )
        )
        await uow.commit()

    scopes = ScopeService(harness.context)
    scope = await scopes.create_version(
        actor,
        scope_id=None,
        name="Two page scope",
        scientific_objective="Paging behaviour under a rate limit.",
        connector_plan=[
            ConnectorPlanEntry(
                connector_id="pubmed-fixture", connector_version="1.0.0", query=query
            )
        ],
        intended_uses=("internal literature search",),
        concepts=("paging",),
    )
    approved = await scopes.approve(actor, scope_version_id=scope.id)
    run, requests = await DiscoveryService(harness.context).start_run(
        actor, scope_version_id=approved.id
    )
    tasks = await harness.orchestrator.submit_requests(
        requests, workspace_id=workspace.id, actor_id=actor.user_id, run_id=run.id
    )
    connector = harness.connectors.get("pubmed-fixture")
    compiled = await connector.compile_query({"query": query})
    lineage = cursor_lineage_key(
        workspace_id=workspace.id,
        scope_version_id=approved.id,
        connector_id="pubmed-fixture",
        connector_version="1.0.0",
        query_fingerprint=compiled.query_fingerprint,
        cursor_semantics_version="1",
    )
    return {
        "harness": harness,
        "workspace": workspace,
        "run_id": run.id,
        "task": next(task for task in tasks if task.stage.value == "discovery"),
        "lineage": lineage,
    }


class TestBudgetKey:
    def test_the_key_is_connector_and_host(self) -> None:
        assert budget_key(connector_id="crossref", host="api.crossref.org") == (
            "crossref|api.crossref.org"
        )

    def test_a_missing_host_still_produces_a_key(self) -> None:
        assert budget_key(connector_id="crossref", host=None) == "crossref|default"

    def test_the_host_comes_from_the_base_url(self) -> None:
        assert host_of("https://api.crossref.org/works") == "api.crossref.org"
        assert host_of(None) is None


class TestSharedBudget:
    async def test_two_limiters_on_one_context_share_the_allowance(self, limiter_harness) -> None:
        """Two workers, one budget: this is the whole reason it is stored."""
        policy = RatePolicy(requests_per_second=1.0, burst=2, max_concurrent=4)
        worker_one = RateLimiter(limiter_harness.context)
        worker_two = RateLimiter(limiter_harness.context)

        await worker_one.acquire(KEY, policy=policy)
        await worker_one.release(KEY)
        await worker_two.acquire(KEY, policy=policy)
        await worker_two.release(KEY)

        # The clock is fixed, so nothing refilled: the burst of two is spent and the
        # third request must wait rather than proceed.
        with pytest.raises(LabAssistantError) as raised:
            await worker_two.acquire(KEY, policy=policy)
        assert raised.value.error_class is ErrorClass.RATE_LIMITED

    async def test_a_refill_restores_the_allowance(self, limiter_harness) -> None:
        policy = RatePolicy(requests_per_second=1.0, burst=1, max_concurrent=1)
        limiter = RateLimiter(limiter_harness.context)
        await limiter.acquire(KEY, policy=policy)
        await limiter.release(KEY)
        limiter_harness.clock.advance(5)
        await limiter.acquire(KEY, policy=policy)
        await limiter.release(KEY)

    async def test_a_concurrency_cap_blocks_a_second_request(self, limiter_harness) -> None:
        policy = RatePolicy(requests_per_second=10.0, burst=10, max_concurrent=1)
        limiter = RateLimiter(limiter_harness.context)
        await limiter.acquire(KEY, policy=policy)
        with pytest.raises(LabAssistantError) as raised:
            # Not released: a second concurrent request is refused even though
            # tokens remain, because the source caps concurrency separately.
            await limiter.acquire(KEY, policy=policy)
        assert raised.value.error_class is ErrorClass.RATE_LIMITED
        await limiter.release(KEY)

    async def test_a_policy_change_takes_effect_without_deleting_the_row(
        self, limiter_harness
    ) -> None:
        limiter = RateLimiter(limiter_harness.context)
        await limiter.acquire(KEY, policy=RatePolicy(requests_per_second=1.0, burst=1))
        await limiter.release(KEY)
        async with limiter_harness.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(KEY)
        assert budget is not None and budget.burst == 1

        limiter_harness.clock.advance(60)
        await limiter.acquire(KEY, policy=RatePolicy(requests_per_second=9.0, burst=9))
        await limiter.release(KEY)
        async with limiter_harness.context.uow_factory() as uow:
            updated = await uow.tasks.get_rate_budget(KEY)
        assert updated is not None
        assert updated.burst == 9
        assert updated.refill_per_second == 9.0


class TestThrottledPages:
    async def test_every_page_costs_one_request(self, limiter_harness) -> None:
        limiter = RateLimiter(limiter_harness.context)
        policy = RatePolicy(requests_per_second=100.0, burst=100, max_concurrent=2)
        seen = [page async for page in limiter.throttle(_pages(4), key=KEY, policy=policy)]
        assert seen == [0, 1, 2, 3]
        async with limiter_harness.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(KEY)
        assert budget is not None
        # Four pages plus the acquisition that met StopAsyncIteration.
        assert budget.tokens == pytest.approx(100 - 5)
        assert budget.in_flight == 0

    async def test_an_exhausted_budget_defers_the_task_and_keeps_the_pages_it_had(
        self, limiter_harness
    ) -> None:
        """A rate limit is retryable, and the cursor state survives it."""
        limiter = RateLimiter(limiter_harness.context)
        policy = RatePolicy(requests_per_second=0.01, burst=2, max_concurrent=1)
        collected: list[int] = []
        with pytest.raises(LabAssistantError) as raised:
            async for page in limiter.throttle(_pages(10), key=KEY, policy=policy):
                collected.append(page)
        assert raised.value.error_class is ErrorClass.RATE_LIMITED
        assert raised.value.context["retry_after_seconds"] >= 1
        # The burst allowed two pages; the third deferred the task. What matters is
        # that the pages already fetched are not lost — the caller keeps its cursor
        # and the task resumes rather than restarting the run.
        assert collected == [0, 1]

    async def test_an_upstream_retry_after_is_recorded_for_every_worker(
        self, limiter_harness
    ) -> None:
        limiter = RateLimiter(limiter_harness.context)
        policy = RatePolicy(requests_per_second=10.0, burst=10, max_concurrent=2)

        async def rate_limited_source() -> AsyncIterator[int]:
            yield 0
            raise LabAssistantError(
                "upstream rate limited the request", context={"retry_after_seconds": 120}
            ).with_class(ErrorClass.RATE_LIMITED)

        with pytest.raises(LabAssistantError):
            async for _page in limiter.throttle(rate_limited_source(), key=KEY, policy=policy):
                pass

        async with limiter_harness.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(KEY)
        assert budget is not None and budget.next_permitted_at is not None
        assert budget.in_flight == 0

        # A different worker is held off too — that is what "distributed" means.
        other = RateLimiter(limiter_harness.context)
        with pytest.raises(LabAssistantError) as raised:
            await other.acquire(KEY, policy=policy)
        assert raised.value.context["retry_after_seconds"] >= 100

    async def test_a_non_rate_error_releases_the_slot(self, limiter_harness) -> None:
        limiter = RateLimiter(limiter_harness.context)
        policy = RatePolicy(requests_per_second=10.0, burst=10, max_concurrent=1)

        async def failing() -> AsyncIterator[int]:
            yield 0
            raise LabAssistantError("upstream is unavailable").with_class(
                ErrorClass.SOURCE_UNAVAILABLE
            )

        with pytest.raises(LabAssistantError):
            async for _page in limiter.throttle(failing(), key=KEY, policy=policy):
                pass
        async with limiter_harness.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(KEY)
        # A leaked in-flight count would wedge the connector for every worker.
        assert budget is not None and budget.in_flight == 0


class TestDiscoveryUsesTheLimiter:
    async def test_a_discovery_run_spends_the_budget(self, harness, provisioned) -> None:
        """The wire itself: a real discovery run leaves a budget row behind.

        Without this, every assertion above would pass while live discovery kept
        calling `connector.discover()` directly — which is exactly what the audit
        found.
        """
        from lab_assistant.application.discovery import DiscoveryService
        from lab_assistant.application.scopes import ScopeService
        from lab_assistant.domain.scope import ConnectorPlanEntry

        actor = provisioned["steward_actor"]
        workspace = provisioned["workspace"]
        scopes = ScopeService(harness.context)
        scope = await scopes.create_version(
            actor,
            scope_id=None,
            name="Yarrowia lipid production",
            scientific_objective="Lipid yields in engineered oleaginous yeast.",
            connector_plan=[
                ConnectorPlanEntry(
                    connector_id="pubmed-fixture",
                    connector_version="1.0.0",
                    query="yarrowia lipolytica lipid production",
                )
            ],
            intended_uses=("internal literature search",),
            concepts=("lipid production",),
        )
        approved = await scopes.approve(actor, scope_version_id=scope.id)
        run, tasks = await DiscoveryService(harness.context).start_run(
            actor, scope_version_id=approved.id
        )
        await harness.orchestrator.submit_requests(
            tasks, workspace_id=workspace.id, actor_id=actor.user_id, run_id=run.id
        )
        await harness.run_all_tasks()

        async with harness.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(
                budget_key(connector_id="pubmed-fixture", host=None)
            )
        assert budget is not None, "discovery did not go through the rate limiter"
        assert budget.tokens < budget.burst
        assert budget.in_flight == 0


class TestRateLimitedWorkAlwaysComesBack:
    """The half of MIT-010 the first pass missed.

    Wrapping the connector was only part of it. A rate-limited discovery task was
    returned as `StageStatus.BLOCKED`, which the orchestrator stores verbatim: no
    `not_before`, and a blocked task is not claimable. The cursor survived and the
    work never resumed, which is the worst of both — it looks like back-pressure and
    behaves like a stuck queue.
    """

    async def test_a_rate_limited_discovery_task_becomes_ready_with_a_wait(
        self, harness, provisioned
    ) -> None:
        from lab_assistant.application.discovery import DiscoveryService
        from lab_assistant.application.scopes import ScopeService
        from lab_assistant.domain.scope import ConnectorPlanEntry
        from lab_assistant.domain.states import TaskState
        from lab_assistant.domain.tasks import RateBudget

        actor = provisioned["steward_actor"]
        workspace = provisioned["workspace"]
        key = budget_key(connector_id="pubmed-fixture", host=None)

        # The shared budget says "not before two minutes from now" — the state an
        # upstream 429 leaves behind for every worker.
        async with harness.context.uow_factory() as uow:
            await uow.tasks.upsert_rate_budget(
                RateBudget(
                    key=key,
                    tokens=0.0,
                    refill_per_second=0.01,
                    burst=1.0,
                    max_concurrent=1,
                    updated_at=harness.context.now(),
                )
            )
            await uow.commit()
        limiter = RateLimiter(harness.context)
        await limiter.note_retry_after(key, seconds=120)

        scopes = ScopeService(harness.context)
        scope = await scopes.create_version(
            actor,
            scope_id=None,
            name="Yarrowia lipid production",
            scientific_objective="Lipid yields in engineered oleaginous yeast.",
            connector_plan=[
                ConnectorPlanEntry(
                    connector_id="pubmed-fixture",
                    connector_version="1.0.0",
                    query="yarrowia lipolytica lipid production",
                )
            ],
            intended_uses=("internal literature search",),
            concepts=("lipid production",),
        )
        approved = await scopes.approve(actor, scope_version_id=scope.id)
        run, requests = await DiscoveryService(harness.context).start_run(
            actor, scope_version_id=approved.id
        )
        tasks = await harness.orchestrator.submit_requests(
            requests, workspace_id=workspace.id, actor_id=actor.user_id, run_id=run.id
        )
        discovery_task = next(task for task in tasks if task.stage.value == "discovery")
        result = await harness.orchestrator.execute(discovery_task)

        async with harness.context.uow_factory() as uow:
            stored = await uow.tasks.get(workspace.id, discovery_task.id)
            stored_run = await uow.observations.get_run(workspace.id, run.id)

        assert stored is not None
        # Claimable again, after the wait the source asked for.
        assert stored.state is TaskState.READY
        assert stored.not_before is not None
        assert stored.not_before > harness.context.now()
        assert stored.error_class is ErrorClass.RATE_LIMITED
        assert result.retry_after_seconds and result.retry_after_seconds >= 100
        # And the run records the retryable outcome rather than a failure.
        assert stored_run is not None
        state = stored_run.state_for("pubmed-fixture")
        assert state is not None and state.outcome.value == "rate_limited_retryable"

    async def test_the_retry_resumes_from_the_stored_cursor(self, provisioned_two_page) -> None:
        """Page one is fetched, page two is deferred, and the cursor points at two."""
        from lab_assistant.domain.states import TaskState

        harness = provisioned_two_page["harness"]
        workspace = provisioned_two_page["workspace"]
        task = provisioned_two_page["task"]
        run_id = provisioned_two_page["run_id"]

        await harness.orchestrator.execute(task)

        async with harness.context.uow_factory() as uow:
            stored = await uow.tasks.get(workspace.id, task.id)
            requests = await uow.observations.list_requests(workspace.id, run_id)
            cursor = await uow.observations.get_cursor(
                workspace.id, lineage_key=provisioned_two_page["lineage"]
            )

        assert stored is not None
        assert stored.state is TaskState.READY, "a rate-limited run must come back"
        assert stored.not_before is not None
        # Exactly one page was fetched and stored before the budget ran out…
        assert len(requests) == 1
        # …and the cursor is the one the *next* attempt starts from, so the retry
        # resumes rather than re-fetching page one.
        assert cursor == "page-2"

    async def test_automatic_acquisition_spends_the_connector_budget(
        self, harness, provisioned
    ) -> None:
        """Acquisition used to call the connector directly, budget untouched."""
        from lab_assistant.application.acquisition import AcquisitionHandler
        from lab_assistant.contracts.connector import IdentifierScheme
        from lab_assistant.contracts.policy import PolicyAction
        from lab_assistant.contracts.stage import StageCommand, StageName
        from lab_assistant.domain.assets import (
            AcquisitionPlan,
            AcquisitionRoute,
            Manifestation,
        )
        from lab_assistant.domain.identity import Work, WorkIdentifier, WorkKind
        from lab_assistant.domain.states import RelevanceState

        actor = provisioned["steward_actor"]
        workspace = provisioned["workspace"]
        context = harness.context

        async with context.uow_factory() as uow:
            work = Work(
                id=context.new_id(),
                workspace_id=workspace.id,
                work_kind=WorkKind.ARTICLE,
                relevance=RelevanceState.INCLUDED,
                created_at=context.now(),
            )
            await uow.works.add(work)
            await uow.works.add_identifier(
                WorkIdentifier(
                    id=context.new_id(),
                    workspace_id=workspace.id,
                    work_id=work.id,
                    scheme=IdentifierScheme.DOI,
                    value_raw="10.1234/example",
                    value_normalized="10.1234/example",
                    value_display="10.1234/example",
                    is_active=True,
                    asserted_by=("pubmed-fixture",),
                )
            )
            manifestation = Manifestation(
                id=context.new_id(),
                workspace_id=workspace.id,
                work_id=work.id,
                manifestation_kind="version_of_record",
                label="version of record",
            )
            await uow.assets.add_manifestation(manifestation)
            plan = AcquisitionPlan(
                id=context.new_id(),
                workspace_id=workspace.id,
                work_id=work.id,
                manifestation_ids=(manifestation.id,),
                requested_actions=frozenset(
                    {
                        PolicyAction.STORE_CONTENT,
                        PolicyAction.PROCESS_LOCAL,
                        PolicyAction.INDEX,
                        PolicyAction.QUERY,
                    }
                ),
                route_order=(AcquisitionRoute.OPEN_ACCESS_API,),
                status="planned",
                created_at=context.now(),
            )
            await uow.assets.add_plan(plan)
            await uow.commit()

        result = await AcquisitionHandler(context).execute(
            StageCommand(
                stage=StageName.ACQUISITION,
                workspace_id=workspace.id,
                actor_id=actor.user_id,
                inputs={"plan_id": str(plan.id)},
            )
        )

        async with context.uow_factory() as uow:
            attempts = await uow.assets.list_attempts(workspace.id, plan_id=plan.id)
            budget = await uow.tasks.get_rate_budget(
                budget_key(connector_id="fake-acquire", host=None)
            )
        assert attempts, f"no acquisition was attempted ({result.metrics})"
        assert budget is not None, "acquisition bypassed the shared rate budget"
        assert budget.tokens < budget.burst
        assert budget.in_flight == 0


class TestAbandonedSlots:
    """A worker stopped mid-request never returns its slot.

    Two redeploys during a Europe PMC run left `in_flight` at 2 of 2 with nothing
    running, and every later request for that source waited on slots nobody held
    until the task dead-lettered with "did not refill".
    """

    async def test_a_slot_untouched_for_a_lease_is_reclaimed(self, limiter_harness) -> None:
        from lab_assistant.domain.tasks import SLOT_LEASE_SECONDS

        policy = RatePolicy(requests_per_second=10.0, burst=10, max_concurrent=2)
        limiter = RateLimiter(limiter_harness.context)
        # Two requests whose workers die before releasing.
        await limiter.acquire(KEY, policy=policy)
        await limiter.acquire(KEY, policy=policy)

        with pytest.raises(LabAssistantError) as raised:
            await limiter.acquire(KEY, policy=policy)
        # Deferred until the slots would count as abandoned, not for a short backoff
        # that would spend the task's attempts against the same full count.
        assert raised.value.error_class is ErrorClass.RATE_LIMITED
        assert raised.value.context["retry_after_seconds"] == SLOT_LEASE_SECONDS + 1

        limiter_harness.clock.advance(SLOT_LEASE_SECONDS)
        await limiter.acquire(KEY, policy=policy)
        async with limiter_harness.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(KEY)
        assert budget is not None and budget.in_flight == 1

    async def test_a_slot_in_use_is_not_reclaimed(self, limiter_harness) -> None:
        """Slots still being taken and returned are trusted, however old the first."""
        from lab_assistant.domain.tasks import SLOT_LEASE_SECONDS

        policy = RatePolicy(requests_per_second=10.0, burst=10, max_concurrent=2)
        limiter = RateLimiter(limiter_harness.context)
        await limiter.acquire(KEY, policy=policy)  # a long request, still running
        limiter_harness.clock.advance(SLOT_LEASE_SECONDS - 10)
        await limiter.acquire(KEY, policy=policy)
        await limiter.release(KEY)
        limiter_harness.clock.advance(20)
        await limiter.acquire(KEY, policy=policy)
        async with limiter_harness.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(KEY)
        assert budget is not None and budget.in_flight == 2

    async def test_slots_leaked_before_the_lease_existed_are_reclaimed(
        self, limiter_harness
    ) -> None:
        """The state the server's Europe PMC row is in: full, with no change time."""
        from lab_assistant.domain.tasks import RateBudget

        async with limiter_harness.context.uow_factory() as uow:
            await uow.tasks.upsert_rate_budget(
                RateBudget(
                    key=KEY,
                    tokens=5.0,
                    refill_per_second=5.0,
                    burst=5.0,
                    max_concurrent=2,
                    in_flight=2,
                    updated_at=limiter_harness.context.now(),
                )
            )
            await uow.commit()
        limiter = RateLimiter(limiter_harness.context)
        await limiter.acquire(
            KEY, policy=RatePolicy(requests_per_second=5.0, burst=5, max_concurrent=2)
        )
        async with limiter_harness.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(KEY)
        assert budget is not None and budget.in_flight == 1
