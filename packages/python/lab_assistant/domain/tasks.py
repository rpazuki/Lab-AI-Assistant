"""The durable task DAG.

Run state lives in a row, never only in process memory. That is what lets a
worker be killed mid-stage and another worker pick the work up after the lease
expires, and it is why rate-limit state is shared rather than per-process.

Waiting for a human is a state (`awaiting_user`), not a failure and not a lease.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.errors import ErrorClass, RetryEligibility, retry_eligibility
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.states import TaskState

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "SLOT_LEASE_SECONDS",
    "RateBudget",
    "Task",
    "backoff_seconds",
    "next_state_for_error",
]

DEFAULT_MAX_ATTEMPTS = 5

#: How long a request may hold a concurrency slot. Four times the connectors' HTTP
#: timeout: a slot unchanged for longer belongs to a request that will never return
#: it, usually because its worker was stopped mid-request by a redeploy.
SLOT_LEASE_SECONDS = 120


def backoff_seconds(attempt: int, *, base: int = 2, cap: int = 900) -> int:
    """Deterministic exponential backoff.

    No jitter: a deterministic schedule is testable, and at this scale the
    thundering-herd risk is negligible compared with the debugging cost of a
    nondeterministic retry time.
    """
    return min(cap, base ** max(1, attempt))


def next_state_for_error(error_class: ErrorClass, *, attempt: int, max_attempts: int) -> TaskState:
    eligibility = retry_eligibility(error_class)
    if eligibility is RetryEligibility.HUMAN_ACTION:
        return TaskState.AWAITING_USER
    if error_class is ErrorClass.MALWARE_DETECTED:
        return TaskState.QUARANTINED
    if error_class is ErrorClass.CANCELLED:
        return TaskState.CANCELLED
    if eligibility is RetryEligibility.PERMANENT:
        return TaskState.FAILED
    if attempt >= max_attempts:
        return TaskState.DEAD_LETTER
    return TaskState.READY


class Task(BaseModel):
    """One unit of durable work."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    #: Who asked for this work. Durable, because a worker's audit rows are useless
    #: if they attribute a steward's decision to the workspace itself — and the
    #: workspace uuid is what a fallback produces.
    actor_id: UUID | None = None
    stage: StageName
    state: TaskState = TaskState.PENDING
    #: Digest of the stage command; two identical fingerprints are one logical task.
    input_fingerprint: str
    idempotency_key: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    run_id: UUID | None = None
    depends_on: tuple[UUID, ...] = ()
    priority: int = 100
    attempt: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    not_before: datetime | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_class: ErrorClass | None = None
    error_detail: str | None = None
    human_action_required: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    #: Set once a successful result exists, so a duplicate delivery is a no-op.
    result_fingerprint: str | None = None
    worker_version: str | None = None
    version: int = 1

    @property
    def worker_class(self) -> str:
        return self.stage.worker_class

    def is_claimable(self, now: datetime) -> bool:
        if self.state is TaskState.READY:
            return self.not_before is None or now >= self.not_before
        if self.state is TaskState.LEASED:
            # A dead worker's lease expires and the task becomes claimable again.
            return self.lease_expires_at is not None and now >= self.lease_expires_at
        return False

    def claim(self, *, owner: str, now: datetime, lease_seconds: int = 300) -> None:
        self.state = TaskState.LEASED
        self.lease_owner = owner
        self.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self.heartbeat_at = now
        self.attempt += 1
        self.started_at = self.started_at or now
        self.version += 1

    def heartbeat(self, *, now: datetime, lease_seconds: int = 300) -> None:
        self.heartbeat_at = now
        self.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self.version += 1

    def succeed(self, *, now: datetime, result: dict[str, Any], fingerprint: str) -> None:
        self.state = TaskState.SUCCEEDED
        self.completed_at = now
        self.result = result
        self.result_fingerprint = fingerprint
        self.lease_owner = None
        self.lease_expires_at = None
        self.error_class = None
        self.error_detail = None
        self.version += 1

    def fail(
        self,
        *,
        now: datetime,
        error_class: ErrorClass,
        detail: str,
        retry_after_seconds: int | None = None,
        human_action: str | None = None,
    ) -> TaskState:
        self.error_class = error_class
        self.error_detail = detail
        self.lease_owner = None
        self.lease_expires_at = None
        state = next_state_for_error(
            error_class, attempt=self.attempt, max_attempts=self.max_attempts
        )
        self.state = state
        if state is TaskState.READY:
            delay = retry_after_seconds or backoff_seconds(self.attempt)
            self.not_before = now + timedelta(seconds=delay)
        if state is TaskState.AWAITING_USER:
            self.human_action_required = human_action or error_class.value
        if state.is_terminal:
            self.completed_at = now
        self.version += 1
        return state

    def defer(self, *, now: datetime, seconds: int) -> None:
        """Back in the queue after ``seconds``, the attempt it was claimed for given back.

        For work that was not tried at all (the site it needs is not free yet):
        waiting on a busy site is not a failure, and must not use up the retries
        a real failure needs.
        """
        self.state = TaskState.READY
        self.attempt = max(0, self.attempt - 1)
        self.not_before = now + timedelta(seconds=max(1, seconds))
        self.lease_owner = None
        self.lease_expires_at = None
        self.version += 1

    def block_on(self, dependency_ids: tuple[UUID, ...]) -> None:
        self.depends_on = dependency_ids
        self.state = TaskState.BLOCKED
        self.version += 1

    def release_dependency_block(self) -> None:
        self.state = TaskState.READY
        self.version += 1

    def cancel(self, *, now: datetime) -> None:
        if self.state.is_terminal:
            return
        self.state = TaskState.CANCELLED
        self.completed_at = now
        self.lease_owner = None
        self.lease_expires_at = None
        self.version += 1


