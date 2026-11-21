"""S3-compatible object storage, over the maintained AWS SDK.

Same contract as the filesystem store, same key convention::

    sha256/ab/cd/<full-digest>        immutable content
    records/<object-id>.json         one metadata record per (workspace, digest)

A configured prefix is prepended to both when the bucket is shared, and it is
*not* part of the stored key: ``StoredObjectRecord.key`` stays logical, so the
database survives a move to another bucket, prefix or provider. A bucket name is
an address, not an identity (ADR-0023).

This replaces a hand-written SigV4 signer. That signer had four independent
defects — an unsigned query parameter appended after signing, a host header
without the port, no endpoint path prefix, and static keys only — and none of
them could be found without a real provider, because the stubbed-transport tests
ADR-0017 described were never written. The SDK brings the credential chain and
its refresh, which long acquisition runs under temporary credentials need.

Three behaviours here are load-bearing:

- **Missing is not denied.** A 404 means the object is absent; a 403 means
  storage answered and refused. Conflating them let a reader with wrong bucket
  permissions conclude the evidence was gone and acquire it again, paying a
  source a second time for bytes that were already there.
- **Writes are conditional.** Content is created with ``If-None-Match: *``, and
  a record update carries ``If-Match`` on the version it read. A stale writer
  cannot resurrect a tombstoned record by writing over it.
- **Digest verified on read.** A provider ETag is a concurrency token, not a
  content hash: it is not the SHA-256 for a multipart upload and it is not the
  SHA-256 under server-side encryption. The application hash is checked itself.

Nothing here decides *whether* bytes may be stored. That is the rights policy's
job, evaluated long before an object reaches a store.
"""

from __future__ import annotations

import asyncio
import base64
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

__all__ = ["S3ObjectStore", "S3StoreConfig"]

#: Provider error codes, by what the caller must do about them. A code absent
#: from every set falls back to the HTTP status, and an unrecognised status is a
#: provider error rather than a silent success.
_DENIED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AccountProblem",
        "AllAccessDisabled",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidAccessKeyId",
        "InvalidToken",
        "RequestTimeTooSkewed",
        "SignatureDoesNotMatch",
        "UnauthorizedAccess",
    }
)
_MISSING_CODES = frozenset({"404", "NoSuchBucket", "NoSuchKey", "NotFound"})
_CONFLICT_CODES = frozenset(
    {"ConditionalRequestConflict", "OperationAborted", "PreconditionFailed"}
)
#: The object is in a cold storage class. It exists, it is not readable yet, and
#: the only correct response is to wait for the restore — never to report it
#: missing and never to re-run the analysis that produced it.
_RESTORE_CODES = frozenset({"InvalidObjectState"})
_THROTTLE_CODES = frozenset(
    {"RequestLimitExceeded", "SlowDown", "Throttling", "ThrottlingException", "TooManyRequests"}
)
#: Conditional writes are unsupported by some S3-compatible gateways, which say
#: so with one of these rather than by honouring the header.
_UNSUPPORTED_CODES = frozenset({"MethodNotAllowed", "NotImplemented", "UnsupportedArgument"})


class ConditionalWritesUnsupported(LabAssistantError):
    """The endpoint does not implement the precondition this write needs."""

    error_class = ErrorClass.PROVIDER_REFUSAL


