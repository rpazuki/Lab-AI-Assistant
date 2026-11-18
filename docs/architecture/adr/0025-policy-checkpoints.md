Status: proposed
Owner: technical owner + data steward
Last verified: 2026-11-18
Applies to: the shared archive's policy stream (phase 2)

# ADR-0025: Policy travels as signed checkpoints that narrow and never widen

Proposed rather than accepted. The mechanism is implemented and tested, but two
of its consequences are the data steward's to accept: a widening (an embargo
lifting, a renewed review date) reaches readers only with the next snapshot,
and the default validity window is a proposal, not a measured choice.

## Context

A published release's scientific content is immutable
([ADR-0009](0009-releases-and-aliases.md)). Permission to see it is not. A licence
is revoked, a review date passes, a paper is retracted, a release is withdrawn, a
payload is taken down — after readers have imported a snapshot that says
otherwise. `RefreshService.revoke_rights` already rewrites the decision row on
the publisher, and the snapshot a reader holds cannot hear about it.

Waiting for the next catalog publication would leave a reader serving revoked
material for as long as nobody publishes, which in a lab can be months. The
architecture proposal therefore asks for a separate, versioned policy stream
that a reader refreshes independently, with a bounded validity so that a reader
that cannot refresh stops disclosing restricted material rather than continuing
on the last thing it heard (scenarios S19, S20, S65).

Three things make this harder than a list of revocations:

- the stream can be **stale, missing or replayed**, so its failure modes have to
  be safe ones;
- **one payload can serve two acquisitions** under different decisions (S14), so
  "withhold these bytes" and "withhold this record" are different questions;
- the library has **one writer and now two control records** to fence, which
  cannot be moved atomically.

## Decision

**A checkpoint is a signed, sequenced statement of current standing** for the
published closure: every rights decision reachable from a release that was ever
published, those releases' lifecycle states, retracted or corrected works among
them, and tombstones on their payloads. Drafts and unpublished acquisitions are
not included; a reader cannot hold them and listing them would disclose them.
Contract: `contracts/archive_policy.py`. Objects live at
`libraries/<id>/policy/<sequence>-<digest>.json`, named by their signing digest,
and `libraries/<id>/control/policy-head.json` names the current one.

**It narrows; it never widens.** A reader applies the intersection of the
decision the snapshot froze and the standing the checkpoint reports — the
stricter status, the actions both allow, every restriction either names, the
groups both admit, the earlier expiry (`domain/archive_policy.narrow`). A
decision the checkpoint does not mention is withheld. A status a reader does not
recognise is the strictest. A refused checkpoint withholds nothing either: the
reader keeps what it applied before.

**Freshness is signed.** `valid_until` bounds restricted material and
`public_valid_until` is an offline window for unrestricted material, equal to
`valid_until` unless the publisher's steward approved more. Both are inside the
signature. A reader may enforce a shorter bound and may never extend these. The
default issue window is 24 hours (`LAB_ARCHIVE_POLICY_VALIDITY_HOURS`) and the
public window is off (`LAB_ARCHIVE_POLICY_PUBLIC_VALIDITY_HOURS=0`).

**Sequence and parent make replay visible.** The sequence is monotonic per
library and independent of the catalog's; each checkpoint names its
predecessor's digest. A reader refuses a lower sequence than one it applied
(replay), a second checkpoint at one sequence (fork), and a checkpoint older than
the one the snapshot was built under: `lab archive package` records the policy
head in the manifest's `policy_sequence` and `policy_artifact_digest`, and
`lab archive publish` refuses a snapshot naming a checkpoint the archive does
not hold.

**Both heads fence the writer.** `domain/publication.authority_refusal` is the
one copy of the epoch and owner rules, and a writer checks the catalog head *and*
the policy head before moving either. A handover that moved one and crashed
before the other has still fenced the old writer from both. As for the catalog
head, a failed swap is never retried.