class RateBudget(BaseModel):
    """Shared request allowance for one connector credential and host.

    Stored, not in-process: the legacy system's in-memory rate state forced a
    single worker topology and made one long provider call starve another queue.
    """

    model_config = {"validate_assignment": True}

    key: str  # "<connector_id>|<host>"
    tokens: float
    refill_per_second: float
    burst: float
    max_concurrent: int = 1
    in_flight: int = 0
    updated_at: datetime
    next_permitted_at: datetime | None = None
    #: When a slot was last taken or returned. `in_flight` is a count, not a set of
    #: leases, so this is what lets a slot a dead worker held be reclaimed.
    slots_changed_at: datetime | None = None
    #: Push-backs in a row (a rate limit, a bot wall); each rests the site longer.
    strikes: int = 0

    def try_acquire(self, *, now: datetime) -> tuple[bool, float]:
        """Attempt to take one request slot.

        Returns ``(granted, wait_seconds)``. ``wait_seconds`` is what the caller
        should sleep before retrying, honouring any stored ``Retry-After``.
        """
        if self.next_permitted_at is not None and now < self.next_permitted_at:
            return False, (self.next_permitted_at - now).total_seconds()
        elapsed = max(0.0, (now - self.updated_at).total_seconds())
        self.tokens = min(self.burst, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now
        self._reclaim_abandoned_slots(now=now)
        if self.in_flight >= self.max_concurrent:
            return False, 1.0 / max(self.refill_per_second, 0.001)
        if self.tokens < 1.0:
            deficit = 1.0 - self.tokens
            return False, deficit / max(self.refill_per_second, 0.001)
        self.tokens -= 1.0
        self.in_flight += 1
        self.slots_changed_at = now
        return True, 0.0

    def release(self, *, now: datetime) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self.slots_changed_at = now

    def slots_reclaimed_in(self, *, now: datetime) -> float | None:
        """Seconds until a full set of slots counts as abandoned; None if not full."""
        if self.in_flight < self.max_concurrent:
            return None
        if self.slots_changed_at is None:
            return 0.0
        held = (now - self.slots_changed_at).total_seconds()
        return max(0.0, SLOT_LEASE_SECONDS - held)

    def _reclaim_abandoned_slots(self, *, now: datetime) -> None:
        """Return slots no request has touched for a whole lease.

        A worker stopped between acquire and release (a redeploy, an OOM kill) never
        returns its slot. Without this, two such stops fill a two-slot connector for
        good: every later request waits on slots nobody holds, and the task
        dead-letters with "did not refill". While any request still takes or returns
        a slot the count is trusted; once none has for a lease, nothing is in flight.
        A row with slots but no change time predates this field and is reclaimed.
        """
        if self.in_flight == 0:
            return
        if (
            self.slots_changed_at is None
            or (now - self.slots_changed_at).total_seconds() >= SLOT_LEASE_SECONDS
        ):
            self.in_flight = 0

    def apply_retry_after(self, *, now: datetime, seconds: int) -> None:
        self.next_permitted_at = now + timedelta(seconds=seconds)

    def push_back(
        self, *, now: datetime, retry_after: int | None, base_seconds: int, cap_seconds: int
    ) -> int:
        """The site refused: rest it, longer each time in a row. Returns the rest in seconds.

        The rest doubles with each strike (``base``, ``2·base``, ``4·base`` …) up to
        ``cap``, and is never shorter than the site's own ``Retry-After``.
        """
        self.strikes += 1
        rest = min(cap_seconds, base_seconds * 2 ** (self.strikes - 1))
        rest = max(rest, retry_after or 0)
        self.next_permitted_at = now + timedelta(seconds=rest)
        return rest

    def clear_strikes(self) -> None:
        self.strikes = 0

    def resting_for(self, *, now: datetime) -> float:
        """Seconds until the site may be asked again; 0 when it may be now."""
        if self.next_permitted_at is None or now >= self.next_permitted_at:
            return 0.0
        return (self.next_permitted_at - now).total_seconds()