class S3StoreConfig:
    """Bucket, region, addressing and credentials, validated at construction.

    A store that cannot work refuses to exist rather than failing on the first
    upload — which would be during an acquisition run, hours after the deploy
    that broke it (ADR-0017, kept).

    Credentials are optional *here* but not absent: leaving them unset selects
    the SDK's ordinary chain — environment, shared config, container and
    instance roles, web identity — which is how a deployment gets short-lived
    credentials that refresh inside a long job. Construction still refuses when
    the chain resolves to nothing, so "no credentials anywhere" remains a
    startup failure rather than a runtime one.
    """

    __slots__ = (
        "access_key",
        "bucket",
        "conditional_writes",
        "connect_timeout",
        "endpoint",
        "max_attempts",
        "max_object_bytes",
        "max_pool_connections",
        "multipart_threshold",
        "path_style",
        "prefix",
        "profile",
        "read_timeout",
        "region",
        "secret_key",
        "session_token",
        "transfer_concurrency",
    )

    def __init__(
        self,
        *,
        bucket: str | None,
        region: str | None,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        session_token: str | None = None,
        profile: str | None = None,
        prefix: str = "",
        path_style: bool = True,
        conditional_writes: bool = True,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_attempts: int = 5,
        max_pool_connections: int = 16,
        multipart_threshold: int = SPOOL_THRESHOLD_BYTES,
        max_object_bytes: int | None = None,
        transfer_concurrency: int = 4,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("LAB_OBJECT_STORE_BUCKET", bucket),
                ("LAB_OBJECT_STORE_REGION", region),
            )
            if not value
        ]
        if missing:
            raise LabAssistantError(
                "S3 object store is selected but not configured",
                context={"missing": missing},
            ).with_class(ErrorClass.INVALID_CONTENT)
        if bool(access_key) != bool(secret_key):
            # Half a pair would otherwise fall through to the credential chain
            # and succeed with somebody else's identity.
            absent = "LAB_OBJECT_STORE_SECRET_KEY" if access_key else "LAB_OBJECT_STORE_ACCESS_KEY"
            raise LabAssistantError(
                "S3 object store has half a static credential; supply both keys or "
                "neither and let the credential chain resolve it",
                context={"missing": [absent]},
            ).with_class(ErrorClass.INVALID_CONTENT)
        assert bucket and region  # for type narrowing
        self.bucket = bucket
        self.region = region
        self.endpoint = endpoint.rstrip("/") if endpoint else None
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.profile = profile
        #: Shared-bucket isolation. Normalised to end in "/" so a key is simply
        #: appended, and never stored on a record: see the module docstring.
        self.prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""
        #: Path style (``endpoint/bucket/key``) works on every S3-compatible
        #: implementation; virtual-host style does not.
        self.path_style = path_style
        #: Turn off only for an endpoint that rejects preconditions. Doing so
        #: reopens the write race this adapter exists to close, so it is a
        #: deliberate, logged choice rather than a silent fallback.
        self.conditional_writes = conditional_writes
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_attempts = max_attempts
        self.max_pool_connections = max_pool_connections
        #: At or above this a payload is uploaded in parts, so a transfer
        #: that fails resumes from a part boundary rather than from zero.
        self.multipart_threshold = multipart_threshold
        #: Refused before the upload starts. ``None`` means the deployment
        #: has not set one, not that anything goes: the caller's own limit
        #: still applies.
        self.max_object_bytes = max_object_bytes
        #: Parts in flight per upload. Bounded, because the thread pool
        #: behind them is shared with every other request in the process.
        self.transfer_concurrency = transfer_concurrency

    @classmethod
    def from_settings(cls, settings: Any) -> S3StoreConfig:
        return cls(
            bucket=settings.object_store_bucket,
            region=settings.object_store_region,
            endpoint=settings.object_store_endpoint,
            access_key=settings.object_store_access_key,
            secret_key=settings.object_store_secret_key,
            session_token=getattr(settings, "object_store_session_token", None),
            profile=getattr(settings, "object_store_profile", None),
            prefix=getattr(settings, "object_store_prefix", "") or "",
            conditional_writes=getattr(settings, "object_store_conditional_writes", True),
            multipart_threshold=getattr(
                settings, "object_store_multipart_threshold_bytes", SPOOL_THRESHOLD_BYTES
            ),
            max_object_bytes=getattr(settings, "object_store_max_object_bytes", None) or None,
        )

    def build_client(self) -> Any:
        """Construct the SDK client, refusing when no credential resolves."""
        try:
            import boto3
            from botocore.config import Config
        except ModuleNotFoundError as exc:
            raise _sdk_missing("s3", "boto3", "objectstore") from exc

        session = boto3.session.Session(
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            aws_session_token=self.session_token,
            region_name=self.region,
            profile_name=self.profile,
        )
        if session.get_credentials() is None:
            raise LabAssistantError(
                "S3 object store found no credentials: set LAB_OBJECT_STORE_ACCESS_KEY "
                "and LAB_OBJECT_STORE_SECRET_KEY, or give the deployment a role the "
                "SDK credential chain can resolve",
                context={"region": self.region, "bucket": self.bucket},
            ).with_class(ErrorClass.AUTHENTICATION_REQUIRED)
        return session.client(
            "s3",
            endpoint_url=self.endpoint,
            config=Config(
                s3={"addressing_style": "path" if self.path_style else "virtual"},
                retries={"max_attempts": self.max_attempts, "mode": "standard"},
                connect_timeout=self.connect_timeout,
                read_timeout=self.read_timeout,
                max_pool_connections=self.max_pool_connections,
            ),
        )


