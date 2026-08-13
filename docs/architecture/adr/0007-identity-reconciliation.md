Status: accepted
Owner: data steward + technical owner
Last verified: 2026-08-12
Applies to: work identity

# ADR-0007: Strong-identifier-only automatic merge

## Context

The predecessor merged distinct DOIs on title similarity. It later corrected this to
strong-identifier grouping, but transitive identifier bridges could still combine
contradictory identities with no quarantine and no human decision.

## Decision

An automatic merge requires **all** of:

1. at least one exact strong identifier bridge;
2. no contradictory active strong identifier;
3. compatible resource kinds;
4. the configured confidence threshold;
5. a persisted decision explanation.

Title, author, and year similarity may *propose* a pair for review. It can never
satisfy requirement 1. A contradiction produces `identity_conflict`, and a work in
conflict cannot enter a release.

A preprint and its version of record are **linked, not merged**: they are related
works, and collapsing them loses the distinction between what was submitted and what
was published.

## Consequences

- Merged distinct works — the failure that is hardest to undo — becomes impossible
  automatically.
- More human decisions, and a review queue that must be staffed.
- Every merge, refusal, and conflict carries its reasoning, so "why are these one
  work" is always answerable.

## Alternatives considered

- **A cleverer similarity measure.** Rejected: cleverness cannot supply the strong
  identifier the invariant demands, and it costs reproducibility.
- **Auto-merge with an undo.** Rejected: unmerging after downstream claims and
  citations reference the merged work is not reliably possible.

## Validation / revisit trigger

`packages/python/tests/test_identity.py` covers every refusal reason. Title-only
auto-merges must remain zero in every reconciliation report.

## References

- `packages/python/lab_assistant/domain/identity.py`
