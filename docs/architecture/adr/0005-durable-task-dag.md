Status: accepted
Owner: technical owner + operator
Last verified: 2026-08-12
Applies to: orchestration

# ADR-0005: Durable PostgreSQL task DAG with leases

## Context

The predecessor had two job models, in-memory rate state, and phase names that
overlapped between subsystems, so recovery was guesswork and a killed worker could
lose work or duplicate it.

## Decision

A durable task row records stage, state, input fingerprint, lease owner, heartbeat,
attempt, dependencies, eligibility time, and result. Claiming uses
`FOR UPDATE SKIP LOCKED`. Retries are bounded per error class. Rate budgets are rows,
not process memory.

States are explicit and separate: `pending`, `ready`, `leased`, `blocked`,
`awaiting_user`, `degraded`, `quarantined`, `succeeded`, `failed`, `dead_letter`,
`cancelled`.

## Consequences

- A killed worker's task becomes claimable when the lease expires, and the retry is
  idempotent because the fingerprint is stable.
- **Waiting for a human is a state, not a failure**: `awaiting_user` holds no lease
  and does not count toward failure metrics.
- Progress is read from rows; completion is never inferred from a missing heartbeat.
- Duplicate delivery is one logical result: a completed task returns its stored
  result rather than re-running.

## Alternatives considered

- **A broker.** Rejected for now: another dependency, and durable rows give
  inspection and human-wait semantics a queue does not.
- **A workflow engine.** Deferred until measured lock contention or month-long
  timers justify it.

## Validation / revisit trigger

`tests/integration/test_task_engine.py` covers idempotency, lease expiry, caps, dead
letters, and cancellation. Revisit on sustained lock contention above SLO.

## References

- `packages/python/lab_assistant/domain/tasks.py`
- `packages/python/lab_assistant/application/tasks.py`
