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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from lab_assistant.application.context import ServiceContext
from lab_assistant.contracts.canonical import sha256_text
from lab_assistant.contracts.connector import RatePolicy, SiteGate
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.domain.tasks import RateBudget

__all__ = [
    "SITE_PREFIX",
    "RateLimiter",
    "SitePacer",
    "budget_key",
    "host_of",
    "login_budget_key",
    "site_key",
]

#: Longer than this and the task waits in the queue instead of holding a lease. A
#: worker sleeping for minutes is a worker not draining any other stage.
_MAX_INLINE_WAIT_SECONDS = 5.0

#: Guard against a bucket that never refills because of a clock or config error.
_MAX_INLINE_ATTEMPTS = 10


def budget_key(*, connector_id: str, host: str | None) -> str:
    """One budget per connector and host: the pair the source rate-limits on."""
    return f"{connector_id}|{host or 'default'}"


def login_budget_key(value: str, *, kind: str = "login") -> str:
    """A login bucket keyed by digest, never by the address itself.

    Rate limiting is not a reason to write plaintext emails or client addresses into
    a shared table that operators read during incidents.
    """
    return f"{kind}|{sha256_text(value)[:16]}"


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
        reclaimed_in: float | None = None
        for _ in range(_MAX_INLINE_ATTEMPTS):
            granted, wait, reclaimed_in = await self._try_acquire(key, policy=policy)
            if granted:
                return
            if wait > _MAX_INLINE_WAIT_SECONDS:
                raise LabAssistantError(
                    f"rate budget for {key} is exhausted for {wait:.0f}s",
                    context={"retry_after_seconds": int(wait) + 1, "budget_key": key},
                ).with_class(ErrorClass.RATE_LIMITED)
            await asyncio.sleep(wait)
        context: dict[str, object] = {"budget_key": key}
        if reclaimed_in is not None:
            # Every slot is held. Come back no later than the moment held slots
            # count as abandoned, rather than spending the task's attempts on
            # short backoffs that all meet the same full count.
            context["retry_after_seconds"] = int(reclaimed_in) + 1
        raise LabAssistantError(
            f"rate budget for {key} did not refill after {_MAX_INLINE_ATTEMPTS} attempts",
            context=context,
        ).with_class(ErrorClass.RATE_LIMITED)

    async def release(self, key: str) -> None:
        async with self.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(key, for_update=True)
            if budget is None:  # pragma: no cover - release without acquire
                return
            now = self._now()
            budget.release(now=now)
            budget.updated_at = now
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

    async def request[T](
        self,
        call: Callable[[], Awaitable[T]],
        *,
        key: str,
        policy: RatePolicy,
    ) -> T:
        """Spend one budgeted request on a single external call.

        The single-call sibling of ``throttle``. Acquisition fetches one document per
        attempt rather than a stream of pages, and it hits the same hosts under the
        same limits — a budget that only discovery respects is a budget the source
        does not experience.
        """
        await self.acquire(key, policy=policy)
        try:
            return await call()
        except LabAssistantError as exc:
            if exc.error_class is ErrorClass.RATE_LIMITED:
                seconds = int((exc.context or {}).get("retry_after_seconds") or 30)
                await self.note_retry_after(key, seconds=seconds)
            raise
        finally:
            await self.release(key)

    async def per_site[T](self, call: Callable[[SiteGate], Awaitable[T]]) -> T:
        """A call to a connector that reaches many sites, each paced on its own.

        The connector is handed a gate and asks it before every request to a site
        (see ``SitePacer``), so the budget it spends is the site's, not one shared
        by every site it links to.
        """
        return await call(SitePacer(self.context))

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

    async def _try_acquire(
        self, key: str, *, policy: RatePolicy
    ) -> tuple[bool, float, float | None]:
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
            reclaimed_in = None if granted else budget.slots_reclaimed_in(now=now)
            await uow.tasks.upsert_rate_budget(budget)
            await uow.commit()
        return granted, wait, reclaimed_in

    def _now(self) -> datetime:
        # The injected clock, so a test can hold time still and assert the exact
        # wait a budget asks for.
        return self.context.now()