def _translate(exc: Exception, *, operation: str, key: str | None) -> LabAssistantError:
    """Map one provider failure onto the taxonomy, deliberately.

    The default is ``PROVIDER_ERROR`` — retryable — rather than anything that
    reads as an answer about the object. An unrecognised failure is a failure,
    not an absence.
    """
    from botocore.exceptions import (
        ClientError,
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        NoCredentialsError,
        ReadTimeoutError,
    )

    context: dict[str, Any] = {"operation": operation}
    if key is not None:
        context["key"] = key

    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        return LabAssistantError(
            f"timeout contacting the object store during {operation}", context=context
        ).with_class(ErrorClass.TIMEOUT)
    if isinstance(exc, (ConnectionClosedError, EndpointConnectionError)):
        return LabAssistantError(
            f"cannot reach the object store endpoint during {operation}", context=context
        ).with_class(ErrorClass.SOURCE_UNAVAILABLE)
    if isinstance(exc, NoCredentialsError):
        return LabAssistantError(
            "the object store credential chain resolved to nothing", context=context
        ).with_class(ErrorClass.AUTHENTICATION_REQUIRED)
    if not isinstance(exc, ClientError):
        return LabAssistantError(
            f"object store {operation} failed: {exc}", context=context
        ).with_class(ErrorClass.PROVIDER_ERROR)

    error = exc.response.get("Error", {})
    code = str(error.get("Code", ""))
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
    context |= {"code": code, "status": status}

    # The code is read before the status, because the two disagree. A GET on an
    # object in a cold class answers `InvalidObjectState` with HTTP 403, and a
    # status-first reading turns "this is being restored, wait" into "you are
    # not allowed to see this" — which is the same class of conflation as
    # treating a denial as an absence, one layer further in.
    if code:
        if code in _MISSING_CODES:
            return LabAssistantError(
                f"object store has no {key or 'object'}", context=context
            ).with_class(ErrorClass.NOT_FOUND)
        if code in _RESTORE_CODES:
            return LabAssistantError(
                f"object is in a cold storage class and is being restored: {key}",
                context=context,
            ).with_class(ErrorClass.STORAGE_RESTORE_PENDING)
        if code in _DENIED_CODES:
            # Never NOT_FOUND. A denial that reads as an absence sends the
            # caller back to the source for bytes sitting in the bucket.
            return LabAssistantError(
                f"object store denied {operation}", context=context
            ).with_class(ErrorClass.STORAGE_DENIED)
        if code in _UNSUPPORTED_CODES:
            return ConditionalWritesUnsupported(
                f"object store endpoint does not support {operation} as issued", context=context
            )
        if code in _CONFLICT_CODES:
            return LabAssistantError(
                f"object store precondition failed for {key or operation}", context=context
            ).with_class(ErrorClass.CONFLICT)
        if code in _THROTTLE_CODES:
            return LabAssistantError(
                f"object store throttled {operation}", context=context
            ).with_class(ErrorClass.RATE_LIMITED)

    # Only then the status, for a provider that sent a code we do not know.
    if status == 404:
        return LabAssistantError(
            f"object store has no {key or 'object'}", context=context
        ).with_class(ErrorClass.NOT_FOUND)
    if status in (401, 403):
        return LabAssistantError(f"object store denied {operation}", context=context).with_class(
            ErrorClass.STORAGE_DENIED
        )
    if status == 501:
        return ConditionalWritesUnsupported(
            f"object store endpoint does not support {operation} as issued", context=context
        )
    if status in (409, 412):
        return LabAssistantError(
            f"object store precondition failed for {key or operation}", context=context
        ).with_class(ErrorClass.CONFLICT)
    if status == 429:
        return LabAssistantError(f"object store throttled {operation}", context=context).with_class(
            ErrorClass.RATE_LIMITED
        )
    if status >= 500:
        return LabAssistantError(
            f"object store returned {status} for {operation}", context=context
        ).with_class(ErrorClass.PROVIDER_ERROR)
    return LabAssistantError(
        f"object store {operation} failed with {code or status}", context=context
    ).with_class(ErrorClass.PROVIDER_ERROR)


