"""The library's policy stream: signed, sequenced checkpoints (phase 2, **draft**).

A published release's scientific content is immutable. Permission to see it is
not. A licence is revoked, an embargo turns out to cover more than anybody
thought, a paper is retracted, a release is withdrawn — and every one of those
happens *after* readers have imported a snapshot that says otherwise. Waiting
for the next catalog publication to carry the news would leave a reader
serving revoked material for as long as nobody publishes, which in a lab can be
months. So policy travels on its own sequence, beside the catalog rather than
inside it, and a reader refreshes it independently.

A checkpoint is the publisher's signed statement of **current** standing for
everything a reader may hold: every rights decision reachable from a release
that was ever published, every withdrawn release, every retracted or corrected
work, and every payload digest the library has tombstoned. Four rules shape it.

**It narrows; it never widens.** A checkpoint can take away what a snapshot
permitted and cannot add to it (`domain/archive_policy.py`). An embargo lifting
is a real event, and it is a *publication*: the next snapshot carries the wider
decision. Keeping the stream deny-only is what makes its failure modes safe — a
stale, missing or replayed checkpoint can at worst be out of date about a
revocation, bounded by its validity, and can never make a reader more
permissive than the snapshot the publisher signed.

**Absent is denied.** A decision a snapshot names and the current checkpoint
does not mention is withheld, never inherited from the snapshot. "The policy
said nothing" is not a grant.

**Freshness is signed, not configured.** `valid_until` bounds how long restricted
material may be served on this checkpoint's word; `public_valid_until` is an
offline window for unrestricted material that the *publisher's* steward
approved, and equals `valid_until` unless somebody approved more. Both are
inside the signed digest. A reader may enforce a shorter bound of its own and
may never extend these: an offline mode a reader's operator could grant itself
is not an approval.

**Sequence and parent make replay visible.** `policy_sequence` is monotonic per
library and `parent_checkpoint_digest` names the predecessor, so a reader that
has applied sequence 9 refuses an 8 (a replayed or rolled-back head), and two
different checkpoints claiming one sequence are a fork rather than a refresh.

What this does not define: how a reader *enforces* a checkpoint while serving.
That is phase 3, and the domain rules it will call are already in
`domain/archive_policy.py` so that the producer side and the reader side cannot
disagree about what a checkpoint means.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

__all__ = [
    "POLICY_DIRECTORY",
    "POLICY_FORMAT_NAME",
    "POLICY_FORMAT_VERSION",
    "POLICY_HEAD_FORMAT_VERSION",
    "POLICY_HEAD_LOCATION",
    "PolicyCheckpoint",
    "PolicyHeadDescriptor",
    "ReleaseStanding",
    "RightsStanding",
    "TombstonedArtifact",
    "WorkStanding",
    "policy_location",
]

POLICY_FORMAT_NAME = "lab-assistant-library-policy"
#: Major.minor. **Stricter than the snapshot format about what it does not
#: understand:** a reader refuses a checkpoint carrying a field it cannot read,
#: in any version. The snapshot rule — preserve and do not interpret — is safe
#: for a catalog, where an unread field is information not yet used. In a
#: stream whose only job is to withhold, an unread field is most likely a
#: denial ignored. So readers are upgraded before a producer emits a new field,
#: which is the order the compatibility plan already requires.
POLICY_FORMAT_VERSION = "1.0"

#: Every model in a checkpoint refuses fields it does not declare.
_STRICT = ConfigDict(frozen=True, extra="forbid")

#: The policy head, beside the catalog head. A fixed location for the same
#: reason: a reader with a GET-only credential has to find it without listing.
POLICY_HEAD_LOCATION = "control/policy-head.json"
POLICY_HEAD_FORMAT_VERSION = "1.0"

#: Where checkpoints live inside a library's namespace.
POLICY_DIRECTORY = "policy"


def policy_location(sequence: int, digest: str) -> str:
    """The key of one checkpoint, relative to the library's prefix.

    Zero-padded so the keys sort in sequence order for an operator listing the
    directory, and named by digest so a key can never hold two different
    checkpoints: the location *is* the identity, as it is for content.
    """
    return f"{POLICY_DIRECTORY}/{sequence:012d}-{digest}.json"


class RightsStanding(BaseModel):
    """The current state of one rights decision.

    The same decision a snapshot froze as a `RightsDecisionRecord`, as it stands
    now. A reader intersects the two rather than choosing one, so a revocation
    here withdraws what the snapshot granted and nothing here can grant what the
    snapshot did not.
    """

    model_config = _STRICT

    decision_id: UUID
    #: A `RightsState` value. An unknown one is treated as the strictest, so a
    #: newer producer's state cannot read as a permission to an older reader.
    status: str
    allowed_actions: tuple[str, ...] = ()
    restrictions: tuple[str, ...] = ()
    #: Empty means the workspace default applies, exactly as on the record.
    restricted_to_group_ids: tuple[UUID, ...] = ()
    #: When the permission lapses. A reader treats a decision past this as
    #: expired whether or not a newer checkpoint has arrived to say so.
    review_due_at: datetime | None = None
    decided_at: datetime | None = None
    #: The steward's reason, when one was recorded: `licence_revoked`, say.
    reason_code: str | None = None


class WorkStanding(BaseModel):
    """A work whose standing changed in a way a reader must show.

    Only works with something to say travel: an entry per work would make the
    checkpoint a copy of the catalog. A retraction does not withhold the work —
    a retracted paper is still part of the record — it changes how a reader
    must present it.
    """

    model_config = _STRICT

    work_id: UUID
    retraction_status: str
    correction_status: str


class ReleaseStanding(BaseModel):
    """The lifecycle state of a release that was published at some point.

    A withdrawn release is the case this exists for: a reader that imported it
    must stop serving it, and nothing in an immutable snapshot can say so.
    Releases that were never published are absent: a reader cannot hold them,
    and listing them would disclose drafts.
    """

    model_config = _STRICT

    release_id: UUID
    status: str


class TombstonedArtifact(BaseModel):
    """A payload the library has disowned, by digest.

    By digest rather than by object id: a reader holds bytes it fetched by
    digest, and a tombstone it could not match against its own cache would
    withhold nothing.
    """

    model_config = _STRICT

    sha256: str
    reason: str | None = None


class PolicyCheckpoint(BaseModel):
    """One signed statement of a library's current policy."""

    model_config = _STRICT

    format_name: str = POLICY_FORMAT_NAME
    format_version: str = POLICY_FORMAT_VERSION

    library_id: UUID
    source_workspace_id: UUID
    checkpoint_id: UUID
    #: Monotonic per library and independent of the catalog's publication
    #: sequence: policy moves more often than science does.
    policy_sequence: int = Field(ge=1)
    #: The signing digest of the checkpoint this one succeeds. Absent only for
    #: the first. Two checkpoints naming one parent are a fork.
    parent_checkpoint_digest: str | None = None
    origin_installation_id: UUID
    writer_epoch: int

    issued_at: datetime
    #: Restricted material may be served on this checkpoint's word until here.
    valid_until: datetime
    #: Unrestricted material, until here. Equal to `valid_until` unless the
    #: publisher's steward approved an offline window.
    public_valid_until: datetime

    rights: tuple[RightsStanding, ...] = ()
    works: tuple[WorkStanding, ...] = ()
    releases: tuple[ReleaseStanding, ...] = ()
    tombstones: tuple[TombstonedArtifact, ...] = ()

    signature: str | None = None
    key_identifier: str | None = None

    def signing_digest(self) -> str:
        """Everything except the signature, which is also the checkpoint's name.

        The same exclusion `SnapshotManifest.signing_digest` makes, for the
        same reason: a signature inside the thing it signs would change it.
        """
        payload = self.model_dump(mode="python")
        payload.pop("signature", None)
        payload.pop("key_identifier", None)
        return sha256_bytes(canonical_json(payload))

    def content_digest(self) -> str:
        """The policy alone, without the envelope around it.

        Two checkpoints with equal content digests say the same thing about the
        library, however far apart they were issued. A re-issue that only
        extends the validity window is the normal heartbeat, and this is how
        an operator tells it from a change.
        """
        return sha256_bytes(
            canonical_json(
                {
                    "rights": self.rights,
                    "works": self.works,
                    "releases": self.releases,
                    "tombstones": self.tombstones,
                }
            )
        )

    def serialise(self) -> bytes:
        """Canonical bytes: what is written, fetched and digested."""
        return canonical_json(self.model_dump(mode="python"))

    @property
    def is_signed(self) -> bool:
        return bool(self.signature and self.key_identifier)

    @property
    def location(self) -> str:
        return policy_location(self.policy_sequence, self.signing_digest())


class PolicyHeadDescriptor(BaseModel):
    """The control record naming a library's current checkpoint.

    Carries owner, epoch and sequence for the reason the catalog head does: a
    compare-and-swap from a fenced writer must fail even when its checkpoint is
    perfectly well formed. It is deliberately *not* where a reader learns how
    long the policy is valid — the head is unsigned, and freshness is taken
    from the signed checkpoint it names.
    """

    model_config = {"frozen": True}

    format_version: str = POLICY_HEAD_FORMAT_VERSION
    library_id: UUID
    owner_installation_id: UUID
    writer_epoch: int
    policy_sequence: int
    checkpoint_id: UUID
    #: The checkpoint's signing digest. A reader compares it with the checkpoint
    #: it fetched, so a key holding something else is caught.
    checkpoint_digest: str
    updated_at: datetime | None = None
    #: Set when a handover moved the epoch, as evidence of who was fenced.
    fenced_predecessor_id: UUID | None = None

    def serialise(self) -> bytes:
        return canonical_json(self.model_dump(mode="python"))

    @property
    def location(self) -> str:
        return policy_location(self.policy_sequence, self.checkpoint_digest)
