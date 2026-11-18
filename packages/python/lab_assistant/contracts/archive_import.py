"""What a reader recorded about the snapshots it imported (phase 3).

One row per import attempt. The newest active one is the reader's durable memory
of where it is — which snapshot it serves, at which head sequence, under which
policy checkpoint — and that memory is what turns a replayed or rolled-back head
into a refusal rather than a quiet step backwards (scenario S65). It survives a
restart because it is in the database, not in a process.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = ["ImportRecord", "ImportState"]


class ImportState(StrEnum):
    #: Imported and serving. The newest active row is the reader's position.
    ACTIVE = "active"
    #: Refused or failed; the previous active import still serves.
    FAILED = "failed"


class ImportRecord(BaseModel):
    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    library_id: UUID
    snapshot_id: UUID
    #: The catalog head's sequence when the snapshot was fetched.
    commit_sequence: int
    policy_sequence: int | None = None
    policy_checkpoint_digest: str | None = None
    state: ImportState
    detail: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    imported_at: datetime
