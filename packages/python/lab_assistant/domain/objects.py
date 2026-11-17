"""The registry row for one immutable payload in content-addressed storage.

An object store holds bytes. This record is the database's statement that those
bytes exist, what they are, and which workspace they belong to. Twelve columns
across the schema are foreign keys into ``objects`` — an asset, a rendition, a
source response, a canonical record, a release manifest — and every one of them
depends on this row being written in the same transaction as the row that points
at it. Without it the reference is a promise the database cannot keep, and
PostgreSQL rejects the dependent write rather than storing a dangling pointer.

Registration is insert-if-absent, never an update. Re-storing identical bytes
must not clear a tombstone, and the first writer's ``object_class`` is a label on
one use of a payload, not a claim that no other use exists: a payload can serve
more than one artifact role, and the role belongs to the referencing record.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

__all__ = ["ObjectRecord"]


class ObjectRecord(BaseModel):
    """One registered object: its address, the identity of its bytes, its class.

    ``storage_key`` is where the configured backend put the bytes, and it is a
    per-backend address rather than an identity: ``id`` and ``sha256`` survive a
    move from the filesystem to a bucket, the key need not.
    """

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID | None = None
    sha256: str
    byte_length: int
    media_type: str
    object_class: str
    storage_key: str
    tombstoned: bool = False
    tombstone_reason: str | None = None
    created_at: datetime | None = None
