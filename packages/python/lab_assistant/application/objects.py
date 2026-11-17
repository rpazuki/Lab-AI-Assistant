"""Registering stored bytes in the database.

``ObjectStore.put`` writes bytes and returns their address. It does not write the
``objects`` row, and it must not: that row belongs to the caller's transaction,
beside the asset, rendition, source request, or release manifest that points at
it. An adapter registering on the caller's behalf would need its own
transaction, and the dependent row would then commit against a registration that
could still roll back.

The order is deliberate and is the one the architecture requires: store the bytes
first, register and commit second. A crash between them leaves a payload nobody
references, which a reachability sweep can reclaim. The reverse order leaves a
committed reference to bytes that were never written — a citation into nothing,
and no sweep can repair it.

``register_object`` is called at every ``object_store.put`` site in this layer.
``tests/contract/test_object_registration.py`` reads the AST and fails if a new
one appears without it, because the whole class of bug here is a write path
somebody forgot.
"""

from __future__ import annotations

from lab_assistant.application.ports import StoredObject, UnitOfWork
from lab_assistant.domain.objects import ObjectRecord

__all__ = ["object_record", "register_object"]


def object_record(stored: StoredObject) -> ObjectRecord:
    """The registry row describing an object the store has just accepted."""
    return ObjectRecord(
        id=stored.id,
        workspace_id=stored.workspace_id,
        sha256=stored.sha256,
        byte_length=stored.byte_length,
        media_type=stored.media_type,
        object_class=stored.object_class,
        storage_key=stored.key,
        tombstoned=getattr(stored, "tombstoned", False),
        created_at=stored.created_at,
    )


async def register_object(uow: UnitOfWork, stored: StoredObject) -> ObjectRecord:
    """Record ``stored`` in the registry inside the caller's open transaction."""
    record = object_record(stored)
    await uow.objects.register(record)
    return record
