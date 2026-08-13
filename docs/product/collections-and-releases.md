Status: active
Owner: data steward
Last verified: 2026-08-12
Source of truth: `packages/python/lab_assistant/application/scopes.py`, `releases.py`

# Collections and releases

A **collection** is a living scope: what to look for, where, and under what rules. A
**release** is a frozen, immutable snapshot of what that scope produced. Questions are
answered from releases, never from a live scope — otherwise the same question would
give different answers as material arrived mid-session.

## Building one

1. **Define the scope.** Queries, sources, date range, inclusion and exclusion rules,
   review thresholds. Every edit creates a new scope *version* with a config checksum,
   so a release can name exactly which version produced it.
2. **Discover.** Each connector's raw response is stored before it is parsed, so a
   parser fix never requires re-querying a source. Partial failure is recorded per
   connector; a run with a failed connector is not silently a complete run.
3. **Freeze the run.** Freezing is refused while a connector is incomplete or
   pagination is unreconciled, and the blockers are named. Freezing is also what
   schedules reconciliation — over the complete observation set, not a partial one.
4. **Reconcile.** Strong identifiers merge automatically. A contradiction becomes an
   `identity_conflict` for a human. Title similarity only ever *proposes*.
5. **Acquire.** The best legally available copy, by the route ladder in
   [assets and rights](../data-contracts/assets-and-rights.md). When no route is
   permitted, the item enters `awaiting_user` and a human may upload a copy they are
   entitled to. Waiting for a person is not a failure.
6. **Parse, extract, review.** See [review workflows](review-workflows.md).

## Publishing

```text
draft --validate--> validated --approve--> approved --publish--> published
                                                                    |
                                              rollback / supersede  v
                                                              superseded
```

`validate` reports **every** blocker at once — unreviewed claims, unverified spans,
missing rights decisions, failed evaluation gates — rather than the first one. A
validation that surfaces one problem per attempt turns publishing into a guessing
game.

Approval and publication are separate. Approval is a scientific judgement; publication
is an operational act with a schedule.

## Aliases and rollback

Readers use an alias (`current`), never a release id. Publishing repoints the alias
atomically. **Rollback repoints it back** — the previous release still exists with its
projections intact, so recovery is a pointer change measured in seconds, not a rebuild
measured in hours.

## What a release costs to build

Projections are incremental. Each item has a content digest; when it has not changed,
its chunk and embedding rows are **copied** into the new release rather than
recomputed. Publishing a release that adds twelve papers to a corpus of thousands
costs twelve papers' worth of work. This is a tested property, not an aspiration.

## Retraction

Retracting a work in a published release does not rewrite the release — an immutable
snapshot that quietly changed would be worthless. It records the retraction, flags
answers that cited it, and surfaces the work as excluded in the next release.
