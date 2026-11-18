"""Google Cloud Storage, natively (ADR-0024).

Same contract as the filesystem and S3 stores, same key convention::

    sha256/ab/cd/<full-digest>        immutable content
    records/<object-id>.json         one metadata record per (workspace, digest)

A configured prefix is prepended to both when the bucket is shared, and it is
*not* part of the stored key: ``StoredObjectRecord.key`` stays logical, so the
database survives a move to another bucket, prefix or provider.

Three differences from the S3 adapter are provider facts rather than choices,
and each one is load-bearing:

- **Preconditions are generations.** A create carries ``if_generation_match=0``
  — GCS's "only if absent" — and a record update carries the generation it read,
  which is the compare-and-swap. They are first-class here, so unlike S3 there
  is no feature detection and no unsupported-endpoint fallback: an endpoint that
  is GCS has them.
- **The provider cannot verify SHA-256.** GCS checksums are CRC32C and MD5. The
  adapter sends an MD5 so a transfer corrupted in flight is rejected at the
  door, and the application's SHA-256 stays the identity check on every read.
  MD5 is transport integrity here and nothing else; identity is the digest in
  the key and in the database.
- **Signing needs a key or `signBlob`.** Where the running identity can do
  neither, ``signed_url`` raises rather than handing out a URL that will be
  refused when somebody clicks it.

Everything the other adapters guarantee is guaranteed here. Missing is not
denied: a 404 means absent and a 403 means storage answered and refused, because
conflating them lets a reader with wrong permissions conclude the evidence is
gone and pay a source again for bytes already in the bucket. Writes are
conditional, so a stale writer cannot resurrect a tombstoned record. Reads are
digest-verified, because a provider checksum is a transport check and not the
content hash this corpus cites.

Nothing here decides *whether* bytes may be stored. That is the rights policy's
job, evaluated long before an object reaches a store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from lab_assistant.adapters.object_store._streaming import (
    SPOOL_THRESHOLD_BYTES,
    STREAM_CHUNK_BYTES,
    SpooledPayload,
    enforce_limit,
    spool_stream,
)
from lab_assistant.adapters.object_store.filesystem import (
    StoredObjectRecord,
    _key_for,
    _object_id,
)
from lab_assistant.contracts.canonical import sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = ["GcsObjectStore", "GcsStoreConfig"]

#: GCS's own "precondition failed". The SDK raises a distinct class, but an
#: emulator or a proxy may surface the bare status instead.
_PRECONDITION_STATUS = frozenset({409, 412})


class GcsStoreConfig:
    """Bucket, project, endpoint and credentials, validated at construction.

    A store that cannot work refuses to exist rather than failing on the first
    upload — which would be during an acquisition run, hours after the deploy
    that broke it (ADR-0017, kept through ADR-0023 and ADR-0024).

    Credentials are optional *here* but not absent: leaving them unset selects
    the SDK's application-default chain — environment, gcloud config, metadata
    server, workload identity — which is how a deployment gets short-lived
    credentials that refresh inside a long job.
    """

    __slots__ = (
        "bucket",
        "credentials_path",
        "endpoint",
        "max_object_bytes",
        "multipart_threshold",
        "prefix",
        "project",
        "timeout",
    )

    def __init__(
        self,
        *,
        bucket: str | None,
        project: str | None = None,
        endpoint: str | None = None,
        credentials_path: str | None = None,
        prefix: str = "",
        multipart_threshold: int = SPOOL_THRESHOLD_BYTES,
        max_object_bytes: int | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not bucket:
            raise LabAssistantError(
                "GCS object store needs a bucket: set LAB_OBJECT_STORE_BUCKET",
                context={"kind": "gcs"},
            ).with_class(ErrorClass.INVALID_CONTENT)
        if prefix and prefix.startswith("/"):
            raise LabAssistantError(
                "LAB_OBJECT_STORE_PREFIX must not start with '/': it is a key prefix, not a path",
                context={"prefix": prefix},
            ).with_class(ErrorClass.INVALID_CONTENT)
        self.bucket = bucket
        self.project = project
        self.endpoint = endpoint
        self.credentials_path = credentials_path
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self.multipart_threshold = multipart_threshold
        self.max_object_bytes = max_object_bytes
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: Any) -> GcsStoreConfig:
        return cls(
            bucket=settings.object_store_bucket,
            project=getattr(settings, "object_store_project", None),
            endpoint=getattr(settings, "object_store_endpoint", None),
            credentials_path=getattr(settings, "object_store_credentials_path", None),
            prefix=getattr(settings, "object_store_prefix", "") or "",
            multipart_threshold=getattr(
                settings, "object_store_multipart_threshold_bytes", SPOOL_THRESHOLD_BYTES
            ),
            max_object_bytes=getattr(settings, "object_store_max_object_bytes", None) or None,
        )

    def build_client(self) -> Any:
        """Construct the SDK client, refusing when no credential resolves.

        An explicit service-account file wins when configured. Otherwise the
        application-default chain is used, and a chain that resolves to nothing
        is a startup failure rather than a runtime one — except against a local
        emulator, which authenticates nobody and must not be made to.
        """
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import storage

        if self.endpoint:
            # An emulator. Anonymous on purpose: resolving the ambient
            # credential chain here would make a developer's own Google
            # identity the thing a local test runs as.
            return storage.Client(
                project=self.project or "lab-assistant-emulator",
                credentials=AnonymousCredentials(),
                client_options={"api_endpoint": self.endpoint},
            )

        if self.credentials_path:
            return storage.Client.from_service_account_json(
                self.credentials_path, project=self.project
            )

        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        try:
            credentials, discovered = google.auth.default()
        except DefaultCredentialsError as exc:
            raise LabAssistantError(
                "GCS object store found no credentials: set "
                "LAB_OBJECT_STORE_CREDENTIALS_PATH, or give the deployment a workload "
                "identity the application-default chain can resolve",
                context={"bucket": self.bucket, "project": self.project},
            ).with_class(ErrorClass.AUTHENTICATION_REQUIRED) from exc
        return storage.Client(project=self.project or discovered, credentials=credentials)


def _translate(exc: Exception, *, operation: str, key: str | None) -> LabAssistantError:
    """Map one provider failure onto the taxonomy, deliberately.

    The default is ``PROVIDER_ERROR`` — retryable — rather than anything that
    reads as an answer about the object. An unrecognised failure is a failure,
    not an absence.
    """
    from google.api_core import exceptions as api

    context: dict[str, Any] = {"operation": operation}
    if key is not None:
        context["key"] = key

    if isinstance(exc, api.NotFound):
        return LabAssistantError(
            f"object store has no {key or 'object'}", context=context
        ).with_class(ErrorClass.NOT_FOUND)
    if isinstance(exc, (api.Forbidden, api.Unauthorized)):
        # Never NOT_FOUND. A denial that reads as an absence sends the caller
        # back to the source for bytes sitting in the bucket.
        return LabAssistantError(f"object store denied {operation}", context=context).with_class(
            ErrorClass.STORAGE_DENIED
        )
    if isinstance(exc, (api.PreconditionFailed, api.Conflict)):
        return LabAssistantError(
            f"object store precondition failed for {key or operation}", context=context
        ).with_class(ErrorClass.CONFLICT)
    if isinstance(exc, api.TooManyRequests):
        return LabAssistantError(f"object store throttled {operation}", context=context).with_class(
            ErrorClass.RATE_LIMITED
        )
    if isinstance(exc, (api.DeadlineExceeded, api.GatewayTimeout)):
        return LabAssistantError(
            f"timeout contacting the object store during {operation}", context=context
        ).with_class(ErrorClass.TIMEOUT)
    if isinstance(exc, api.ServiceUnavailable):
        return LabAssistantError(
            f"cannot reach the object store endpoint during {operation}", context=context
        ).with_class(ErrorClass.SOURCE_UNAVAILABLE)

    status = getattr(exc, "code", None)
    if isinstance(status, int):
        context["status"] = status
        if status == 404:
            return LabAssistantError(
                f"object store has no {key or 'object'}", context=context
            ).with_class(ErrorClass.NOT_FOUND)
        if status in (401, 403):
            return LabAssistantError(
                f"object store denied {operation}", context=context
            ).with_class(ErrorClass.STORAGE_DENIED)
        if status in _PRECONDITION_STATUS:
            return LabAssistantError(
                f"object store precondition failed for {key or operation}", context=context
            ).with_class(ErrorClass.CONFLICT)
        if status == 429:
            return LabAssistantError(
                f"object store throttled {operation}", context=context
            ).with_class(ErrorClass.RATE_LIMITED)
        if status >= 500:
            return LabAssistantError(
                f"object store returned {status} for {operation}", context=context
            ).with_class(ErrorClass.PROVIDER_ERROR)

    if isinstance(exc, api.GoogleAPICallError):
        return LabAssistantError(
            f"object store {operation} failed: {exc}", context=context
        ).with_class(ErrorClass.PROVIDER_ERROR)
    return LabAssistantError(f"object store {operation} failed: {exc}", context=context).with_class(
        ErrorClass.PROVIDER_ERROR
    )


class GcsObjectStore:
    """Content-addressed storage over Google Cloud Storage.

    The SDK is synchronous, so every call crosses one explicit thread boundary
    and a semaphore bounds how many are in flight. The boundary is in one place
    on purpose: a blocking SDK call made directly from the event loop stalls
    every other request in the process, and the failure is a latency mystery
    rather than an exception.
    """

    def __init__(
        self,
        config: GcsStoreConfig,
        *,
        client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        max_concurrency: int = 8,
    ) -> None:
        self._config = config
        self._client = client if client is not None else config.build_client()
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._gate = asyncio.Semaphore(max_concurrency)

    # ── the ObjectStore protocol ────────────────────────────────────────────

    async def put(
        self,
        data: bytes,
        *,
        workspace_id: UUID | None,
        object_class: str,
        media_type: str = "application/octet-stream",
    ) -> StoredObjectRecord:
        enforce_limit(len(data), self._config.max_object_bytes)
        digest = sha256_bytes(data)
        object_id = _object_id(workspace_id, digest)
        existing = await self.head(object_id)
        if existing is not None:
            # Registering the same bytes twice is a no-op, never an overwrite.
            return existing
        key = _key_for(digest)
        await self._put_content(key, data, media_type)
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
        try:
            await self._write_record(record, create_only=True)
        except LabAssistantError as exc:
            if exc.error_class is not ErrorClass.CONFLICT:
                raise
            # Another writer registered these bytes between the head and the
            # write. Its record wins — including a tombstone it may carry.
            concurrent = await self.head(object_id)
            if concurrent is None:  # pragma: no cover - provider contradiction
                raise
            return concurrent
        return record

    async def put_stream(
        self,
        source: AsyncIterator[bytes],
        *,
        workspace_id: UUID | None,
        object_class: str,
        media_type: str = "application/octet-stream",
        max_bytes: int | None = None,
        created_at: datetime | None = None,
    ) -> StoredObjectRecord:
        """Spool to a digest, then upload — resumably when it is large enough.

        The key is the digest and the digest is only known at the end, so the
        payload is spooled: in memory while small, on disk once it is not.
        """
        limit = max_bytes if max_bytes is not None else self._config.max_object_bytes
        with await spool_stream(source, max_bytes=limit) as payload:
            object_id = _object_id(workspace_id, payload.sha256)
            existing = await self.head(object_id)
            if existing is not None:
                return existing
            key = _key_for(payload.sha256)
            await self._upload(payload, self._object_key(key), media_type)
            record = StoredObjectRecord(
                id=object_id,
                workspace_id=workspace_id,
                sha256=payload.sha256,
                byte_length=payload.byte_length,
                media_type=media_type,
                object_class=object_class,
                created_at=created_at or self._clock(),
                key=key,
            )
        try:
            await self._write_record(record, create_only=True)
        except LabAssistantError as exc:
            if exc.error_class is not ErrorClass.CONFLICT:
                raise
            concurrent = await self.head(object_id)
            if concurrent is None:  # pragma: no cover - provider contradiction
                raise
            return concurrent
        return record

    async def stream(
        self, object_id: UUID, *, chunk_size: int = STREAM_CHUNK_BYTES
    ) -> AsyncIterator[bytes]:
        """Yield the object's bytes, verifying the digest as they pass.

        Each read crosses the thread boundary, because the SDK's reader is
        blocking and reading it on the event loop would stall every other
        request for the length of the download.
        """
        record = await self.head(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        if record.tombstoned:
            raise FileNotFoundError(f"object {object_id} has been tombstoned")
        key = self._object_key(record.key)
        blob = self._blob(key)
        reader = await self._in_thread("open", lambda: blob.open("rb"), key=key)
        digest = hashlib.sha256()
        try:
            while True:
                chunk = await self._in_thread("read", lambda: reader.read(chunk_size), key=key)
                if not chunk:
                    break
                digest.update(chunk)
                yield chunk
        finally:
            close = getattr(reader, "close", None)
            if close is not None:
                close()
        if digest.hexdigest() != record.sha256:
            raise LabAssistantError(
                "stored object does not match its recorded digest",
                context={"object_id": str(object_id), "key": record.key},
            ).with_class(ErrorClass.INVALID_CONTENT)

    async def get(self, object_id: UUID) -> bytes:
        record = await self.head(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        if record.tombstoned:
            raise FileNotFoundError(f"object {object_id} has been tombstoned")
        payload = await self._get_bytes(self._object_key(record.key))
        # Verified on read: a substituted or truncated object is a detected
        # fault, not evidence someone cites.
        if sha256_bytes(payload) != record.sha256:
            raise LabAssistantError(
                "stored object does not match its recorded digest",
                context={"object_id": str(object_id), "key": record.key},
            ).with_class(ErrorClass.INVALID_CONTENT)
        return payload

    async def get_by_digest(self, sha256: str, *, workspace_id: UUID | None) -> bytes | None:
        try:
            return await self.get(_object_id(workspace_id, sha256))
        except FileNotFoundError:
            return None

    async def head(self, object_id: UUID) -> StoredObjectRecord | None:
        record, _generation = await self._read_record(object_id)
        return record

    async def verify(self, object_id: UUID) -> bool:
        """Recompute the digest of stored bytes and compare with the record.

        ``False`` means absent or corrupt. A denial, a timeout or an
        unreachable endpoint raises instead: "I was not allowed to look" is not
        "the bytes are wrong", and answering ``False`` would let a permissions
        mistake be reported as data corruption.
        """
        record = await self.head(object_id)
        if record is None:
            return False
        try:
            payload = await self._get_bytes(self._object_key(record.key))
        except LabAssistantError as exc:
            if exc.error_class is ErrorClass.NOT_FOUND:
                return False
            raise
        return sha256_bytes(payload) == record.sha256

    async def signed_url(self, object_id: UUID, *, expires_seconds: int) -> str:
        """A V4 signed GET, so bytes never pass through the API process.

        Needs a service-account key or the IAM ``signBlob`` permission on the
        running identity. Where neither is available this raises rather than
        returning a URL the provider will refuse — the caller treats a failure
        to sign as a reason to serve the bytes through the API instead, and a
        URL that 403s when somebody clicks it is a worse answer than an error
        here.
        """
        from datetime import timedelta

        record = await self.head(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        if record.tombstoned:
            raise FileNotFoundError(f"object {object_id} has been tombstoned")
        key = self._object_key(record.key)
        blob = self._blob(key)
        expires = min(expires_seconds, 604_800)
        return await self._in_thread(
            "generate_signed_url",
            lambda: blob.generate_signed_url(
                version="v4", expiration=timedelta(seconds=expires), method="GET"
            ),
            key=key,
        )

    async def tombstone(self, object_id: UUID, *, reason: str) -> None:
        """Deny access to the payload without touching the payload.

        The update is conditional on the generation just read, so two stewards
        acting at once produce a conflict rather than one silently overwriting
        the other — and a slow writer holding an old record cannot put it back.
        """
        record, generation = await self._read_record(object_id)
        if record is None:
            return
        await self._write_record(replace(record, tombstoned=True), expected_generation=generation)

    # ── readiness ──────────────────────────────────────────────────────────

    async def ping(self) -> None:
        """Confirm the bucket answers. Raises with the reason if it does not.

        This is a LIST, which a narrowly scoped reader credential may not hold
        even when every GET it needs would succeed — which is why a reader is
        probed by fetching a manifest it serves instead (F15). This is the
        publisher's probe.
        """
        bucket = self._client.bucket(self._config.bucket)
        await self._in_thread(
            "list_blobs",
            lambda: list(
                self._client.list_blobs(bucket, prefix=self._config.prefix or None, max_results=1)
            ),
        )

    async def aclose(self) -> None:
        """Release the SDK's transport on shutdown."""
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)

    # ── internals ──────────────────────────────────────────────────────────

    def _object_key(self, logical_key: str) -> str:
        return f"{self._config.prefix}{logical_key}"

    def _record_key(self, object_id: UUID) -> str:
        return f"{self._config.prefix}records/{object_id}.json"

    def _blob(self, key: str, *, generation: int | None = None) -> Any:
        bucket = self._client.bucket(self._config.bucket)
        return bucket.blob(key) if generation is None else bucket.blob(key, generation=generation)

    async def _in_thread(self, operation: str, call: Callable[[], Any], *, key: str | None = None):
        async with self._gate:
            try:
                return await asyncio.to_thread(call)
            except LabAssistantError:
                raise
            except Exception as exc:  # translated, never leaked raw
                raise _translate(exc, operation=operation, key=key) from exc

    async def _get_bytes(self, key: str) -> bytes:
        blob = self._blob(key)
        return await self._in_thread("download_as_bytes", lambda: blob.download_as_bytes(), key=key)

    async def _upload(self, payload: SpooledPayload, key: str, media_type: str) -> None:
        """One request, or a resumable upload, depending on size.

        A precondition failure is not an error: the key *is* the digest, so an
        object already under it holds these bytes.
        """
        if payload.byte_length < self._config.multipart_threshold:
            await self._put_content_bytes(key, payload.read_all(), media_type)
            return

        blob = self._blob(key)
        # Setting a chunk size is what makes the SDK use a resumable session
        # rather than one request, so a transfer interrupted part way does not
        # start again from zero. GCS requires it to be a multiple of 256 KiB.
        chunk = max(self._config.multipart_threshold, 256 * 1024)
        blob.chunk_size = chunk - (chunk % (256 * 1024)) or 256 * 1024
        blob.md5_hash = _md5_base64_of_file(payload)
        timeout = self._config.timeout

        def call() -> None:
            payload.file.seek(0)
            blob.upload_from_file(
                payload.file,
                size=payload.byte_length,
                content_type=media_type,
                if_generation_match=0,
                timeout=timeout,
            )

        try:
            await self._in_thread("upload_from_file", call, key=key)
        except LabAssistantError as exc:
            if exc.error_class is not ErrorClass.CONFLICT:
                raise

    async def _put_content(self, logical_key: str, data: bytes, media_type: str) -> None:
        await self._put_content_bytes(self._object_key(logical_key), data, media_type)

    async def _put_content_bytes(self, key: str, data: bytes, media_type: str) -> None:
        """Write immutable content in one request, created once.

        The MD5 goes to the provider so it rejects a transfer damaged in
        flight. It is a transport check and nothing more: GCS has no SHA-256
        field, so content identity stays the application digest — in the key, in
        the database, and verified on every read.
        """
        blob = self._blob(key)
        blob.md5_hash = _md5_base64(data)
        try:
            await self._in_thread(
                "upload_from_string",
                lambda: blob.upload_from_string(
                    data,
                    content_type=media_type,
                    if_generation_match=0,
                    timeout=self._config.timeout,
                ),
                key=key,
            )
        except LabAssistantError as exc:
            if exc.error_class is not ErrorClass.CONFLICT:
                raise

    async def _read_record(self, object_id: UUID) -> tuple[StoredObjectRecord | None, int | None]:
        """The metadata record and the generation that produced it.

        Returns ``(None, None)`` only for a genuine absence. Every other
        failure — denied, throttled, unreachable — is raised, because the
        caller's next move differs for each and "absent" is the one answer that
        sends it back to the source.
        """
        key = self._record_key(object_id)
        blob = self._blob(key)

        def call() -> tuple[bytes, int | None]:
            payload = blob.download_as_bytes()
            return payload, blob.generation

        try:
            payload, generation = await self._in_thread("download_as_bytes", call, key=key)
        except LabAssistantError as exc:
            if exc.error_class is ErrorClass.NOT_FOUND:
                return None, None
            raise
        return _record_from_json(payload), generation

    async def _write_record(
        self,
        record: StoredObjectRecord,
        *,
        create_only: bool = False,
        expected_generation: int | None = None,
    ) -> None:
        """Write the metadata record under a precondition.

        ``if_generation_match=0`` is GCS's "only if absent"; a generation is the
        compare-and-swap. Exactly one of the two applies, and an unconditional
        write is not offered: that is how a stale writer resurrects a tombstone.
        """
        key = self._record_key(record.id)
        blob = self._blob(key)
        payload = json.dumps(
            {
                "id": str(record.id),
                "workspace_id": str(record.workspace_id) if record.workspace_id else None,
                "sha256": record.sha256,
                "byte_length": record.byte_length,
                "media_type": record.media_type,
                "object_class": record.object_class,
                "key": record.key,
                "tombstoned": record.tombstoned,
                "created_at": record.created_at.isoformat() if record.created_at else None,
            },
            sort_keys=True,
        ).encode("utf-8")

        generation = 0 if create_only else expected_generation
        await self._in_thread(
            "upload_from_string",
            lambda: blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=generation,
                timeout=self._config.timeout,
            ),
            key=key,
        )


