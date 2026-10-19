"""Content-addressed object storage.

Key convention (target architecture §4.2)::

    sha256/ab/cd/<full-digest>

Object names encode nothing else. Not a DOI, not a PMID, not a user filename, not
a run directory — those change, collide, or disclose. Logical metadata lives in
PostgreSQL and points here.

Objects are immutable. There is no overwrite operation, and deletion is a
tombstone plus an audited purge, never a file rewrite.

The filesystem backend supports a single POSIX VM with a shared persistent
volume. File locks coordinate API and worker processes; off-VM backups are
required because the VM and its database share a failure domain.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from uuid import UUID, uuid5

from lab_assistant.contracts.canonical import sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = ["FilesystemObjectStore", "InMemoryObjectStore", "StoredObjectRecord"]

#: Namespace so an object's id is a deterministic function of its digest and
#: workspace. The same bytes in the same workspace always resolve to one id.
_OBJECT_NAMESPACE = UUID("6f2b0f30-3ac7-4a05-9a17-6e1c6a3f0f11")


@dataclass(frozen=True, slots=True)
class StoredObjectRecord:
    id: UUID
    workspace_id: UUID | None
    sha256: str
    byte_length: int
    media_type: str
    object_class: str
    created_at: datetime
    key: str
    tombstoned: bool = False


def _object_id(workspace_id: UUID | None, digest: str) -> UUID:
    return uuid5(_OBJECT_NAMESPACE, f"{workspace_id or 'shared'}:{digest}")


def _key_for(digest: str) -> str:
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"


def _synchronised(method):
    @wraps(method)
    async def call(self, *args, **kwargs):
        with self._locked():
            self._load_index()
            return await method(self, *args, **kwargs)

    return call


class FilesystemObjectStore:
    """Immutable local object store used by development and single-VM profiles."""

    def __init__(self, root: Path, *, clock=None) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._index: dict[UUID, StoredObjectRecord] = {}
        self._index_offset = 0
        with self._locked():
            self._load_index()

    @contextmanager
    def _locked(self):
        with (self._root / ".store.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @property
    def root(self) -> Path:
        return self._root

    def _index_path(self) -> Path:
        return self._root / "index.jsonl"

    def _load_index(self) -> None:
        path = self._index_path()
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(self._index_offset)
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                record = StoredObjectRecord(
                    id=UUID(row["id"]),
                    workspace_id=UUID(row["workspace_id"]) if row["workspace_id"] else None,
                    sha256=row["sha256"],
                    byte_length=row["byte_length"],
                    media_type=row["media_type"],
                    object_class=row["object_class"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    key=row["key"],
                    tombstoned=row.get("tombstoned", False),
                )
                self._index[record.id] = record
            self._index_offset = handle.tell()

    def _append_index(self, record: StoredObjectRecord) -> None:
        with self._index_path().open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "id": str(record.id),
                        "workspace_id": str(record.workspace_id) if record.workspace_id else None,
                        "sha256": record.sha256,
                        "byte_length": record.byte_length,
                        "media_type": record.media_type,
                        "object_class": record.object_class,
                        "created_at": record.created_at.isoformat(),
                        "key": record.key,
                        "tombstoned": record.tombstoned,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

            handle.flush()
            os.fsync(handle.fileno())

    @_synchronised
    async def put(
        self,
        data: bytes,
        *,
        workspace_id: UUID | None,
        object_class: str,
        media_type: str = "application/octet-stream",
    ) -> StoredObjectRecord:
        digest = sha256_bytes(data)
        object_id = _object_id(workspace_id, digest)
        existing = self._index.get(object_id)
        if existing is not None:
            # Registering the same bytes twice is a no-op, never an overwrite.
            return existing
        key = _key_for(digest)
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        record = StoredObjectRecord(
            id=object_id,
            workspace_id=workspace_id,
            sha256=digest,
            byte_length=len(data),
            media_type=media_type,
            object_class=object_class,
            created_at=self._clock(),
            key=key,
        )
        self._index[object_id] = record
        self._append_index(record)
        return record

    @_synchronised
    async def get(self, object_id: UUID) -> bytes:
        record = self._index.get(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        if record.tombstoned:
            raise FileNotFoundError(f"object {object_id} has been tombstoned")
        payload = (self._root / record.key).read_bytes()
        if sha256_bytes(payload) != record.sha256:
            raise LabAssistantError(
                "stored object does not match its recorded digest",
                context={"object_id": str(object_id), "key": record.key},
            ).with_class(ErrorClass.INVALID_CONTENT)
        return payload

    async def get_by_digest(self, sha256: str, *, workspace_id: UUID | None) -> bytes | None:
        object_id = _object_id(workspace_id, sha256)
        try:
            return await self.get(object_id)
        except FileNotFoundError:
            return None

    @_synchronised
    async def head(self, object_id: UUID) -> StoredObjectRecord | None:
        return self._index.get(object_id)

    @_synchronised
    async def verify(self, object_id: UUID) -> bool:
        record = self._index.get(object_id)
        if record is None:
            return False
        path = self._root / record.key
        if not path.is_file():
            return False
        return sha256_bytes(path.read_bytes()) == record.sha256

    @_synchronised
    async def signed_url(self, object_id: UUID, *, expires_seconds: int) -> str:
        record = self._index.get(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        # A local store has no signing service; the API serves bytes behind its
        # own authorisation check instead of handing out a filesystem path.
        return f"/api/v1/objects/{object_id}?expires_in={expires_seconds}"

    @_synchronised
    async def tombstone(self, object_id: UUID, *, reason: str) -> None:
        record = self._index.get(object_id)
        if record is None:
            return
        tombstoned = replace(record, tombstoned=True)
        self._index[object_id] = tombstoned
        self._append_index(tombstoned)
        (self._root / f"{record.key}.tombstone").write_text(reason, encoding="utf-8")


class InMemoryObjectStore:
    """Object store for tests. Same immutability rules, no filesystem."""

    def __init__(self, *, clock=None) -> None:
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._objects: dict[UUID, bytes] = {}
        self._records: dict[UUID, StoredObjectRecord] = {}

    async def put(
        self,
        data: bytes,
        *,
        workspace_id: UUID | None,
        object_class: str,
        media_type: str = "application/octet-stream",
    ) -> StoredObjectRecord:
        digest = sha256_bytes(data)
        object_id = _object_id(workspace_id, digest)
        if object_id in self._records:
            return self._records[object_id]
        record = StoredObjectRecord(
            id=object_id,
            workspace_id=workspace_id,
            sha256=digest,
            byte_length=len(data),
            media_type=media_type,
            object_class=object_class,
            created_at=self._clock(),
            key=_key_for(digest),
        )
        self._objects[object_id] = data
        self._records[object_id] = record
        return record

    async def get(self, object_id: UUID) -> bytes:
        record = self._records.get(object_id)
        if record is None or record.tombstoned:
            raise FileNotFoundError(f"object {object_id} is not available")
        return self._objects[object_id]

    async def get_by_digest(self, sha256: str, *, workspace_id: UUID | None) -> bytes | None:
        object_id = _object_id(workspace_id, sha256)
        return self._objects.get(object_id)

    async def head(self, object_id: UUID) -> StoredObjectRecord | None:
        return self._records.get(object_id)

    async def verify(self, object_id: UUID) -> bool:
        record = self._records.get(object_id)
        if record is None:
            return False
        return sha256_bytes(self._objects[object_id]) == record.sha256

    async def signed_url(self, object_id: UUID, *, expires_seconds: int) -> str:
        if object_id not in self._records:
            raise FileNotFoundError(f"object {object_id} is not registered")
        return f"/api/v1/objects/{object_id}?expires_in={expires_seconds}"

    async def tombstone(self, object_id: UUID, *, reason: str) -> None:
        record = self._records.get(object_id)
        if record is None:
            return
        self._records[object_id] = replace(record, tombstoned=True)

    def corrupt_for_test(self, object_id: UUID, data: bytes) -> None:
        """Deliberately break stored bytes so integrity detection can be tested."""
        self._objects[object_id] = data
