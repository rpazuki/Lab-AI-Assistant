"""Immutable, content-addressed object storage."""

from lab_assistant.adapters.object_store.filesystem import (
    FilesystemObjectStore,
    InMemoryObjectStore,
    StoredObjectRecord,
)

__all__ = ["FilesystemObjectStore", "InMemoryObjectStore", "StoredObjectRecord"]
