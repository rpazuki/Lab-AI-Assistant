Status: accepted
Owner: product owner + technical owner
Last verified: 2026-08-13
Applies to: cutover

# ADR-0020: The API is a deliberate break, with a written map

## Context

The predecessor's API was PubMed-first: `/api/v1/chat/sessions`, analytics endpoints
keyed to MeSH terms, a `search` that returned chunks of `documents`. This API is
source-neutral, release-scoped, and evidence-first. The shapes are not compatible,
and pretending otherwise with adapter routes would carry the predecessor's model —
one overloaded `status`, chunks as citation targets — into the successor's surface.

But "intentionally different" is not a migration plan. Lab members have saved chats,
notebooks hit the old analytics endpoints, and evaluation scripts call the old
search.

## Decision

**No compatibility shim.** The break is explicit, and a written map covers every
predecessor endpoint: mapped, retired, or handled by a dual-run procedure. The map
is [`docs/migration/api-compatibility.md`](../../migration/api-compatibility.md) and
it is part of the cutover checklist rather than a note in a ticket.

Three rules behind it:

- **Chat history is not migrated.** A predecessor answer cites a chunk of a
  possibly-changed row; imported here it would be an answer whose citations cannot
  resolve. The predecessor stays readable read-only through cutover, which is where
  someone reads their old threads.
- **Analytics endpoints move rather than translate.** MeSH is one vocabulary
  asserted by one source; the successor reports subject assertions with their
  source. A URL redirect would return numbers with a different meaning under an old
  name, which is worse than a 404.
- **Evaluation scripts are rewritten, and that is the point.** They now measure
  spans rather than works, because retrieving the right paper and the wrong
  paragraph is the failure the successor exists to catch.

## Consequences

- Nobody discovers the break at cutover: the map is written and the dual-run period
  is when people notice what they relied on.
- Some predecessor workflows have no successor and are retired on purpose. Each one
  is named in the map with the reason, so retirement is a decision rather than an
  omission.
- The API keeps its shape for its own sake: source-neutral, release-scoped, and free
  of the predecessor's assumptions.

## Alternatives considered

**Adapter routes under `/api/v0`.** Cheap to write, expensive to keep: every
successor change would have to be expressible in the predecessor's vocabulary, and
the "temporary" layer would outlive the migration.

**Migrate chat history with citations degraded to metadata.** Produces answers whose
citations do not resolve — precisely the property the successor was built to remove.

## References

- `docs/migration/api-compatibility.md`
- ADR-0015 (delivery profile and dual running)
- Remaining-tasks audit MIT-013
