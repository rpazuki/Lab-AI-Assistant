"""Compare-and-swap for the library head, on each supported backend.

One well-known key, changed exactly once per publication, with the previous
version as the precondition. The preconditions already exist on every backend
this application supports — they are the same primitives the object stores use
to stop a stale writer resurrecting a tombstone — so this is a narrow surface
over each, not a new mechanism.

What differs per backend is only the name of the token:

- **S3** uses an ETag, with `If-None-Match: *` to create and `If-Match` to swap;
- **GCS** uses a generation, with `if_generation_match=0` and `=<generation>`;
- **the filesystem** has no provider to ask, so the token is a digest of the
  current bytes and the swap is done holding an exclusive lock. That is a
  single-host guarantee, which is exactly what ADR-0022 scopes the filesystem
  profile to. It is not a multi-host writer protocol and must not be used as
  one.

A failed precondition is `CONFLICT` on all three, and the caller is expected to
stop rather than re-read and retry — see `domain/publication.py` for why that
matters more than it looks.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import tempfile
from pathlib import Path
from typing import Any

from lab_assistant.application.ports import VersionedPayload
from lab_assistant.contracts.canonical import sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = [
    "FilesystemControlStore",
    "GcsControlStore",
    "MemoryControlStore",
    "S3ControlStore",
]


def _conflict(key: str, detail: str) -> LabAssistantError:
    return LabAssistantError(
        f"control record {key} failed its precondition: {detail}",
        context={"key": key, "hint": "do not retry by adopting the version you just read"},
    ).with_class(ErrorClass.CONFLICT)


class MemoryControlStore:
    """In-memory, with the same precondition semantics.

    A fake that permitted an unconditional overwrite would make every
    concurrency test pass while proving nothing, so this one refuses exactly
    where a provider would.
    """

    def __init__(self) -> None:
        self._records: dict[str, tuple[bytes, str]] = {}

    async def read(self, key: str) -> VersionedPayload | None:
        found = self._records.get(key)
        if found is None:
            return None
        data, version = found
        return VersionedPayload(data=data, version=version)

    async def create(self, key: str, data: bytes) -> str:
        if key in self._records:
            raise _conflict(key, "it already exists")
        version = sha256_bytes(data)
        self._records[key] = (data, version)
        return version

    async def compare_and_swap(self, key: str, data: bytes, *, expected_version: str) -> str:
        found = self._records.get(key)
        if found is None:
            raise _conflict(key, "it does not exist")
        if found[1] != expected_version:
            raise _conflict(key, f"it is at {found[1][:12]}…, not {expected_version[:12]}…")
        version = sha256_bytes(data)
        self._records[key] = (data, version)
        return version

    async def delete(self, key: str) -> bool:
        """Remove a key. ``False`` when it was already gone."""
        return self._records.pop(key, None) is not None


class FilesystemControlStore:
    """Single-host compare-and-swap, under an exclusive lock.

    The version token is a digest of the current bytes: there is no provider to
    issue one, and a digest has the property that matters — it changes when the
    content does.

    The lock makes read-compare-write atomic *on this host*. Two processes on
    one VM are safe; two VMs sharing an NFS mount are not, because `flock` over
    NFS is a promise the network does not keep. ADR-0022 scopes the filesystem
    profile to one VM, and this does not widen it.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._root / key

    def _lock_path(self, key: str) -> Path:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.parent / f".{path.name}.lock"

    def _locked_write(self, key: str, data: bytes, *, expected_version: str | None) -> str:
        path = self._path(key)
        with self._lock_path(key).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                current = path.read_bytes() if path.is_file() else None
                if expected_version is None:
                    if current is not None:
                        raise _conflict(key, "it already exists")
                else:
                    if current is None:
                        raise _conflict(key, "it does not exist")
                    actual = sha256_bytes(current)
                    if actual != expected_version:
                        raise _conflict(
                            key, f"it is at {actual[:12]}…, not {expected_version[:12]}…"
                        )
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as scratch:
                        temporary = Path(scratch.name)
                        scratch.write(data)
                        scratch.flush()
                        os.fsync(scratch.fileno())
                    os.replace(temporary, path)
                    temporary = None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return sha256_bytes(data)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    async def read(self, key: str) -> VersionedPayload | None:
        path = self._path(key)
        if not path.is_file():
            return None
        data = await asyncio.to_thread(path.read_bytes)
        return VersionedPayload(data=data, version=sha256_bytes(data))

    async def create(self, key: str, data: bytes) -> str:
        return await asyncio.to_thread(self._locked_write, key, data, expected_version=None)

    async def compare_and_swap(self, key: str, data: bytes, *, expected_version: str) -> str:
        return await asyncio.to_thread(
            self._locked_write, key, data, expected_version=expected_version
        )

    async def delete(self, key: str) -> bool:
        """Remove a key under its lock. ``False`` when it was already gone.

        Only the filesystem and memory stores delete. A bucket's deletion
        authority belongs to a separate principal and its own lifecycle rules
        (ADR-0028), so the bucket adapters deliberately offer no ``delete``.
        """

        def remove() -> bool:
            path = self._path(key)
            with self._lock_path(key).open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    if not path.is_file():
                        return False
                    path.unlink()
                    return True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

        return await asyncio.to_thread(remove)


