Status: active
Owner: technical owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/states.py`, `contracts/errors.py`

# State and error taxonomy

## States are orthogonal

An item is in eight states at once. They are stored separately.

| Dimension | Values |
|---|---|
| `IdentityState` | unresolved, resolved, conflict, merged, split |
| `RelevanceState` | unreviewed, included, included_uncertain, excluded |
| `RightsState` | unknown, permitted, restricted, prohibited, expired |
| `AcquisitionState` | unplanned, planned, acquired, assisted, unavailable, failed |
| `ParseState` | not_started, ready, degraded, quarantined, failed |
| `ExtractionState` | not_applicable, pending, proposed, validated, incomplete, failed |
| `ReviewState` | unreviewed, assigned, accepted, corrected, rejected, disputed, deferred |
| `ReleaseState` | draft, validating, rejected, approved, publishing, published, superseded, withdrawn |

One `status` column cannot express "identity resolved, rights unknown, acquired,
parsed but degraded, extraction pending". The predecessor tried, and the answer to
"why is this item stuck" became unanswerable.

`derive_workflow_label` collapses the dimensions into one human-facing label in
priority order — for display only. **The label is never stored as the source of
truth.**

## Task state

`pending`, `ready`, `leased`, `blocked`, `awaiting_user`, `degraded`, `quarantined`,
`succeeded`, `failed`, `dead_letter`, `cancelled`.

Two properties are load-bearing: `is_terminal`, and `counts_as_failure` — which
excludes `awaiting_user`, because waiting for a human is not a failure and must not
page anyone.

## Error classes and retry

Each `ErrorClass` maps to exactly one `RetryEligibility` in a table:

| Eligibility | Classes |
|---|---|
| `retryable` | source_unavailable, timeout, provider_error, conflict, internal |
| `retryable_after` | rate_limited — waits for the stored `Retry-After` |
| `human_action` | authentication_required, entitlement_required, budget_exceeded |
| `permanent` | policy_blocked, robots_blocked, malware_detected, invalid_query, invalid_content, identity_mismatch, too_large, not_found, provider_refusal, schema_violation, cancelled |

The table is the whole retry policy. Nothing decides retry behaviour by inspecting an
exception's message.

## Outcome taxonomies

- `SourceOutcome` — ten values per connector per discovery run. Only `succeeded` and
  `succeeded_empty` let a run freeze without steward acceptance.
- `AcquisitionOutcome` — seventeen values per attempt. `produced_asset` and
  `needs_human` are properties, not string comparisons at call sites.
- `AbstentionReason` — eleven values; only two `supports_source_statement`. See
  [entities, experiments, claims](entities-experiments-claims.md).

## HTTP surface

Errors reach the API as RFC 9457 problem details: `type`, `title`, `status`, `detail`,
`error_class`, `retry_eligibility`, and `trace_id`. A client can act on
`error_class` without parsing prose, and the `trace_id` matches the structured logs.

A policy denial is `403` with `policy_blocked` — and a resource the caller may not see
is `404`, with no hint that it exists. Distinguishing the two would be an enumeration
oracle.

## What members see

Every status the web app shows is described, in one sentence, in
`apps/web/src/lib/statuses.json`. A state chip given a `kind` shows an info button
that opens that description, and each how-to guide tabulates the groups its page can
show. Each group names the enum it describes, and
`tests/contract/test_status_glossary.py` fails when an enum gains a value without a
description, or the glossary describes one the enum no longer has, so adding a state
here means describing it in the same change.
