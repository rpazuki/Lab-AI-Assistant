Status: proposed
Owner: technical owner
Last verified: 2026-11-18
Applies to: how a reader holds an imported library (phase 3)

# ADR-0029: A reader imports additively, under the producer's ids, and activates by alias

## Context

The plan required one tested strategy for reader generations before any schema work:
generation-keyed corpus tables (every query pins a generation), or separate staging and
active schemas. Both are large refactors of every repository query. Meanwhile most of
the corpus is already immutable by id — releases, release entries, renditions, spans,
claim revisions — and the parts that are not, permissions above all, now have their own
channel in the policy stream ([ADR-0025](0025-policy-checkpoints.md)).

## Decision

**Neither of the plan's two options, for now: an additive import.** `lab archive import`
writes a fetched, validated snapshot's rows into the reader's ordinary tables under the
producer's ids, skipping rows already present, so a second snapshot of the same library
adds rows and rewrites none. Search projections are keyed by release, as they already
are. The serving alias moves last, in the same transaction as the import record; until
then the new rows belong to a release no alias names, which serving cannot reach.

**Permission is the one thing rewritten on every import.** The reader's rights rows are
the snapshot's frozen decisions narrowed by the current policy checkpoint, with producer
group ids translated through an operator's `--group-map`; an unmapped group admits
nobody. Retrieval now withholds a work whose rights decision is revoked or past its
review date, before scoring and in the counts — a check it never made, so until now a
revocation stopped nothing on the producer either.

**Checked again on arrival.** The import validates the directory with payloads,
signature and policy; re-verifies every evidence span against the rendition bytes it
points into; and refuses an older head, a second snapshot at one head, or an older policy
checkpoint than the one the reader's import record says it applied. Vectors are imported
only under the reader's own embedding profile; otherwise search is lexical and the report
says so. Titles a producer reconciles from source assertions come from the snapshot, kept
in a `work_metadata` table.

## Consequences

- The reader's database looks like a publisher's, so every existing serving path —
  retrieval, chat, citations, analytics — works on it unchanged.
- **Mutable-by-id metadata is the residual risk.** A work's reconciled title, or a claim's
  current revision, is updated in place by a later import, so a request served from an
  older release can show the newer title. Evidence, values and citations are immutable
  and are not affected. If that matters, generation-keyed tables are the next step.
- Old releases stay as long as their rows do; a reader's own retention is not built.
- Objects are stored before the row transaction, so a failed import can leave registered
  payloads with nothing pointing at them, which `lab storage scan` reports as orphans.

## Alternatives considered

**Generation-keyed tables, or staging and active schemas.** Each is what the plan
recommended, and each changes every query. Deferred until the residual risk above is
shown to matter.

**Importing into fresh tables and swapping them.** Breaks every foreign key from the
reader's own rows — chats and citations — into the corpus.

## Revisit when

- in-place title or current-revision updates cause a visible inconsistency;
- readers need to drop old releases, which needs a reader-side retention plan.
