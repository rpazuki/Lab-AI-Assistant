Status: accepted
Owner: technical owner + operator
Last verified: 2026-08-12
Applies to: storage and search

# ADR-0003: PostgreSQL control plane, task queue, and search

## Context

The measured predecessor corpus was roughly 13k works for 50–100 internal users. The
plan's alternatives — OpenSearch, a dedicated vector service, a workflow engine —
each add an operational surface, and each was proposed without a scale number.

## Decision

PostgreSQL 16 with pgvector holds transactional state, domain records, the task DAG,
audit, full-text search, and vectors. S3-compatible object storage holds immutable
bytes. No broker, no separate search service, no workflow engine.

## Consequences

- One database to back up, restore, and reason about; one transaction boundary for a
  write and its outbox event.
- Lexical and vector candidates can be filtered by the same policy predicate in
  SQL, *before* ranking — much harder across two systems.
- Postgres full-text ranking is weaker than a dedicated engine. Accepted at this
  scale, with the thresholds below making the deferral testable rather than
  rhetorical.

## Deferral thresholds

Re-evaluate when any of these is crossed:

| Signal | Threshold |
|---|---|
| Chunk rows in one active release | above ~5M |
| p95 vector search latency at target `top_k` | above 500 ms after index tuning |
| Index build time per publish | above the publish window |
| Ranking features needed | anything beyond RRF plus a cross-encoder |
| Concurrent search queries | above pool capacity at target latency |

## Alternatives considered

- **OpenSearch now.** Rejected: a second store to secure, back up, and keep in sync,
  bought before any measurement said it was needed.
- **Redis or Celery for tasks.** Rejected: another dependency, and the durable-row
  queue is what makes crash recovery and human-wait states inspectable.

## Validation / revisit trigger

The thresholds above, measured in production rather than estimated.

## References

- `packages/python/lab_assistant/adapters/db/`
- ADR-0014 for the index type and its parameters