**Bytes carry their retention edges.** Each inventory entry records what it was
derived from (`depends_on`: a rendition depends on its asset) and which decisions
govern it (`governed_by`). A record is withheld when its own acquisition's
decision withholds it; bytes are withheld only when every governing decision
does, so revoking one acquisition leaves another its evidence (S40). A rendition
inherits its asset's fate, and so do the passages searched out of it.

**Checkpoints refuse fields they do not declare.** Stricter than the catalog's
preserve-and-ignore rule, because in a stream whose only job is to withhold, an
ignored field is most likely a denial ignored. Readers are upgraded before a
producer emits a new field.

**The signed statement is domain-separated.** One key signs snapshots and
checkpoints; the policy statement carries `statement_type` and its own field
names, so no signature over one can verify as the other.

## Consequences

- A revocation reaches readers within one refresh interval, without a new
  snapshot or any re-import.
- **A widening is a publication.** Lifting an embargo or renewing a review date
  reaches readers with the next snapshot, not the next checkpoint. Re-packaging
  an unchanged closure is cheap — vectors are reused — but somebody has to do it
  before the old review date passes, or readers withhold the material.
- **The publisher must re-issue on a timer.** A checkpoint that is not renewed
  expires, and readers then stop serving restricted material. Nothing schedules
  `lab archive policy publish --apply` yet; `lab archive policy status` exits
  non-zero when the current checkpoint is not fresh, so a monitor can alert.
- Readers enforce none of this until phase 3. The rules they will call are the
  ones the validator already applies (`lab archive verify --policy`), so the
  producer and the reader cannot disagree about what a checkpoint means.
- `withheld_by_retention` is not derived. Whether a reader may *keep* bytes is a
  remote-storage and replication permission the architecture says must be
  explicit, and `store_content` must not be silently read as it. That needs the
  steward's decision on a new action.
- A checkpoint is one document. That is proportionate at the lab's scale; past a
  few tens of thousands of decisions it should be sharded like the catalog.
- No audit row is written for an issue, matching snapshot publication; the
  signed checkpoint and the head record who issued what.
- **What moves, for the rights review.** A checkpoint carries little a
  snapshot's reader does not already hold: the same decision fields, plus each
  decision's reason code and the digests of tombstoned payloads, and only for the
  published closure. Drafts, unpublished acquisitions, accounts, grants and
  memberships stay on the publisher.
- Snapshots packaged before this change carry no retention edges and now fail
  `lab archive verify`. The format is a draft and none has reached a real
  archive, so they are re-packaged rather than migrated.

## Alternatives considered

**Policy fields on the catalog head.** One control record, one swap. Rejected:
it couples policy to the snapshot sequence, and every policy-only update would
rewrite a head whose `ALREADY_PUBLISHED` and sequence rules exist for snapshots.

**Replace rather than intersect.** Lets a checkpoint lift an embargo at once.
Rejected: a stale or replayed checkpoint could then grant what the publisher
later withdrew, which turns every freshness failure into a disclosure.

**Freshness from each reader's configuration.** Rejected as the source of the
window, kept as a way to shorten it: an offline mode a reader's operator could
grant itself is not an approval.

**An outbox for policy issues.** Rejected. The catalog needs one because
publishing a snapshot twice claims two sequences for one piece of work. A
checkpoint is state, not work: if the swap lands and the process dies, the head
already says what happened, and the next issue supersedes it.

**Pushing revocations to readers.** Deferred. It needs a channel to every
reader, and a reader that missed a push would be no better off than one polling
— the validity window is what bounds the risk either way.

## Revisit when

- a deployment needs embargo lifts or renewals faster than its publication
  cadence;
- checkpoints grow past what one document should carry;
- the steward defines a remote-storage or replication permission, which is when
  `withheld_by_retention` can be derived;
- phase 3 measures how often readers actually refresh, which is when the 24-hour
  default should be replaced by a number.
