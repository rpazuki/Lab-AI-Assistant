"""Distributed connector rate limiting.

Every external request passes through a stored token bucket keyed by connector and
host. Stored, not in-process, because the predecessor's in-memory limiter forced a
single-worker topology: scaling out doubled the request rate at the source, and the
first sign of it was an email from the provider.

The limiter is a decorator around a connector's page stream. Acquiring before each
page — rather than once per run — is what makes it a limit: a run is many requests,
and it is the requests the source counts.

Two outcomes when the budget is empty:

* a short wait: sleep and retry, because a two-second pause is cheaper than a task
  round trip;
* a long wait (a stored ``Retry-After``, usually): raise ``rate_limited`` so the
  task retries later with its **cursor preserved**. That distinction is why a
  rate-limited discovery run resumes rather than restarting.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from lab_assistant.application.context import ServiceContext
from lab_assistant.contracts.connector import RatePolicy
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.domain.tasks import RateBudget

__all__ = ["RateLimiter", "budget_key", "host_of"]

#: Longer than this and the task waits in the queue instead of holding a lease. A
#: worker sleeping for minutes is a worker not draining any other stage.
_MAX_INLINE_WAIT_SECONDS = 5.0

#: Guard against a bucket that never refills because of a clock or config error.
_MAX_INLINE_ATTEMPTS = 10


def budget_key(*, connector_id: str, host: str | None) -> str:
    """One budget per connector and host: the pair the source rate-limits on."""
    return f"{connector_id}|{host or 'default'}"


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc or None


@dataclass(slots=True)
class RateLimiter:
    """Acquire and release stored request slots around external calls."""

    context: ServiceContext

    async def acquire(self, key: str, *, policy: RatePolicy) -> None:
        """Take one request slot, waiting briefly or deferring the task."""
        for _ in range(_MAX_INLINE_ATTEMPTS):
            granted, wait = await self._try_acquire(key, policy=policy)
            if granted:
                return
            if wait > _MAX_INLINE_WAIT_SECONDS:
                raise LabAssistantError(
                    f"rate budget for {key} is exhausted for {wait:.0f}s",
                    context={"retry_after_seconds": int(wait) + 1, "budget_key": key},
                ).with_class(ErrorClass.RATE_LIMITED)
            await asyncio.sleep(wait)
        raise LabAssistantError(
            f"rate budget for {key} did not refill after {_MAX_INLINE_ATTEMPTS} attempts",
            context={"budget_key": key},
        ).with_class(ErrorClass.RATE_LIMITED)

    async def release(self, key: str) -> None:
        async with self.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(key, for_update=True)
            if budget is None:  # pragma: no cover - release without acquire
                return
            budget.release()
            budget.updated_at = self._now()
            await uow.tasks.upsert_rate_budget(budget)
            await uow.commit()

    async def note_retry_after(self, key: str, *, seconds: int) -> None:
        """Honour the source's own instruction, for every worker at once."""
        async with self.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(key, for_update=True)
            if budget is None:
                return
            budget.apply_retry_after(now=self._now(), seconds=seconds)
            await uow.tasks.upsert_rate_budget(budget)
            await uow.commit()

    async def throttle[T](
        self,
        pages: AsyncIterator[T],
        *,
        key: str,
        policy: RatePolicy,
    ) -> AsyncIterator[T]:
        """Wrap a page stream so each page costs one budgeted request."""
        iterator = pages.__aiter__()
        while True:
            await self.acquire(key, policy=policy)
            try:
                page = await iterator.__anext__()
            except StopAsyncIteration:
                await self.release(key)
                return
            except LabAssistantError as exc:
                await self.release(key)
                if exc.error_class is ErrorClass.RATE_LIMITED:
                    seconds = int((exc.context or {}).get("retry_after_seconds") or 30)
                    await self.note_retry_after(key, seconds=seconds)
                raise
            except BaseException:
                await self.release(key)
                raise
            await self.release(key)
            yield page

    # ── internals ──────────────────────────────────────────────────────────

    async def _try_acquire(self, key: str, *, policy: RatePolicy) -> tuple[bool, float]:
        now = self._now()
        async with self.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(key, for_update=True)
            if budget is None:
                budget = RateBudget(
                    key=key,
                    tokens=float(policy.burst or 1),
                    refill_per_second=float(policy.requests_per_second),
                    burst=float(policy.burst or 1),
                    max_concurrent=int(policy.max_concurrent or 1),
                    updated_at=now,
                )
            else:
                # The policy is versioned config; a change takes effect on the next
                # acquisition rather than requiring the row to be deleted.
                budget.refill_per_second = float(policy.requests_per_second)
                budget.burst = float(policy.burst or 1)
                budget.max_concurrent = int(policy.max_concurrent or 1)
            granted, wait = budget.try_acquire(now=now)
            await uow.tasks.upsert_rate_budget(budget)
            await uow.commit()
        return granted, wait

    def _now(self) -> datetime:
        # The injected clock, so a test can hold time still and assert the exact
        # wait a budget asks for.
        return self.context.now()