def _md5_base64(data: bytes) -> str:
    """Base64 MD5, which is the field GCS verifies an upload against.

    Not a security property and not an identity: MD5 is here because it is what
    the provider checks, and it catches a transfer corrupted in flight. What
    identifies the object is its SHA-256.
    """
    import base64

    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")


def _md5_base64_of_file(payload: SpooledPayload) -> str:
    """The same, for a payload too large to hold in memory."""
    import base64

    digest = hashlib.md5(usedforsecurity=False)
    payload.file.seek(0)
    while chunk := payload.file.read(STREAM_CHUNK_BYTES):
        digest.update(chunk)
    payload.file.seek(0)
    return base64.b64encode(digest.digest()).decode("ascii")


def _record_from_json(payload: bytes) -> StoredObjectRecord:
    data = json.loads(payload)
    created = data.get("created_at")
    return StoredObjectRecord(
        id=UUID(data["id"]),
        workspace_id=UUID(data["workspace_id"]) if data.get("workspace_id") else None,
        sha256=data["sha256"],
        byte_length=int(data["byte_length"]),
        media_type=data["media_type"],
        object_class=data["object_class"],
        created_at=datetime.fromisoformat(created) if created else None,
        key=data["key"],
        tombstoned=bool(data.get("tombstoned", False)),
    )
