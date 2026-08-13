Status: accepted
Owner: technical owner + scientific owner
Last verified: 2026-08-12
Applies to: serving

# ADR-0009: Immutable corpus releases behind atomic aliases

## Context

The predecessor's index build created a manifest before processing and updated counts
in a `finally` block, so a partial run could leave a plausible-looking manifest and
log completion-like information for incomplete work. There was no publish gate and no
way to roll back a bad corpus without rebuilding.

## Decision

A corpus release is an immutable set of works, renditions, claim revisions, and a
policy snapshot, with a deterministic canonical manifest and a digest. Publishing
builds projections, validates gates, and then switches an alias atomically. Rollback
switches the alias back.

A release's projection build is **incremental by construction**: an item whose content
digest matches its parent copies its projection rows instead of recomputing them, so
publish time tracks the size of the change rather than the size of the corpus.

## Consequences

- A failed build never touches an active alias.
- An old answer stays reproducible after a correction, because the correction is a
  successor release and the previous release still exists.
- Storage roughly doubles during publication while two projections coexist. Sized for.
- Frequent releases stay practical, which is what makes correction cheap.

## Alternatives considered

- **Mutate the index in place.** Rejected: it destroys reproducibility and makes
  rollback a restore.
- **Full rebuild per release.** Rejected: publish time would grow with the corpus, and
  a lab that cannot afford to publish stops correcting.

## Validation / revisit trigger

`tests/integration/test_incremental_projection.py` proves a one-item diff recomputes
one item. Revisit the publish window if index build time exceeds it.

## References

- `packages/python/lab_assistant/domain/release.py`
- `packages/python/lab_assistant/application/indexing.py`
