"""S3-compatible object storage.

Same contract as the filesystem store, same key convention::

    sha256/ab/cd/<full-digest>        immutable content
    records/<object-id>.json         one metadata record per (workspace, digest)

Content objects are written once and never overwritten: ``put`` checks for the key
first, and there is no code path that rewrites bytes under an existing key. The
metadata record beside it *is* mutable, because a tombstone has to be recordable
without touching content — the distinction is deliberate and is why they are
separate keys.

Requests are signed with SigV4 using the standard library's ``hmac`` and
``hashlib`` over ``httpx``, so this adapter adds no dependency and stays async.
Any S3-compatible endpoint works: AWS, MinIO, Ceph, or an institutional gateway.

Nothing here decides *whether* bytes may be stored. That is the rights policy's
job, evaluated long before an object reaches a store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID

import httpx

from lab_assistant.adapters.object_store.filesystem import StoredObjectRecord, _key_for, _object_id
from lab_assistant.contracts.canonical import sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = ["S3ObjectStore", "S3StoreConfig"]

_UNSIGNED = "UNSIGNED-PAYLOAD"
_ALGORITHM = "AWS4-HMAC-SHA256"


class S3StoreConfig:
    """Endpoint, bucket, region, and credentials, validated at construction.

    A store that is missing its bucket cannot work, so it refuses to exist rather
    than failing on the first upload — which would be during an acquisition run,
    hours after the deploy that broke it.
    """

    __slots__ = ("access_key", "bucket", "endpoint", "path_style", "region", "secret_key")

    def __init__(
        self,
        *,
        bucket: str | None,
        endpoint: str | None,
        region: str | None,
        access_key: str | None,
        secret_key: str | None,
        path_style: bool = True,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("LAB_OBJECT_STORE_BUCKET", bucket),
                ("LAB_OBJECT_STORE_ENDPOINT", endpoint),
                ("LAB_OBJECT_STORE_REGION", region),
                ("LAB_OBJECT_STORE_ACCESS_KEY", access_key),
                ("LAB_OBJECT_STORE_SECRET_KEY", secret_key),
            )
            if not value
        ]
        if missing:
            raise LabAssistantError(
                "S3 object store is selected but not configured",
                context={"missing": missing},
            ).with_class(ErrorClass.INVALID_CONTENT)
        assert bucket and endpoint and region and access_key and secret_key  # for type narrowing
        self.bucket = bucket
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        #: Path style (``endpoint/bucket/key``) works on every S3-compatible
        #: implementation; virtual-host style does not.
        self.path_style = path_style

    @classmethod
    def from_settings(cls, settings) -> S3StoreConfig:
        return cls(
            bucket=settings.object_store_bucket,
            endpoint=settings.object_store_endpoint,
            region=settings.object_store_region,
            access_key=settings.object_store_access_key,
            secret_key=settings.object_store_secret_key,
        )


class S3ObjectStore:
    """Content-addressed storage over an S3-compatible endpoint."""

    def __init__(
        self,
        config: S3StoreConfig,
        *,
        client: httpx.AsyncClient | None = None,
        clock=None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._config = config
        self._client = client
        self._timeout = timeout_seconds
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    # ── the ObjectStore protocol ────────────────────────────────────────────

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
        existing = await self.head(object_id)
        if existing is not None:
            # Registering the same bytes twice is a no-op, never an overwrite.
            return existing
        key = _key_for(digest)
        if await self._status(key) != 200:
            await self._request("PUT", key, body=data, content_type=media_type)
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
        await self._write_record(record)
        return record

    async def get(self, object_id: UUID) -> bytes:
        record = await self.head(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        if record.tombstoned:
            raise FileNotFoundError(f"object {object_id} has been tombstoned")
        response = await self._request("GET", record.key)
        payload = response.content
        # Verified on read: a substituted or truncated object is a detected fault,
        # not evidence someone cites.
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
        key = self._record_key(object_id)
        response = await self._request("GET", key, allow_missing=True)
        if response is None:
            return None
        row = json.loads(response.content)
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

    async def verify(self, object_id: UUID) -> bool:
        record = await self.head(object_id)
        if record is None:
            return False
        response = await self._request("GET", record.key, allow_missing=True)
        if response is None:
            return False
        return sha256_bytes(response.content) == record.sha256

    async def signed_url(self, object_id: UUID, *, expires_seconds: int) -> str:
        """A presigned GET, so bytes never pass through the API process."""
        record = await self.head(object_id)
        if record is None:
            raise FileNotFoundError(f"object {object_id} is not registered")
        if record.tombstoned:
            raise FileNotFoundError(f"object {object_id} has been tombstoned")
        return self._presign(record.key, expires_seconds=expires_seconds)

    async def tombstone(self, object_id: UUID, *, reason: str) -> None:
        record = await self.head(object_id)
        if record is None:
            return
        await self._write_record(replace(record, tombstoned=True))
        await self._request(
            "PUT",
            f"{self._record_key(object_id)}.tombstone",
            body=reason.encode("utf-8"),
            content_type="text/plain",
        )

    # ── readiness ──────────────────────────────────────────────────────────

    async def ping(self) -> None:
        """Confirm the bucket answers. Raises with the reason if it does not."""
        response = await self._request("GET", "", params={"max-keys": "0", "list-type": "2"})
        if response.status_code != 200:  # pragma: no cover - _request already raises
            raise LabAssistantError(
                f"object store returned {response.status_code}"
            ).with_class(ErrorClass.SOURCE_UNAVAILABLE)

    # ── internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _record_key(object_id: UUID) -> str:
        return f"records/{object_id}.json"

    async def _write_record(self, record: StoredObjectRecord) -> None:
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
        await self._request(
            "PUT", self._record_key(record.id), body=payload, content_type="application/json"
        )

    def _url(self, key: str) -> str:
        config = self._config
        prefix = f"{config.endpoint}/{config.bucket}" if config.path_style else config.endpoint
        if not key:
            return prefix
        return f"{prefix}/{quote(key, safe='/')}"

    async def _status(self, key: str) -> int:
        response = await self._request("HEAD", key, allow_missing=True)
        return 404 if response is None else response.status_code

    async def _request(
        self,
        method: str,
        key: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        params: dict[str, str] | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response | None:
        url = self._url(key)
        headers = self._sign(method, key, body=body, content_type=content_type, params=params)
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            response = await client.request(
                method, url, content=body, headers=headers, params=params
            )
        except httpx.TimeoutException as exc:
            raise LabAssistantError(f"timeout contacting the object store: {url}").with_class(
                ErrorClass.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise LabAssistantError(f"object store transport error: {exc}").with_class(
                ErrorClass.SOURCE_UNAVAILABLE
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code in (403, 404) and allow_missing:
            return None
        if response.status_code >= 400:
            raise LabAssistantError(
                f"object store {method} {key or '/'} failed with {response.status_code}",
                context={"status": response.status_code},
            ).with_class(
                ErrorClass.AUTHENTICATION_REQUIRED
                if response.status_code in (401, 403)
                else ErrorClass.SOURCE_UNAVAILABLE
            )
        return response

    # ── SigV4 ──────────────────────────────────────────────────────────────

    def _sign(
        self,
        method: str,
        key: str,
        *,
        body: bytes | None,
        content_type: str | None,
        params: dict[str, str] | None,
    ) -> dict[str, str]:
        config = self._config
        now = datetime.now(tz=UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body or b"").hexdigest()
        host = httpx.URL(config.endpoint).host
        canonical_uri = self._canonical_uri(key)
        canonical_query = self._canonical_query(params or {})
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            headers["content-type"] = content_type
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            f"{name}:{headers[name]}\n" for name in sorted(headers)
        )
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        scope = f"{date_stamp}/{config.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        out = dict(headers)
        out["authorization"] = (
            f"{_ALGORITHM} Credential={config.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return out

    def _presign(self, key: str, *, expires_seconds: int) -> str:
        config = self._config
        now = datetime.now(tz=UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        scope = f"{date_stamp}/{config.region}/s3/aws4_request"
        host = httpx.URL(config.endpoint).host
        query = {
            "X-Amz-Algorithm": _ALGORITHM,
            "X-Amz-Credential": f"{config.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(min(expires_seconds, 604800)),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_request = "\n".join(
            [
                "GET",
                self._canonical_uri(key),
                self._canonical_query(query),
                f"host:{host}\n",
                "host",
                _UNSIGNED,
            ]
        )
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        expiry = (now + timedelta(seconds=expires_seconds)).isoformat()
        query["X-Amz-Signature"] = signature
        # The expiry is in the signature; the readable copy is for logs and tests.
        return f"{self._url(key)}?{self._canonical_query(query)}&x-lab-expires-at={quote(expiry)}"

    def _canonical_uri(self, key: str) -> str:
        path = f"/{self._config.bucket}" if self._config.path_style else ""
        if key:
            path = f"{path}/{quote(key, safe='/')}"
        return path or "/"

    @staticmethod
    def _canonical_query(params: dict[str, str]) -> str:
        return "&".join(
            f"{quote(name, safe='-_.~')}={quote(str(value), safe='-_.~')}"
            for name, value in sorted(params.items())
        )

    def _signing_key(self, date_stamp: str) -> bytes:
        def sign(key: bytes, message: str) -> bytes:
            return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

        key = sign(f"AWS4{self._config.secret_key}".encode(), date_stamp)
        key = sign(key, self._config.region)
        key = sign(key, "s3")
        return sign(key, "aws4_request")