class S3ControlStore:
    """ETag preconditions, over the same SDK the object store uses."""

    def __init__(self, config: Any, *, client: Any | None = None) -> None:
        self._config = config
        self._client = client if client is not None else config.build_client()

    def _key(self, key: str) -> str:
        return f"{getattr(self._config, 'prefix', '')}{key}"

    async def read(self, key: str) -> VersionedPayload | None:
        from lab_assistant.adapters.object_store.s3 import _translate

        full = self._key(key)

        def call() -> tuple[bytes, str | None]:
            response = self._client.get_object(Bucket=self._config.bucket, Key=full)
            return response["Body"].read(), response.get("ETag")

        try:
            data, version = await asyncio.to_thread(call)
        except Exception as exc:
            translated = _translate(exc, operation="get_object", key=full)
            if translated.error_class is ErrorClass.NOT_FOUND:
                return None
            raise translated from exc
        return VersionedPayload(data=data, version=version)

    async def _put(self, key: str, data: bytes, arguments: dict[str, Any]) -> str | None:
        from lab_assistant.adapters.object_store.s3 import _translate

        full = self._key(key)

        def call() -> Any:
            return self._client.put_object(
                Bucket=self._config.bucket,
                Key=full,
                Body=data,
                ContentType="application/json",
                **arguments,
            )

        try:
            response = await asyncio.to_thread(call)
        except Exception as exc:
            raise _translate(exc, operation="put_object", key=full) from exc
        return response.get("ETag") if isinstance(response, dict) else None

    async def create(self, key: str, data: bytes) -> str | None:
        return await self._put(key, data, {"IfNoneMatch": "*"})

    async def compare_and_swap(self, key: str, data: bytes, *, expected_version: str) -> str | None:
        return await self._put(key, data, {"IfMatch": expected_version})


class GcsControlStore:
    """Generation preconditions, over the same SDK the object store uses."""

    def __init__(self, config: Any, *, client: Any | None = None) -> None:
        self._config = config
        self._client = client if client is not None else config.build_client()

    def _blob(self, key: str) -> Any:
        bucket = self._client.bucket(self._config.bucket)
        return bucket.blob(f"{getattr(self._config, 'prefix', '')}{key}")

    async def read(self, key: str) -> VersionedPayload | None:
        from lab_assistant.adapters.object_store.gcs import _translate

        blob = self._blob(key)

        def call() -> tuple[bytes, str | None]:
            payload = blob.download_as_bytes()
            return payload, str(blob.generation) if blob.generation is not None else None

        try:
            data, version = await asyncio.to_thread(call)
        except Exception as exc:
            translated = _translate(exc, operation="download_as_bytes", key=key)
            if translated.error_class is ErrorClass.NOT_FOUND:
                return None
            raise translated from exc
        return VersionedPayload(data=data, version=version)

    async def _upload(self, key: str, data: bytes, generation: int) -> str | None:
        from lab_assistant.adapters.object_store.gcs import _translate

        blob = self._blob(key)

        def call() -> None:
            blob.upload_from_string(
                data, content_type="application/json", if_generation_match=generation
            )

        try:
            await asyncio.to_thread(call)
        except Exception as exc:
            raise _translate(exc, operation="upload_from_string", key=key) from exc
        return str(blob.generation) if blob.generation is not None else None

    async def create(self, key: str, data: bytes) -> str | None:
        return await self._upload(key, data, 0)

    async def compare_and_swap(self, key: str, data: bytes, *, expected_version: str) -> str | None:
        try:
            generation = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise _conflict(key, f"{expected_version!r} is not a GCS generation") from exc
        return await self._upload(key, data, generation)
