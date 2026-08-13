Status: accepted
Owner: technical owner
Last verified: 2026-08-12
Applies to: runtime topology

# ADR-0002: Modular monolith with two worker deployments

## Context

The lifecycle has stages with genuinely different failure and resource profiles:
network-bound discovery and acquisition, CPU-heavy parsing, cost-bounded extraction,
database-heavy indexing. The predecessor ran generic ingestion and datasheet queues
in one process because rate-limit state lived in memory, so a long provider call
starved the other queue and horizontal scale was impossible.

The migration plan proposed six worker deployments. At 50–100 users that is six
deployment units to operate for a problem whose actual cause was shared in-process
state.

## Decision

One versioned Python package, one FastAPI deployment, one Next.js application, and
**two** worker deployments: network-bound and compute-bound. Per-stage concurrency
caps live in the database, so the cap — not the replica count — is what stops one
stage starving another.

Worker directories under `workers/` are deployment units with a Dockerfile and a
thin entry point. They import the shared application package and never each other.

## Consequences

- Scaling a replica adds resilience, not request rate; a rate limit is respected
  even with several workers running.
- Two images to build and watch instead of six.
- A stage needing genuinely separate scaling (GPU OCR) can be split out later
  without changing the queue or the handlers, because the topology is not the source
  of truth.

## Alternatives considered

- **Six deployments, one per stage.** Deferred: the operational cost is immediate
  and the benefit is not, and the starvation problem is solved by the durable cap.
- **One worker for everything.** Rejected: parsing memory limits and network
  timeouts want different container settings, and a parsing worker should have no
  network at all.

## Validation / revisit trigger

Revisit when measured contention shows a stage waiting on a slot while a different
stage's replicas are idle, or when a stage needs hardware the other cannot use.

## References

- `packages/python/lab_assistant/contracts/stage.py` (`StageName.worker_class`)
- `tests/integration/test_task_engine.py::TestConcurrencyCaps`