class S3ObjectStore:
    """Content-addressed storage over an S3-compatible endpoint.

    The SDK is synchronous, so every call crosses one explicit thread boundary
    and a semaphore bounds how many are in flight. The boundary is in one place
    on purpose: a blocking SDK call made directly from the event loop stalls
    every other request in the process, and the failure is a latency mystery
    rather than an exception.
    """

    def __init__(
        self,
        config: S3StoreConfig,
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
        """Spool to a digest, then upload — in parts when it is large enough.

        Above the multipart threshold the SDK uploads concurrently and the
        conditional create is not available on the completing call. That is
        acceptable for *content* and only for content: the key is the digest, so
        a writer racing here is writing the same bytes. The metadata record,
        which is mutable and where a race actually matters, is always a single
        conditional ``put_object`` below.
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
            concurrent = await self.head(record.id)
            if concurrent is None:  # pragma: no cover - provider contradiction
                raise
            return concurrent
        return record

    async def stream(
        self, object_id: UUID, *, chunk_size: int = STREAM_CHUNK_BYTES
    ) -> AsyncIterator[bytes]:
        """Yield the object's bytes, verifying the digest as they pass.

        Each read crosses the thread boundary, because the SDK's body is a
        blocking stream and reading it on the event loop would stall every other
        request for the length of the download.
        """
        record = await self.head(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        if record.tombstoned:
            raise FileNotFoundError(f"object {object_id} has been tombstoned")
        key = self._object_key(record.key)
        bucket = self._config.bucket
        body = (
            await self._in_thread(
                "get_object", lambda: self._client.get_object(Bucket=bucket, Key=key), key=key
            )
        )["Body"]
        digest = hashlib.sha256()
        try:
            while True:
                chunk = await self._in_thread("read", lambda: body.read(chunk_size), key=key)
                if not chunk:
                    break
                digest.update(chunk)
                yield chunk
        finally:
            close = getattr(body, "close", None)
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
        record, _version = await self._read_record(object_id)
        return record

    async def verify(self, object_id: UUID) -> bool:
        """Recompute the digest of stored bytes and compare with the record.

        ``False`` means absent or corrupt. A denial, a timeout or a cold object
        raises instead: "I was not allowed to look" is not "the bytes are wrong",
        and answering ``False`` would let a permissions mistake be reported as
        data corruption.
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
        """A presigned GET, so bytes never pass through the API process.

        The URL is a bearer capability for its lifetime and carries no
        per-request policy check, which is why the caller decides whether the
        material may be handed out this way at all.
        """
        record = await self.head(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        if record.tombstoned:
            raise FileNotFoundError(f"object {object_id} has been tombstoned")
        bucket = self._config.bucket
        key = self._object_key(record.key)
        expires = min(expires_seconds, 604_800)
        return await self._in_thread(
            "generate_presigned_url",
            lambda: self._client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
            ),
            key=key,
        )

    async def tombstone(self, object_id: UUID, *, reason: str) -> None:
        """Deny access to the payload without touching the payload.

        The update is conditional on the version just read, so two stewards
        acting at once produce a conflict rather than one silently overwriting
        the other — and a slow writer holding an old record cannot put it back.
        """
        record, version = await self._read_record(object_id)
        if record is None:
            return
        await self._write_record(replace(record, tombstoned=True), expected_version=version)

    # ── readiness ──────────────────────────────────────────────────────────

    async def ping(self) -> None:
        """Confirm the bucket answers. Raises with the reason if it does not.

        This is a LIST, which a narrowly scoped reader credential may not hold
        even when every GET it needs would succeed. Role-specific readiness is
        still owed (audit F15); this keeps the existing behaviour rather than
        quietly weakening it.
        """
        bucket = self._config.bucket
        prefix = self._config.prefix
        await self._in_thread(
            "list_objects_v2",
            lambda: self._client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1),
        )

    async def aclose(self) -> None:
        """Release the SDK's connection pool on shutdown."""
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)

    # ── internals ──────────────────────────────────────────────────────────

    def _object_key(self, logical_key: str) -> str:
        return f"{self._config.prefix}{logical_key}"

    def _record_key(self, object_id: UUID) -> str:
        return f"{self._config.prefix}records/{object_id}.json"

    async def _in_thread(self, operation: str, call: Callable[[], Any], *, key: str | None = None):
        async with self._gate:
            try:
                return await asyncio.to_thread(call)
            except LabAssistantError:
                raise
            except Exception as exc:  # translated, never swallowed
                raise _translate(exc, operation=operation, key=key) from exc

    async def _get_bytes(self, key: str) -> bytes:
        bucket = self._config.bucket

        def call() -> bytes:
            # Read the body inside the worker thread: the stream is blocking
            # too, and handing it back would move the blocking read onto the
            # event loop the thread hop exists to protect.
            response = self._client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read()

        return await self._in_thread("get_object", call, key=key)

    async def _upload(self, payload: SpooledPayload, key: str, media_type: str) -> None:
        """One request, or several parts, depending on size."""
        if payload.byte_length < self._config.multipart_threshold:
            await self._put_content_bytes(key, payload.read_all(), media_type, payload.sha256)
            return

        from boto3.s3.transfer import TransferConfig

        bucket = self._config.bucket
        transfer = TransferConfig(
            multipart_threshold=self._config.multipart_threshold,
            multipart_chunksize=self._config.multipart_threshold,
            max_concurrency=self._config.transfer_concurrency,
        )

        def call() -> None:
            payload.file.seek(0)
            self._client.upload_fileobj(
                payload.file,
                bucket,
                key,
                # Per-part SHA-256, so the provider rejects a part that arrived
                # damaged rather than assembling it into the object. The whole
                # object's digest is still checked by this application on read.
                ExtraArgs={"ContentType": media_type, "ChecksumAlgorithm": "SHA256"},
                Config=transfer,
            )

        await self._in_thread("upload_fileobj", call, key=key)

    async def _put_content(self, logical_key: str, data: bytes, media_type: str) -> None:
        await self._put_content_bytes(
            self._object_key(logical_key), data, media_type, sha256_bytes(data)
        )

    async def _put_content_bytes(self, key: str, data: bytes, media_type: str, sha256: str) -> None:
        """Write immutable content in one request, created once.

        A precondition failure here is not an error: the key *is* the digest, so
        an object already under it holds these bytes.

        ``sha256`` goes to the provider as well, so it verifies what it received
        and rejects a damaged upload at the door. An ETag would not do: it is
        the digest of nothing in particular once server-side encryption or
        multipart is involved.
        """
        checksum = base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
        try:
            await self._put(
                key,
                data,
                media_type,
                create_only=self._config.conditional_writes,
                checksum_sha256=checksum,
            )
        except ConditionalWritesUnsupported:
            await self._put(key, data, media_type, create_only=False, checksum_sha256=checksum)
        except LabAssistantError as exc:
            if exc.error_class is not ErrorClass.CONFLICT:
                raise

    async def _put(
        self,
        key: str,
        data: bytes,
        media_type: str,
        *,
        create_only: bool = False,
        expected_version: str | None = None,
        checksum_sha256: str | None = None,
    ) -> str | None:
        bucket = self._config.bucket
        arguments: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
            "ContentType": media_type,
        }
        if checksum_sha256 is not None:
            arguments["ChecksumSHA256"] = checksum_sha256
        if create_only:
            arguments["IfNoneMatch"] = "*"
        elif expected_version is not None:
            arguments["IfMatch"] = expected_version
        response = await self._in_thread(
            "put_object", lambda: self._client.put_object(**arguments), key=key
        )
        return response.get("ETag") if isinstance(response, dict) else None

    async def _read_record(self, object_id: UUID) -> tuple[StoredObjectRecord | None, str | None]:
        """The metadata record and the provider version that produced it.

        Returns ``(None, None)`` only for a genuine absence. Every other
        failure — denied, throttled, cold, unreachable — is raised, because the
        caller's next move differs for each and "absent" is the one answer that
        sends it back to the source.
        """
        key = self._record_key(object_id)
        bucket = self._config.bucket

        def call() -> tuple[bytes, str | None]:
            response = self._client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read(), response.get("ETag")

        try:
            payload, version = await self._in_thread("get_object", call, key=key)
        except LabAssistantError as exc:
            if exc.error_class is ErrorClass.NOT_FOUND:
                return None, None
            raise
        return _record_from_json(payload), version

    async def _write_record(
        self,
        record: StoredObjectRecord,
        *,
        create_only: bool = False,
        expected_version: str | None = None,
    ) -> None:
        payload = json.dumps(
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
        ).encode("utf-8")
        conditional = self._config.conditional_writes
        checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
        try:
            await self._put(
                self._record_key(record.id),
                payload,
                "application/json",
                create_only=create_only and conditional,
                expected_version=expected_version if conditional else None,
                checksum_sha256=checksum,
            )
        except ConditionalWritesUnsupported:
            await self._put(
                self._record_key(record.id),
                payload,
                "application/json",
                checksum_sha256=checksum,
            )


def _record_from_json(payload: bytes) -> StoredObjectRecord:
    row = json.loads(payload)
    return StoredObjectRecord(
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


def _sdk_missing(kind: str, package: str, extra: str) -> LabAssistantError:
    """A configured provider whose SDK is not installed: say so at startup.

    The SDKs are optional extras, imported only by their adapter. An image built
    without one used to fail with a bare ModuleNotFoundError on the first object
    the application touched.
    """
    return LabAssistantError(
        f"LAB_OBJECT_STORE_KIND={kind} needs the {package} package, which this "
        f"installation does not have: install lab-assistant with the `{extra}` extra",
        context={"object_store_kind": kind, "package": package, "extra": extra},
    ).with_class(ErrorClass.INTERNAL)
