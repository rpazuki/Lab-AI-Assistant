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
    async def test_two_limiters_on_one_context_share_the_allowance(
        self, limiter_harness
    ) -> None:
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

    async def test_a_concurrency_cap_blocks_a_second_request(
        self, limiter_harness
    ) -> None:
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
            async for _page in limiter.throttle(
                rate_limited_source(), key=KEY, policy=policy
            ):
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
            inclusion_criteria=("engineered yeast lipid production",),
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
