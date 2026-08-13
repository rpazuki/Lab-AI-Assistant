Status: accepted
Owner: technical owner + operator
Last verified: 2026-08-12
Applies to: pgvector indexes and publish cost

# ADR-0014: Vector index type, parameters, and build cost

## Context

The predecessor used `ivfflat` with `lists = 100`, chosen for a corpus that no longer
describes the target. The migration plan specified per-dimension tables and nothing
about indexes — yet the index type is part of what makes a release reproducible, and
index build time *is* the publish cost.

## Decision

**HNSW** is the default, with `m = 16` and `ef_construction = 64` recorded in the
embedding profile. HNSW gives better recall and latency than IVFFlat and does not need
a populated, representative table to build well — which matters because a release
publishes a fresh projection.

The index is created **after** the first projection build, not in the initial
migration: an empty-table index is either useless (IVFFlat) or wasted work.

Storage type is part of the profile: `vector` up to 2000 dimensions, `halfvec` above,
because pgvector's indexable dimension ceiling constrains which models can be indexed
directly.

Because release builds are incremental (ADR-0009), the normal publish path is index
*maintenance* on copied and inserted rows rather than a full rebuild.

## Consequences

- Two projections coexist during publish, so disk must be sized for it.
- HNSW build is slower and more memory-hungry than IVFFlat per row, and only the
  changed rows are built on a normal publish.
- Index parameters are part of the profile, so changing them is a new profile and a
  new projection — never a silent `REINDEX` that changes retrieval behaviour under a
  published release.

## Alternatives considered

- **IVFFlat with a tuned list count.** Rejected as the default: it needs
  representative data at build time and re-tuning as the corpus grows.
- **No index below some corpus size.** Rejected: exact scan latency is fine at 13k
  works and quietly stops being fine, which is the kind of cliff nobody notices.

## Validation / revisit trigger

Measure index build time per 100k chunks on the target hardware and set a publish
window SLO. Revisit when p95 vector latency exceeds 500 ms after tuning, per ADR-0003.

## References

- Technical spikes, S4
- `database/migrations/versions/20260812_0001_initial_evidence_graph.py`
