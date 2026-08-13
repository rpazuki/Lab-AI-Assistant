Status: accepted
Owner: scientific owner + technical owner
Last verified: 2026-08-12
Applies to: extraction and review

# ADR-0008: Claims graph with review; model output is a proposal

## Context

The predecessor emitted one wide row per paper, delegated bibliographic fields to the
model, and accepted an evidence quote without checking that it appeared anywhere. A
precise-looking citation could therefore point at nothing, and "Not reported"
conflated absent, inaccessible, not searched, parser failure, and extractor
abstention.

## Decision

Scientific facts are entities, experiments, conditions, and claims with revisions and
reviews. One work reports zero, one, or many experiments.

- Deterministic metadata comes from resolved source assertions. A model that proposes
  it is rejected by a validator.
- A model's output is a **proposal**. It becomes a claim only after schema
  validation, span verification against the rendition, numeric and unit checks, and
  focal-content checks.
- Abstention has ten distinct reasons, and only `reported` and
  `not_reported_after_complete_search` may support a statement about source content.
- A review decision is immutable and names the exact revision reviewed. A correction
  creates a new revision and a new review.
- A reviewer-authored interpretation is typed as such and cannot masquerade as an
  extracted source fact.

## Consequences

- A datasheet row becomes a projection of the graph, so a multi-experiment paper stops
  losing measurements.
- Review is mandatory work, and its throughput is the scarcest resource in the
  system. Double review is enabled per predicate on a measured correction rate rather
  than by default.
- Model confidence is diagnostic, never approval.

## Alternatives considered

- **Trust high-confidence extractions.** Rejected: confidence is not verification,
  and audit finding C-3 is exactly this failure.
- **Keep the wide row and add a quote column.** Rejected: the row cannot represent
  two experiments, and the quote was the thing that was never checked.

## Validation / revisit trigger

100 percent of accepted machine claims have mechanically valid evidence. If a review
pilot shows the budget cannot be met, the response is a stated policy change — a
smaller first template, or sampled review with the sample size recorded on the
release — not a quietly lowered gate.

## References

- `packages/python/lab_assistant/application/extraction.py`
- `packages/python/lab_assistant/domain/claims.py`
