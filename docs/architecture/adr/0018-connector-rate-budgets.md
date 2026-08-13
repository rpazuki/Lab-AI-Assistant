Status: accepted
Owner: platform + operator
Last verified: 2026-08-13
Applies to: every external source request

# ADR-0018: Rate budgets are stored, and enforced per request

## Context

`RateBudget` existed in the domain with a token bucket, leases, and a stored
`Retry-After`. Connectors declared a `RatePolicy`. Repositories persisted budgets.
And live discovery called `connector.discover(request)` directly, so none of it ran.

That is the predecessor's failure with extra steps. Its limiter was in-memory, which
forced a single-worker topology: two workers meant two independent limiters and
double the request rate at the source. Having the *state* be shared while the
*enforcement* is absent produces the same outcome, and looks fixed.

## Decision

Every external source request passes through `RateLimiter`, which acquires a slot
from the stored budget keyed `<connector_id>|<host>`.

- **Per request, not per run.** The limiter wraps the connector's page stream and
  acquires before each page. A run is many requests, and requests are what a source
  counts.
- **Row-locked read-modify-write.** `get_rate_budget(for_update=True)` takes
  `SELECT … FOR UPDATE` on PostgreSQL. Without it two workers read the same token
  count, both proceed, and the shared budget becomes a suggestion.
- **Short waits sleep; long waits defer.** Under five seconds the worker sleeps.
  Beyond that — a stored `Retry-After`, usually — the task raises `rate_limited`
  and retries later **with its cursor preserved**, so a rate-limited run resumes
  rather than restarting.
- **An upstream 429 is recorded for everyone.** `note_retry_after` writes
  `next_permitted_at` on the shared row, so one worker's rejection holds the others
  off too.
- **The policy is config, not state.** Each acquisition refreshes the row's rate,
  burst, and concurrency from the connector's declared policy, so a change takes
  effect without deleting rows.
- **Every operation that reaches a source is budgeted**, not just discovery:
  `discover` through `throttle`, and `acquire` — and any future `resolve` or change
  feed — through `request`. Acquisition hits the same hosts under the same limits, so
  a budget only discovery respected was not a budget the source experienced. An
  AST test forbids awaiting or iterating a connector operation directly from the
  application layer, so a new call site cannot bypass the limiter by omission.
- **A rate-limited stage returns `FAILED` with `rate_limited`, never `BLOCKED`.**
  `BLOCKED` means "waiting on a dependency": the orchestrator stores it verbatim,
  sets no `not_before`, and a blocked task is not claimable — so the run would sit
  there until an operator noticed. `FAILED` routes through `Task.fail()`, which reads
  the retry table and sets `not_before` from the stored `Retry-After`. The cursor is
  already durable, so the retry resumes from the page after the last one fetched.
- **The same limiter throttles login and invitation acceptance** (ADR-0016). One
  store-backed bucket mechanism, two very different callers.

## Consequences

- Scaling workers out no longer scales request volume up. Concurrency caps are
  enforced in the database, which is also where the per-stage caps live.
- Every connector request now costs one small transaction. On a single VM this is
  noise next to the HTTP call it is protecting.
- A connector that bypasses `discover()` would bypass the limiter. The tests assert
  a real discovery run leaves a spent budget row behind, so that regression fails
  rather than passing quietly.

## Alternatives considered

**In-process limiter with a single worker.** What the predecessor did. It works
until someone scales the deployment, and then it fails at the source rather than in
the code.

**A broker with rate-limited queues.** Moves the limit into infrastructure the
supported profile does not have (ADR-0005 keeps the task DAG in PostgreSQL).

## Validation / revisit trigger

Revisit if a source starts publishing per-credential quotas that a token bucket
cannot express (monthly caps, tiered burst), or if budget contention shows up in
query latency.

## References

- `packages/python/lab_assistant/application/rate_limit.py`
- `packages/python/lab_assistant/domain/tasks.py` (`RateBudget`)
- Remaining-tasks audit MIT-010
