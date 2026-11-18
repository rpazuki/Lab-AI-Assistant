"""Who may move a library's head, and what a crash means (phase 2).

One library has one writer. That is not enforced by everyone agreeing to
behave: it is enforced by a compare-and-swap against a control record carrying
owner, epoch and sequence together, and by the rules in this module, which
refuse before the storage layer is even asked.

The trap this module exists to avoid is named in the architecture proposal, and
it is the one a careful implementation walks straight into:

> A writer must never retry publication by adopting a newly observed foreign
> epoch.

A compare-and-swap fails. The obvious recovery is to re-read the head, see what
it says now, and try again with the fresh values. That recovery is exactly
wrong. A failed swap means somebody else moved the head — and if their epoch is
*higher*, this writer has been fenced: a handover happened, deliberately, and
this process is the old publisher that was supposed to stop. Re-reading and
retrying with the new epoch is a fenced writer talking its way back into
authority, and it would succeed. So `evaluate_commit` is given the epoch the
publisher *started with* and refuses to be told a better one.

The other half is what a crash means, and the answer depends entirely on which
side of the swap it happened:

- **before the swap** — the snapshot sits in the archive referenced by nothing.
  A reader resolves the head and never sees it. That is litter, not a
  publication, and retrying is safe because nothing was claimed.
- **after the swap** — the snapshot is live whether or not the publisher
  managed to write that down. Retrying here would publish the same work twice
  under two sequences. Recovery must read the head, recognise its own snapshot,
  and record what already happened rather than repeat it.

`reconcile` is that judgement, and it never writes: it says what a recovering
publisher should conclude, and the caller does it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from lab_assistant.contracts.publication import (
    HeadDescriptor,
    PublicationRecord,
    PublicationState,
)

__all__ = [
    "CommitRefusal",
    "CommitVerdict",
    "ReconcileAction",
    "ReconcileVerdict",
    "authority_refusal",
    "evaluate_commit",
    "next_head",
    "reconcile",
]


class CommitRefusal(StrEnum):
    """Why a publisher may not advance this head."""

    #: The head belongs to a later epoch: a handover fenced this writer. It
    #: must stop, not retry — and specifically must not adopt the epoch it just
    #: observed.
    FENCED_BY_EPOCH = "fenced_by_epoch"
    #: Same epoch, different owner. Two installations believe they are the
    #: publisher, which is a configuration fault rather than a race, and
    #: guessing which is right would be worse than refusing.
    FOREIGN_OWNER = "foreign_owner"
    #: The publisher's epoch is ahead of the head's without a recorded
    #: handover. It may be correct and this process cannot establish that, so
    #: it refuses rather than seizing the library.
    EPOCH_AHEAD_OF_HEAD = "epoch_ahead_of_head"
    #: The sequence this attempt claims is not the next one. Usually a stale
    #: attempt resumed after somebody else published.
    SEQUENCE_NOT_NEXT = "sequence_not_next"
    #: This exact snapshot is already what the head points at.
    ALREADY_PUBLISHED = "already_published"
    #: The snapshot names a policy checkpoint the archive cannot produce — not
    #: uploaded, or ahead of a policy head that went backwards. Published, it
    #: would send every reader looking for policy that is not there.
    POLICY_UNRESOLVED = "policy_unresolved"


@dataclass(frozen=True, slots=True)
class CommitVerdict:
    permitted: bool
    refusal: CommitRefusal | None = None
    detail: str | None = None
    #: The sequence the attempt should claim, when it is permitted.
    commit_sequence: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "permitted": self.permitted,
            "refusal": self.refusal.value if self.refusal else None,
            "detail": self.detail,
            "commit_sequence": self.commit_sequence,
        }


def evaluate_commit(
    head: HeadDescriptor | None,
    *,
    library_id: UUID,
    owner_installation_id: UUID,
    writer_epoch: int,
    snapshot_id: UUID,
) -> CommitVerdict:
    """May this publisher move this head, and to what sequence?

    ``writer_epoch`` is the epoch the publisher held when it started, and it is
    deliberately not re-read from the head here. A function that took the head's
    own epoch as the publisher's would permit every writer, including the one a
    handover just fenced.
    """
    if head is None:
        # An empty library. The first publication creates the head, and the
        # create-only precondition is what makes two simultaneous firsts resolve
        # to one winner rather than to a silent overwrite.
        return CommitVerdict(permitted=True, commit_sequence=1)

    refused = _library_refusal(head, library_id=library_id)
    if refused is not None:
        return refused

    if head.snapshot_id == snapshot_id:
        return CommitVerdict(
            permitted=False,
            refusal=CommitRefusal.ALREADY_PUBLISHED,
            detail=(
                f"the head already points at snapshot {snapshot_id}; publishing it "
                "again would claim a second sequence for one piece of work"
            ),
        )

    refused = _writer_refusal(
        head, owner_installation_id=owner_installation_id, writer_epoch=writer_epoch
    )
    if refused is not None:
        return refused

    return CommitVerdict(permitted=True, commit_sequence=head.commit_sequence + 1)


def authority_refusal(
    record,
    *,
    library_id: UUID,
    owner_installation_id: UUID,
    writer_epoch: int,
) -> CommitVerdict | None:
    """Why this writer has no authority over a control record, if it has none.

    Any control record carrying ``library_id``, ``owner_installation_id`` and
    ``writer_epoch`` — the catalog head or the policy head. A library has two
    and one writer, and the two cannot be swapped atomically: a handover moves
    one and then the other, and can crash in between. So a writer checks
    *both* before moving either, and whichever record a handover reached first
    fences the old writer from the pair. Checking only the record about to be
    moved would let a half-finished handover leave the old publisher issuing
    policy for a library it no longer owns.
    """
    if record is None:
        return None
    return _library_refusal(record, library_id=library_id) or _writer_refusal(
        record, owner_installation_id=owner_installation_id, writer_epoch=writer_epoch
    )


def _library_refusal(record, *, library_id: UUID) -> CommitVerdict | None:
    if record.library_id == library_id:
        return None
    return CommitVerdict(
        permitted=False,
        refusal=CommitRefusal.FOREIGN_OWNER,
        detail=(
            f"the control record at this location belongs to library {record.library_id}, "
            f"not {library_id}: this is the wrong archive, not a conflict"
        ),
    )


def _writer_refusal(
    head, *, owner_installation_id: UUID, writer_epoch: int
) -> CommitVerdict | None:
    """The epoch and owner rules, shared by every control record.

    One copy, because two copies of a fencing rule drift, and the one that
    drifts is the one an old publisher talks its way past.
    """
    if writer_epoch < head.writer_epoch:
        return CommitVerdict(
            permitted=False,
            refusal=CommitRefusal.FENCED_BY_EPOCH,
            detail=(
                f"this publisher holds epoch {writer_epoch} and the head is at "
                f"{head.writer_epoch}: a handover fenced this installation. Stop. Do "
                "not retry with the epoch you just observed — that is the fenced "
                "writer taking the library back"
            ),
        )

    if writer_epoch > head.writer_epoch:
        return CommitVerdict(
            permitted=False,
            refusal=CommitRefusal.EPOCH_AHEAD_OF_HEAD,
            detail=(
                f"this publisher holds epoch {writer_epoch} and the head is at "
                f"{head.writer_epoch}. A handover may be legitimate and this process "
                "cannot establish that it happened, so it refuses rather than seizing "
                "the library"
            ),
        )

    if head.owner_installation_id != owner_installation_id:
        return CommitVerdict(
            permitted=False,
            refusal=CommitRefusal.FOREIGN_OWNER,
            detail=(
                f"installation {owner_installation_id} and {head.owner_installation_id} "
                f"both claim epoch {writer_epoch}. Two publishers on one library is a "
                "configuration fault, and picking one would make it permanent"
            ),
        )

    return None


def next_head(
    head: HeadDescriptor | None,
    *,
    library_id: UUID,
    owner_installation_id: UUID,
    writer_epoch: int,
    snapshot_id: UUID,
    snapshot_manifest_digest: str,
    commit_sequence: int,
    updated_at=None,
) -> HeadDescriptor:
    """The control record a permitted commit writes."""
    return HeadDescriptor(
        library_id=library_id,
        owner_installation_id=owner_installation_id,
        writer_epoch=writer_epoch,
        commit_sequence=commit_sequence,
        snapshot_id=snapshot_id,
        snapshot_manifest_digest=snapshot_manifest_digest,
        updated_at=updated_at,
        fenced_predecessor_id=(
            head.owner_installation_id
            if head is not None and head.owner_installation_id != owner_installation_id
            else None
        ),
    )


class ReconcileAction(StrEnum):
    """What a recovering publisher should conclude about an interrupted attempt."""

    #: The head carries this snapshot. It published; only the bookkeeping is
    #: behind. Record the commit — do not publish again.
    ACKNOWLEDGE = "acknowledge"
    #: The head does not carry it and never will: somebody else moved on.
    #: Abandon the attempt and leave the uploaded shards as litter for a
    #: retention sweep, which is a cheaper problem than a wrong head.
    ABANDON = "abandon"
    #: The head is unchanged and this attempt can still claim its sequence.
    RESUME = "resume"
    #: Nothing to do.
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ReconcileVerdict:
    action: ReconcileAction
    detail: str
    record_id: UUID | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "detail": self.detail,
            "record_id": str(self.record_id) if self.record_id else None,
        }


def reconcile(record: PublicationRecord, head: HeadDescriptor | None) -> ReconcileVerdict:
    """What an interrupted attempt means, given what the head says now.

    The head is the truth and the outbox catches up to it, never the reverse.
    An attempt that believes it committed while the head says otherwise did not
    commit, however confident the local row is: the swap is the only event that
    makes a snapshot real.
    """
    if record.state.is_terminal:
        return ReconcileVerdict(
            action=ReconcileAction.NONE,
            detail=f"attempt is already {record.state.value}",
            record_id=record.id,
        )

    published = head is not None and head.snapshot_id == record.snapshot_id

    if published:
        # The case the outbox exists for: the swap succeeded and the process
        # died before writing it down. Publishing again would claim a second
        # sequence for one piece of work.
        return ReconcileVerdict(
            action=ReconcileAction.ACKNOWLEDGE,
            detail=(
                f"the head carries snapshot {record.snapshot_id} at sequence "
                f"{head.commit_sequence}: this attempt committed and did not record it"
            ),
            record_id=record.id,
        )

    if record.state is PublicationState.COMMITTED:
        # The local row says committed and the head disagrees. The head wins:
        # either the swap never actually landed, or a later publication replaced
        # it. Both mean this snapshot is not what the library serves.
        return ReconcileVerdict(
            action=ReconcileAction.ABANDON,
            detail=(
                f"this attempt recorded a commit of snapshot {record.snapshot_id} and "
                "the head does not carry it; the head is the truth, so the attempt is "
                "abandoned rather than replayed"
            ),
            record_id=record.id,
        )

    if head is not None and head.commit_sequence >= record.commit_sequence:
        return ReconcileVerdict(
            action=ReconcileAction.ABANDON,
            detail=(
                f"this attempt claims sequence {record.commit_sequence} and the head is "
                f"already at {head.commit_sequence}: somebody else published while it "
                "was interrupted"
            ),
            record_id=record.id,
        )

    return ReconcileVerdict(
        action=ReconcileAction.RESUME,
        detail=(
            f"the head is unchanged, so this attempt can still claim sequence "
            f"{record.commit_sequence}"
        ),
        record_id=record.id,
    )
