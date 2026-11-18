Status: proposed
Owner: technical owner + data steward + operator
Last verified: 2026-11-18
Applies to: deleting from the shared archive (phase 5)

# ADR-0028: Archive deletion is reachability-based, planned, reviewed and re-verified

## Context

Snapshots share payloads, old conversations cite old releases, and a tombstone
withholds bytes without deleting them. A purge that went by age, or by "not in the
latest snapshot", would delete evidence a citation still opens or bytes another
snapshot still names. Deletion is also the one archive operation that cannot be
undone, and the architecture asks for a separate deletion authority.

## Decision

**`lab archive retention plan`** reads and decides, and deletes nothing. A snapshot is
retained when it is the head, among the `--keep-last` most recent, held
(`--hold-snapshot`), or the newest snapshot holding a release some conversation
pinned, answered from or cited. A payload is deletable only when no retained snapshot
names it and it is not held (`--hold-digest`). Snapshots that are not retained — and
abandoned uploads no head ever named — are deletable, shards first and manifest last.
Tombstoned payloads a retained snapshot still names are kept, and the plan says which
snapshot keeps them. Snapshots are enumerated from the producer's publication records,
never by listing the bucket.

**`lab archive retention apply plan.json`** recomputes the plan from the parameters
recorded in it and deletes only if the recomputation has the reviewed plan's digest.
It is audited.

**Deletion is its own port** (`ArchiveDeletionStore`), implemented for the filesystem
and memory stores only. The S3 and GCS adapters deliberately have no `delete`: in a
bucket, deletion authority belongs to a separate principal and to the bucket's own
lifecycle and retention rules, and `apply` refuses on those backends.

## Consequences

- An operator can see, before anything is lost, exactly what goes and why everything
  else stays (S40, S41, S18).
- Bucket deployments plan here and delete through their own retention tooling, using
  the plan as the reviewed list. Making that one command needs a deletion principal
  configured separately from the publisher's — deployment work.
- Policy checkpoints and orphaned uploads that no publication record names are not
  enumerated, because finding them would need a bucket listing.
- Reader caches are not invalidated by a purge; readers withhold through policy
  checkpoints, and a reader's own retention is phase 3 work.

## Alternatives considered

**Delete what the latest snapshot does not name.** Rejected: older conversations cite
older releases, and an operator's hold would mean nothing.

**Mirror deletions to readers with a sync-and-delete.** Rejected by the architecture:
authority to delete is a lifecycle event, not a side effect of synchronisation.

## Revisit when

- a separate deletion principal exists in the deployment, which is when `apply` can
  run against a bucket;
- legal holds need to be recorded durably rather than passed on the command line.