# ── per-site pacing ────────────────────────────────────────────────────────
# Pacing each site full text is fetched from, and resting a site that refuses.
#
# Fetching full text used to share one budget per connector: every PDF the
# open-access resolver followed to MDPI, Frontiers, Elsevier and the rest counted
# against the resolver's five requests a second, as one stream. A publisher with
# thousands of papers was asked as fast as the resolver allowed, answered with rate
# limits and bot walls, and each of those answers cost the paper one of its tries.
#
# Now every site has its own stored budget (``site|<host>``, one request at a time,
# ``acquisition_site_requests_per_second``), shared by every worker:
#
# * a site whose turn is a short wait away is waited for;
# * a site whose turn is far off, or that is resting, is not asked: the fetch is
#   **deferred** (``rate_limited`` with ``deferred``), which costs the paper nothing,
#   and the worker moves on to papers on other sites. That is the round robin: a
#   busy or resting site's papers wait while the rest are fetched;
# * a site that pushes back (429, or a bot wall's 403 or 503) gets a strike and
#   rests: ``acquisition_site_rest_seconds``, doubling with each strike in a row up
#   to ``acquisition_site_rest_cap_seconds``, never shorter than its Retry-After;
# * a success clears its strikes;
# * at ``acquisition_site_block_strikes`` in a row the site is taken to block
#   automated downloads while it rests: its papers go to a person instead of
#   waiting on it.

SITE_PREFIX = "site|"


def site_key(host: str) -> str:
    return f"{SITE_PREFIX}{host.lower()}"


@dataclass(slots=True)
class SitePacer:
    """The stored, shared implementation of ``SiteGate``."""

    context: ServiceContext

    def _policy(self) -> RatePolicy:
        return RatePolicy(
            requests_per_second=self.context.settings.acquisition_site_requests_per_second,
            burst=1,
            max_concurrent=1,
        )

    async def enter(self, host: str, *, policy: RatePolicy | None = None) -> None:
        settings = self.context.settings
        key = site_key(host)
        async with self.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(key)
        if budget is not None:
            resting = budget.resting_for(now=self.context.now())
            if resting > 0 and budget.strikes >= settings.acquisition_site_block_strikes:
                raise LabAssistantError(
                    f"{host} refused automated downloads {budget.strikes} times in a row",
                    context={"host": host, "site_blocked": True},
                ).with_class(ErrorClass.POLICY_BLOCKED)
        try:
            await RateLimiter(self.context).acquire(key, policy=policy or self._policy())
        except LabAssistantError as exc:
            if exc.error_class is not ErrorClass.RATE_LIMITED:
                raise
            raise LabAssistantError(
                f"{host} is paced or resting: not asked now",
                context={**exc.context, "host": host, "deferred": True},
            ).with_class(ErrorClass.RATE_LIMITED) from exc

    async def leave(
        self,
        host: str,
        *,
        pushed_back: bool = False,
        succeeded: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        settings = self.context.settings
        now = self.context.now()
        async with self.context.uow_factory() as uow:
            budget = await uow.tasks.get_rate_budget(site_key(host), for_update=True)
            if budget is None:  # pragma: no cover - leave without enter
                return
            budget.release(now=now)
            if pushed_back:
                budget.push_back(
                    now=now,
                    retry_after=retry_after_seconds,
                    base_seconds=settings.acquisition_site_rest_seconds,
                    cap_seconds=settings.acquisition_site_rest_cap_seconds,
                )
            elif succeeded:
                budget.clear_strikes()
            budget.updated_at = max(budget.updated_at, now)
            await uow.tasks.upsert_rate_budget(budget)
            await uow.commit()
