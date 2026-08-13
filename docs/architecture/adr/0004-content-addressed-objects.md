Status: accepted
Owner: technical owner
Last verified: 2026-08-12
Applies to: object storage

# ADR-0004: Content-addressed immutable object storage

## Context

The predecessor's acquisition cache lived inside a run directory and was keyed by a
DOI-derived filename, so a later run could not share an asset and "fetch once across
runs" was not true. A registered checksum could also refer to a file that was later
appended to, which makes content registration meaningless.

## Decision

Objects are addressed by digest:

```text
sha256/ab/cd/<full-digest>
```

Object names encode nothing else — no DOI, no PMID, no user filename, no run
directory. Logical metadata lives in PostgreSQL and points at the key. `put` is
idempotent by digest; there is no overwrite operation. Deletion is a tombstone plus
an audited purge.

## Consequences

- Deduplication, replay, and integrity verification are free.
- A citation target cannot drift, because bytes under a key never change.
- The same public paper is stored once per workspace, since object encryption is
  workspace-scoped. That cost is real and is accepted in ADR-0006.
- A corrected file is a new object and a new rendition, never a mutation.

## Alternatives considered

- **Path-based keys with human-readable names.** Rejected: a path is a per-topology
  address, and the predecessor's absolute paths in a shared column proved it.
- **Mutable append-only caches.** Rejected: the checksum stops meaning anything.

## Validation / revisit trigger

`tests/contract/test_adapter_contracts.py::TestObjectStoreImmutability`. Revisit if
storage cost from cross-workspace duplication becomes material — which requires a
second workspace to exist first.

## References

- `packages/python/lab_assistant/adapters/object_store/filesystem.py`
