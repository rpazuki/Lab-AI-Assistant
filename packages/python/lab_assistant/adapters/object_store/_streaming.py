"""Spooling a stream to a digest, without holding it in memory.

Content-addressed storage has an awkward property: the key *is* the digest, and
the digest is only known once every byte has been read. Something therefore has
to hold the payload between the last byte and the first write.

Holding it in RAM is what the byte-oriented API does, and it is why a large
export or a whole-library shard is a memory spike rather than a transfer. This
spools instead: small payloads stay in memory, larger ones go to a temporary
file, and the digest and length are computed on the way past. Memory is bounded
by the spool threshold rather than by the size of the thing being stored.

The size limit is enforced *while* reading, not after. A limit checked at the end
has already allocated whatever it was meant to prevent, which makes it a report
rather than a control — and the thing it is meant to stop is precisely a source
that keeps sending.

Every adapter shares this, because the limit and the digest have to behave
identically across them. An in-memory store that accepted a payload the S3 store
would refuse is a test that proves the wrong thing.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import IO, Any

from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = [
    "SPOOL_THRESHOLD_BYTES",
    "STREAM_CHUNK_BYTES",
    "SpooledPayload",
    "bytes_stream",
    "spool_stream",
]

#: Payloads at or below this stay in memory; larger ones spool to a temporary
#: file. Chosen to match the usual multipart threshold, so a payload that is
#: small enough to upload in one request is also small enough not to touch disk.
SPOOL_THRESHOLD_BYTES = 8 * 1024 * 1024

#: Read and yield size. Large enough that a big transfer is not a syscall storm,
#: small enough that one chunk is never the memory problem.
STREAM_CHUNK_BYTES = 1024 * 1024


@dataclass(slots=True)
class SpooledPayload:
    """A fully read payload, its identity, and where it is being held.

    ``file`` is positioned at the start and belongs to the caller, who must close
    it. It is a context manager for exactly that reason.
    """

    sha256: str
    byte_length: int
    file: IO[bytes]

    def __enter__(self) -> SpooledPayload:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.file.close()

    def read_all(self) -> bytes:
        """The whole payload, for the adapters that still need bytes."""
        self.file.seek(0)
        data = self.file.read()
        self.file.seek(0)
        return data


async def spool_stream(
    source: AsyncIterator[bytes] | Iterable[bytes] | bytes,
    *,
    max_bytes: int | None = None,
    spool_threshold: int = SPOOL_THRESHOLD_BYTES,
) -> SpooledPayload:
    """Read ``source`` once, computing its digest and length as it goes.

    Raises ``TOO_LARGE`` the moment the running total passes ``max_bytes``,
    leaving the rest of the source unread.
    """
    digest = hashlib.sha256()
    total = 0
    # Deliberately outlives this function: the payload is what the caller came
    # for, and SpooledPayload is the context manager that closes it.
    handle: IO[bytes] = SpooledTemporaryFile(  # noqa: SIM115
        max_size=spool_threshold
    )  # type: ignore[assignment]
    try:
        async for chunk in _aiter(source):
            if not chunk:
                continue
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise LabAssistantError(
                    f"object exceeds the configured limit of {max_bytes} bytes",
                    context={"limit_bytes": max_bytes, "read_bytes": total},
                ).with_class(ErrorClass.TOO_LARGE)
            digest.update(chunk)
            handle.write(chunk)
    except BaseException:
        handle.close()
        raise
    handle.seek(0)
    return SpooledPayload(sha256=digest.hexdigest(), byte_length=total, file=handle)


async def bytes_stream(
    data: bytes, *, chunk_size: int = STREAM_CHUNK_BYTES
) -> AsyncIterator[bytes]:
    """Present bytes already in memory as a stream, for one code path."""
    for start in range(0, len(data), chunk_size):
        yield data[start : start + chunk_size]


async def _aiter(
    source: AsyncIterator[bytes] | Iterable[bytes] | bytes,
) -> AsyncIterator[bytes]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        async for chunk in bytes_stream(bytes(source)):
            yield chunk
        return
    if hasattr(source, "__aiter__"):
        async for chunk in source:  # type: ignore[union-attr]
            yield chunk
        return
    for chunk in source:  # type: ignore[union-attr]
        yield chunk
