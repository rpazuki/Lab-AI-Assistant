Status: active
Owner: technical owner
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/domain/observation.py`

# Source observations

An observation records **what a source said, when, in response to what** — not what is
true. Two sources disagreeing about a publication year is normal, and both assertions
survive.

## The record

| Field | Notes |
|---|---|
| `id`, `workspace_id`, `discovery_run_id` | provenance |
| `connector_id`, `connector_version` | which code, which version |
| `request_fingerprint` | canonical digest of the exact query issued |
| `observed_at` | when the source answered |
| `raw_response_digest` | the object-store digest of the **unparsed** response |
| `asserted` | the parsed field values, each with the source that asserted it |
| `page_token`, `page_index` | pagination position |
| `outcome` | a `SourceOutcome` — one of ten, not a boolean |

**The raw response is stored before parsing.** A connector-parsing bug is then fixed by
re-parsing stored bytes rather than re-querying a rate-limited source, and last month's
observation can be re-derived under this month's parser.

**Bytes and envelopes are labelled.** Some sources need two calls to produce one page
(PubMed's `esearch` then `efetch`), so a connector's page body is sometimes a
*replay envelope* the system built rather than the source's own bytes.
`response_body_kind` says which — `upstream_bytes` or `replay_envelope` — and when it
is an envelope the source's bytes are stored as their own object
(`upstream_body_object_id`, `upstream_sha256`). Replay determinism and provenance are
different requirements; claiming "exact upstream bytes" when it is only sometimes true
is worse than not claiming it, because an auditor cannot tell the rows apart.

**Every source request is rate-budgeted.** Discovery acquires a slot from a stored
token bucket keyed `<connector_id>|<host>` before each page, so scaling workers out
does not scale request volume up (ADR-0018). A rate-limited run defers with its cursor
intact and resumes; it does not restart.

## Per-connector outcomes

A run holds one `ConnectorRunState` per connector: outcome, pages fetched, items seen,
reported total, retry-after, and whether pagination reconciled. `succeeded_empty` is
distinct from `succeeded`, and both are distinct from `degraded_partial`. A run where
one source rate-limited is *not* a complete run, and the difference is recorded rather
than inferred later from a suspiciously small count.

## Freezing a run

```python
blockers = run.freeze_blockers()   # empty tuple means it may freeze
```

Freezing is refused, with named blockers, when a connector has a non-terminal outcome,
when an outcome is terminal but not acceptable without steward acceptance, or when
`items_seen` disagrees with the source's reported total and pagination has not been
reconciled. A steward may accept a degraded run explicitly — the acceptance is recorded
and travels with any release built from it.

Freezing is also what schedules reconciliation, exactly once, over the whole
observation set. Reconciling per connector would resolve identity against partial data
and then be deduplicated away when the rest arrived — a real bug, fixed here by
construction.

## Idempotency

An observation's identity is `(connector_id, request_fingerprint, page_token)`.
Re-running discovery over the same query re-observes; it does not duplicate.
