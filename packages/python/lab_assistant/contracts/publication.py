"""The archive head, and the outbox that gets a snapshot to it (phase 2).

Publishing a snapshot is two things that must not be confused: writing bytes
into the archive, which is slow and repeatable, and moving the head, which is
instantaneous and must happen exactly once. Everything here exists to keep that
line sharp.

**The head is the only thing that makes a snapshot real.** Shards written into
the archive and not referenced by the head are invisible: a reader resolves the
head, follows it to a snapshot, and never sees anything else. So a crash during
the upload leaves litter, not a half-published library — which is why the upload
comes first and the head moves last.

**The head is a control record, not a pointer.** It carries the owner, the
writer epoch and the commit sequence together, so a stale writer's
compare-and-swap cannot succeed even when its snapshot is perfectly valid. One
library, one writer, enforced by the storage layer's precondition rather than by
everyone agreeing to behave.

**The outbox is what makes a crash recoverable.** It records the publisher's
intent before the head moves and its knowledge after, so a process that died in
between can tell "I was about to publish" from "I published and did not get to
write it down". Without it, the only safe move after an uncertain crash is to
do nothing, and the only convenient one is to publish again — which is how a
library ends up with two snapshots claiming the same sequence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

__all__ = [
    "HEAD_FORMAT_VERSION",
    "HEAD_LOCATION",
    "HeadDescriptor",
    "PublicationRecord",
    "PublicationState",
]

#: Where the head lives inside a library's namespace. A fixed, boring location:
#: a reader with a GET-only credential must be able to find it without listing.
HEAD_LOCATION = "control/head.json"

HEAD_FORMAT_VERSION = "1.0"


class PublicationState(StrEnum):
    """How far one publication attempt got.

    The states exist to make an interrupted attempt *interpretable*, so each
    one answers a different recovery question.
    """

    #: Intent recorded. Nothing is in the archive yet, and nothing is visible.
    PREPARED = "prepared"
    #: The snapshot is in the archive and validates there. Still invisible: no
    #: head references it. A publisher that dies here has littered, not
    #: published.
    VERIFIED = "verified"
    #: The head compare-and-swap succeeded. The snapshot is live. A publisher
    #: that dies between the swap and writing this row is the case reconciliation
    #: exists for — the head is the truth, and the outbox catches up to it.
    COMMITTED = "committed"
    #: Local bookkeeping caught up with the head. Nothing further to do.
    ACKNOWLEDGED = "acknowledged"
    #: Abandoned, with the reason. Kept rather than deleted: "we tried to
    #: publish and it was refused" is something an operator needs to find.
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (PublicationState.ACKNOWLEDGED, PublicationState.FAILED)

    @property
    def is_visible(self) -> bool:
        """Has the head moved? The only question a reader's view depends on."""
        return self in (PublicationState.COMMITTED, PublicationState.ACKNOWLEDGED)


class HeadDescriptor(BaseModel):
    """The control record: what this library currently publishes, and by whom.

    Owner, epoch and sequence travel together on purpose. A head that carried
    only the snapshot id would let any writer with the current version token
    advance it, which is precisely what a fenced handover must prevent.
    """

    model_config = {"frozen": True}

    format_version: str = HEAD_FORMAT_VERSION
    library_id: UUID
    #: The installation entitled to advance this head. Not advisory: a
    #: compare-and-swap from anyone else is refused by `domain/publication.py`
    #: before the storage layer is asked.
    owner_installation_id: UUID
    #: Incremented only by a deliberate, operator-driven handover. A writer
    #: whose epoch is behind this has been fenced and must stop, not retry.
    writer_epoch: int
    #: Monotonic per library. Two snapshots claiming one sequence is the
    #: duplicate publication the outbox exists to prevent.
    commit_sequence: int
    snapshot_id: UUID
    #: The digest of the snapshot manifest this head points at, so a reader can
    #: tell it fetched the manifest the head meant rather than one that happened
    #: to be at the same key.
    snapshot_manifest_digest: str
    updated_at: datetime | None = None
    #: Set when a handover moved the epoch, as evidence of who took over.
    fenced_predecessor_id: UUID | None = None

    def digest(self) -> str:
        return sha256_bytes(canonical_json(self.model_dump(mode="python")))

    def serialise(self) -> bytes:
        """Canonical bytes, which is what is actually written and compared."""
        return canonical_json(self.model_dump(mode="python"))


class PublicationRecord(BaseModel):
    """One attempt to move the head, and everything recovery needs to judge it."""

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    library_id: UUID
    snapshot_id: UUID
    snapshot_manifest_digest: str
    state: PublicationState
    #: The epoch this publisher believed it held when it began. Compared with
    #: the head at commit time; a mismatch is a fenced writer, not a retry.
    writer_epoch: int
    #: The sequence this attempt intends to claim.
    commit_sequence: int
    #: The storage version token the head carried when this attempt read it.
    #: The compare-and-swap is against *this*, so a head that moved underneath
    #: fails the swap rather than overwriting whoever moved it.
    expected_head_version: str | None = None
    #: What the head carried after a successful swap, as recovery evidence.
    committed_head_version: str | None = None
    detail: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
