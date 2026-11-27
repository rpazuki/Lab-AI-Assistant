Status: proposed
Owner: technical owner + data steward
Last verified: 2026-11-27
Applies to: what a published snapshot carries, and how a reader takes it (phase 3)

# ADR-0026: Content leaves the producer only under an explicit replication permission

Proposed rather than accepted, because its default is a rights decision: whether
any licence in the corpus permits copying papers to other installations is the
data steward's to make, and until it is made nothing's text leaves.

## Context

A reader must be able to search, ask and open citations without the producer.
That needs the papers' text on the reader: renditions to resolve evidence spans
against, search chunks to retrieve, vectors to rank. Phase 2 exported chunks and
vectors into the snapshot directory and then did not upload them; it never
carried the payloads at all. So a published archive held a catalog and nothing a
reader could serve from.

Fixing that means copying content to machines other than the one that acquired
it. The architecture proposal is explicit that this needs its own permission:
existing `store_content` and `process_local` permissions "cannot silently be
interpreted" as permission to store remotely or replicate, and unknown
permission must default to denial. Chunks and vectors are the paper's text in
another shape, so they are covered by the same rule as the PDF.

## Decision

**A new action, `replicate_content`,** in `contracts/policy.py`. It is a content
action, so it is denied by default like every other. It covers a payload and
everything derived from it: its rendition, the rendition's search chunks and
their vectors.

**The starter policy grants it only when the steward says so.**
`LAB_RIGHTS_REPLICATE_OPEN_CONTENT=true` adds it to the open-licence rule
(`cc-*`, `cc0`, `public-domain`). It is never granted by the user-assisted rule:
attested access for one person is not a licence to copy to every reader. It
applies to decisions made after it is set; earlier decisions keep the actions
they were granted.

**Packaging copies what may travel and records what may not.** `lab archive
package` writes payloads to `content/sha256/<ab>/<cd>/<digest>` beside the
catalog when a governing decision grants replication, and otherwise marks the
inventory entry `withheld_by_rights`, not required. Tombstoned payloads never
travel. A rendition follows its asset. Chunks and vectors are exported only for
renditions whose payload travels. The records — works, decisions, claims, reviews,
spans — travel regardless, so a reader knows a withheld paper and its reviewed
claims and cannot serve its text.

**Publication uploads everything the manifest names.** Payloads go to the
library's content store, `libraries/<id>/content/sha256/…`, shared by every
snapshot, so a payload is uploaded once per library; then projection and catalog
shards; then the manifest, last.

**A reader fetches by name, with GET alone** (`lab archive fetch`): head, then the
manifest the head committed to, then every shard and payload it names, then the
current policy checkpoint. Every object is verified against its declared length
and digest before it is written, and the manifest against the head's digest. A
denial raises rather than reading as an absence. An interrupted fetch resumes.
Locations and digests from the manifest are checked before they become paths.

**The catalog carries what a reader's database requires**: parse runs (a
rendition must name one), the entities and experiments claims name, normalised
identifier values, and release entries with the producer's own digest. The apply
order puts rights decisions before the assets that name them.

## Consequences

- **Until the steward decides, readers get records and no text.** That is the
  intended default, and it makes the reader feature inert in a deployment that
  has not made the decision. Tests that need text build their harness with the
  replicating policy.
- An existing corpus does not start replicating when the setting is turned on:
  its decisions predate the action. *Amended 2026-11-27:* `lab rights reapply`
  (`application/rights_reapply.py`) re-decides them. It applies the current policy
  to each decision's own licence and access basis, and only adds
  `replicate_content`; it never revives a prohibited, undecided or lapsed decision.
  A reader that already holds a release fills in the payloads it recorded as
  withheld when a later snapshot carries them, and never revives a payload
  tombstoned for any other reason.
- Whether a `cc-by-nc-nd` licence permits replicating *derived* text is exactly
  the kind of question this leaves to the steward; the open-licence rule matches
  by prefix and covers it.
- Packaging now writes a copy of every replicable payload to local disk, and
  publication reads each one fully into memory. Proportionate for a lab's
  corpus; streaming both is a later change.
- Projection shards are uploaded per snapshot rather than content-addressed, so
  an unchanged release's vectors are uploaded again with each snapshot.
- Snapshots packaged before this change have no payloads and fail
  `verify --payloads`, so `lab archive publish` refuses them. Re-package.

## Alternatives considered

**Read `store_content` as permission to replicate.** Rejected by the
architecture in so many words, and the one choice here that would move licensed
text without a decision.

**Ship chunks and vectors, withhold only the payloads.** Rejected: the chunks are
the full text, so this would gate nothing that matters.

**Have readers read the producer's object store directly.** Rejected: it couples
every reader to the producer's layout and workspace ids, cannot work for a
filesystem producer, and gives readers a credential to everything the producer
holds rather than to one library.

## Revisit when

- the steward decides which licences permit replication, which is when this can
  be accepted;
- a corpus is large enough that per-snapshot projection uploads or in-memory
  payload copies cost too much;
- a re-evaluation command exists for decisions made before this action did.
