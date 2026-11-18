"""The statement a drained publisher hands to its successor (phase 4).

A handover moves the right to publish a library from one installation to
another. The archive's two heads are the authority — whoever they name at the
current epoch may publish — so the handover is, mechanically, a conditional
rewrite of both heads to a new owner at the next epoch. What makes it safe is
everything around that rewrite, and most of it is this statement.

The old publisher, drained, writes down what it last published: the catalog
sequence and the policy sequence its heads carried when it stopped. The new
publisher promotes only if the heads still say exactly that. A publisher that
published again after draining has invalidated its own statement, and the
promotion refuses rather than transferring a library whose last state nobody
has recovered.

The statement is not signed and does not need to be: it confers nothing. It is
compared with the heads, which are the authority, and a forged statement that
disagreed with them would be refused for disagreeing.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

__all__ = ["HANDOVER_FORMAT_VERSION", "HandoverStatement"]

HANDOVER_FORMAT_VERSION = "1.0"


class HandoverStatement(BaseModel):
    """What the outgoing publisher last published, as it stopped."""

    model_config = {"frozen": True}

    format_version: str = HANDOVER_FORMAT_VERSION
    library_id: UUID
    from_installation_id: UUID
    writer_epoch: int
    #: The catalog head as the drain read it; ``None`` when nothing was published.
    catalog_commit_sequence: int | None = None
    catalog_snapshot_id: UUID | None = None
    #: The policy head as the drain read it.
    policy_sequence: int | None = None
    policy_checkpoint_digest: str | None = None
    drained_at: datetime
    #: True when the statement was written by an operator for a publisher that
    #: could not be reached, from the heads alone. The operator asserted the old
    #: publisher is fenced; nothing else in the statement can prove it.
    asserted_fenced: bool = False
