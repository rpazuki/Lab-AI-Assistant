Status: accepted
Owner: technical owner + data steward
Last verified: 2026-08-12
Applies to: the whole domain model

# ADR-0001: Source-neutral evidence graph, not a paper table

## Context

The predecessor's `NormalizedDocument` became five things at once: a source
observation, a canonical work, an acquired byte asset, a parsed rendition, and a
serving projection. Because its shape came from PubMed's record, every new source
looked like another kind of paper and every useful output looked like text to chunk.
Local inventories and datasheet rows were flattened into it too, and a cumulative
cache ended up holding 13,221 document records under 13,063 distinct ids with 4,040
PMIDs appearing under more than one identity.

## Decision

Model the five objects separately and let identity be a decision rather than a
column:

```text
source observation -> work (+ identifiers, relations) -> manifestation
  -> asset (bytes) -> rendition (parsed) -> claim -> release item -> projection
```

PubMed becomes one connector among several, with no privileged position in the
domain. Canonical metadata is *resolved from assertions*, and every canonical field
can name the assertions that support it.

## Consequences

- Adding a source is adding a package, not editing a switch statement.
- "Which source asserted this title, using which connector version" is answerable
  for every field — the predecessor could not answer it.
- A dataset, protocol, or inventory is a `work_kind`, not a fake paper.
- More tables and more joins than a single document row. Accepted: the joins are
  what make the provenance chain resolvable.
- Legacy rows import as *proposals about identity*, never as identity.

## Alternatives considered

- **Generalise `NormalizedDocument` incrementally.** Rejected for the data model:
  its hidden assumption is that everything is a paper, and that cannot be corrected
  in place. (The same rejection does *not* apply to auth, the frontend shell, the
  provider abstractions, or the operations scripts — those were ported. See
  ADR-0015.)
- **One table with a `kind` discriminator.** Rejected: it keeps the flattening while
  adding a column, and the constraints that matter differ per object.

## Validation / revisit trigger

The end-to-end acceptance scenario passes with two sources disagreeing about one
DOI. Revisit if a second lab domain cannot be represented without touching core
domain code — that would mean the core is not source-neutral after all.

## References

- Current-state audit, root cause of the drift
- `packages/python/lab_assistant/domain/`
- `tests/e2e/test_acceptance_scenario.py`
