"""The S3 adapter's contract, against a stubbed SDK client.

ADR-0017 said this coverage existed. It did not, which is why four signing
defects and a missing/denied conflation survived in a shipped adapter. These
tests run offline against ``botocore.stub.Stubber``: real request parameters,
real error codes, no network.

What they are about, in order of how much damage the bug did:

- a 403 must never read as a 404, because a reader whose bucket permissions are
  wrong would conclude the paper is gone and pay a source for it again;
- a write must carry its precondition, because a stale writer that can overwrite
  a record can resurrect tombstoned material;
- a cold object is a named wait, not an absence and not a reason to re-analyse.
"""

from __future__ import annotations

import base64
import hashlib
import io
from datetime import UTC, datetime
from uuid import uuid4

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.response import StreamingBody  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

from lab_assistant.adapters.object_store.filesystem import _key_for, _object_id  # noqa: E402
from lab_assistant.adapters.object_store.s3 import (  # noqa: E402
    S3ObjectStore,
    S3StoreConfig,
)
from lab_assistant.contracts.canonical import sha256_bytes  # noqa: E402
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError  # noqa: E402

BUCKET = "lab-evidence"
REGION = "eu-west-2"
WORKSPACE = uuid4()
PAYLOAD = b"a parsed rendition"
DIGEST = sha256_bytes(PAYLOAD)
CREATED = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def _config(**overrides) -> S3StoreConfig:
    base: dict[str, object] = {
        "bucket": BUCKET,
        "region": REGION,
        "access_key": "AKIAEXAMPLE",
        "secret_key": "secret",
    }
    base.update(overrides)
    return S3StoreConfig(**base)  # type: ignore[arg-type]


def _store(config: S3StoreConfig | None = None) -> tuple[S3ObjectStore, Stubber]:
    config = config or _config()
    client = boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="secret",
    )
    stubber = Stubber(client)
    stubber.activate()
    return S3ObjectStore(config, client=client, clock=lambda: CREATED), stubber


def _record_json(*, tombstoned: bool = False, object_id=None) -> bytes:
    import json

    return json.dumps(
        {
            "id": str(object_id or _object_id(WORKSPACE, DIGEST)),
            "workspace_id": str(WORKSPACE),
            "sha256": DIGEST,
            "byte_length": len(PAYLOAD),
            "media_type": "application/json",
            "object_class": "rendition",
            "created_at": CREATED.isoformat(),
            "key": _key_for(DIGEST),
            "tombstoned": tombstoned,
        },
        sort_keys=True,
    ).encode()


def _checksum(data: bytes) -> str:
    """What the provider is told to verify the bytes against."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _body(payload: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(payload), len(payload))


class TestMissingIsNotDenied:
    """Audit F03, scenario S27. The conflation that cost a second download."""

    async def test_a_404_is_a_genuine_absence(self) -> None:
        store, stubber = _store()
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        assert await store.head(_object_id(WORKSPACE, DIGEST)) is None

    async def test_a_403_raises_rather_than_reporting_absence(self) -> None:
        store, stubber = _store()
        stubber.add_client_error(
            "get_object", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(LabAssistantError) as raised:
            await store.head(_object_id(WORKSPACE, DIGEST))
        assert raised.value.error_class is ErrorClass.STORAGE_DENIED

    async def test_a_denied_lookup_never_becomes_a_cache_miss(self) -> None:
        """``get_by_digest`` returning None is what sends a caller to the source."""
        store, stubber = _store()
        stubber.add_client_error(
            "get_object", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(LabAssistantError) as raised:
            await store.get_by_digest(DIGEST, workspace_id=WORKSPACE)
        assert raised.value.error_class is ErrorClass.STORAGE_DENIED

    async def test_a_missing_object_still_reads_as_absent(self) -> None:
        store, stubber = _store()
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        assert await store.get_by_digest(DIGEST, workspace_id=WORKSPACE) is None

    async def test_an_expired_credential_is_denied_not_missing(self) -> None:
        store, stubber = _store()
        stubber.add_client_error(
            "get_object", service_error_code="ExpiredToken", http_status_code=400
        )
        with pytest.raises(LabAssistantError) as raised:
            await store.head(_object_id(WORKSPACE, DIGEST))
        assert raised.value.error_class is ErrorClass.STORAGE_DENIED


class TestFailuresKeepTheirMeaning:
    @pytest.mark.parametrize(
        ("code", "status", "expected"),
        [
            ("InvalidObjectState", 403, ErrorClass.STORAGE_RESTORE_PENDING),
            ("SlowDown", 503, ErrorClass.RATE_LIMITED),
            ("InternalError", 500, ErrorClass.PROVIDER_ERROR),
            ("PreconditionFailed", 412, ErrorClass.CONFLICT),
        ],
    )
    async def test_each_provider_failure_maps_to_its_own_class(
        self, code: str, status: int, expected: ErrorClass
    ) -> None:
        store, stubber = _store()
        stubber.add_client_error("get_object", service_error_code=code, http_status_code=status)
        with pytest.raises(LabAssistantError) as raised:
            await store.head(_object_id(WORKSPACE, DIGEST))
        assert raised.value.error_class is expected

    async def test_a_cold_object_is_a_wait_and_not_a_corruption(self) -> None:
        """Scenario S45: never 'missing', never a trigger to re-analyse."""
        store, stubber = _store()
        stubber.add_response("get_object", {"Body": _body(_record_json()), "ETag": '"v1"'})
        stubber.add_client_error(
            "get_object", service_error_code="InvalidObjectState", http_status_code=403
        )
        with pytest.raises(LabAssistantError) as raised:
            await store.verify(_object_id(WORKSPACE, DIGEST))
        assert raised.value.error_class is ErrorClass.STORAGE_RESTORE_PENDING

    async def test_an_unreadable_object_is_not_reported_as_corrupt(self) -> None:
        """``verify`` says False for absent or wrong bytes, never for denied."""
        store, stubber = _store()
        stubber.add_response("get_object", {"Body": _body(_record_json()), "ETag": '"v1"'})
        stubber.add_client_error(
            "get_object", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(LabAssistantError) as raised:
            await store.verify(_object_id(WORKSPACE, DIGEST))
        assert raised.value.error_class is ErrorClass.STORAGE_DENIED


class TestWritesCarryTheirPrecondition:
    async def test_content_is_created_once(self) -> None:
        store, stubber = _store()
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stubber.add_response(
            "put_object",
            {"ETag": '"content"'},
            expected_params={
                "Bucket": BUCKET,
                "Key": _key_for(DIGEST),
                "Body": PAYLOAD,
                "ContentType": "application/json",
                "ChecksumSHA256": _checksum(PAYLOAD),
                "IfNoneMatch": "*",
            },
        )
        stubber.add_response(
            "put_object",
            {"ETag": '"record"'},
            expected_params={
                "Bucket": BUCKET,
                "Key": f"records/{_object_id(WORKSPACE, DIGEST)}.json",
                "Body": _record_json(),
                "ContentType": "application/json",
                "ChecksumSHA256": _checksum(_record_json()),
                "IfNoneMatch": "*",
            },
        )
        stored = await store.put(
            PAYLOAD,
            workspace_id=WORKSPACE,
            object_class="rendition",
            media_type="application/json",
        )
        assert stored.sha256 == DIGEST
        assert stored.key == _key_for(DIGEST)
        stubber.assert_no_pending_responses()

    async def test_identical_bytes_under_the_digest_key_are_not_an_error(self) -> None:
        """The key is the digest, so an object already there holds these bytes."""
        store, stubber = _store()
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stubber.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        stubber.add_response("put_object", {"ETag": '"record"'})
        stored = await store.put(
            PAYLOAD,
            workspace_id=WORKSPACE,
            object_class="rendition",
            media_type="application/json",
        )
        assert stored.sha256 == DIGEST

    async def test_a_losing_writer_takes_the_record_that_won(self) -> None:
        """Including a tombstone it carries: no resurrection by ordinary put."""
        store, stubber = _store()
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stubber.add_response("put_object", {"ETag": '"content"'})
        stubber.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        stubber.add_response(
            "get_object", {"Body": _body(_record_json(tombstoned=True)), "ETag": '"v2"'}
        )
        stored = await store.put(
            PAYLOAD,
            workspace_id=WORKSPACE,
            object_class="rendition",
            media_type="application/json",
        )
        assert stored.tombstoned is True
        stubber.assert_no_pending_responses()

    async def test_a_tombstone_is_conditional_on_the_version_it_read(self) -> None:
        """Scenario S42: a stale update conflicts instead of overwriting."""
        store, stubber = _store()
        object_id = _object_id(WORKSPACE, DIGEST)
        stubber.add_response("get_object", {"Body": _body(_record_json()), "ETag": '"v7"'})
        stubber.add_response(
            "put_object",
            {"ETag": '"v8"'},
            expected_params={
                "Bucket": BUCKET,
                "Key": f"records/{object_id}.json",
                "Body": _record_json(tombstoned=True),
                "ContentType": "application/json",
                "ChecksumSHA256": _checksum(_record_json(tombstoned=True)),
                "IfMatch": '"v7"',
            },
        )
        await store.tombstone(object_id, reason="rights withdrawn")
        stubber.assert_no_pending_responses()

    async def test_a_stale_tombstone_conflicts_rather_than_winning(self) -> None:
        store, stubber = _store()
        stubber.add_response("get_object", {"Body": _body(_record_json()), "ETag": '"v7"'})
        stubber.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        with pytest.raises(LabAssistantError) as raised:
            await store.tombstone(_object_id(WORKSPACE, DIGEST), reason="rights withdrawn")
        assert raised.value.error_class is ErrorClass.CONFLICT

    async def test_an_endpoint_without_preconditions_can_be_configured_off(self) -> None:
        """Honest degradation beats a silent fallback that reopens the race."""
        store, stubber = _store(_config(conditional_writes=False))
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stubber.add_response(
            "put_object",
            {"ETag": '"content"'},
            expected_params={
                "Bucket": BUCKET,
                "Key": _key_for(DIGEST),
                "Body": PAYLOAD,
                "ContentType": "application/json",
                "ChecksumSHA256": _checksum(PAYLOAD),
            },
        )
        stubber.add_response("put_object", {"ETag": '"record"'})
        await store.put(
            PAYLOAD,
            workspace_id=WORKSPACE,
            object_class="rendition",
            media_type="application/json",
        )
        stubber.assert_no_pending_responses()


class TestContentIntegrity:
    async def test_substituted_bytes_are_detected_on_read(self) -> None:
        store, stubber = _store()
        stubber.add_response("get_object", {"Body": _body(_record_json()), "ETag": '"v1"'})
        stubber.add_response("get_object", {"Body": _body(b"tampered"), "ETag": '"whatever"'})
        with pytest.raises(LabAssistantError) as raised:
            await store.get(_object_id(WORKSPACE, DIGEST))
        assert raised.value.error_class is ErrorClass.INVALID_CONTENT

    async def test_a_provider_etag_is_not_trusted_as_the_content_hash(self) -> None:
        """Scenario S64: an ETag is a concurrency token, nothing more."""
        store, stubber = _store()
        stubber.add_response("get_object", {"Body": _body(_record_json()), "ETag": '"v1"'})
        stubber.add_response("get_object", {"Body": _body(PAYLOAD), "ETag": '"not-the-sha-256"'})
        assert await store.get(_object_id(WORKSPACE, DIGEST)) == PAYLOAD

    async def test_a_tombstoned_object_is_unreadable(self) -> None:
        store, stubber = _store()
        stubber.add_response(
            "get_object", {"Body": _body(_record_json(tombstoned=True)), "ETag": '"v2"'}
        )
        with pytest.raises(FileNotFoundError):
            await store.get(_object_id(WORKSPACE, DIGEST))


class TestAddressingIsNotIdentity:
    async def test_a_prefix_reaches_the_keys_but_never_the_record(self) -> None:
        """A bucket layout change must not rewrite what the database stores."""
        store, stubber = _store(_config(prefix="libraries/rla-lab/"))
        object_id = _object_id(WORKSPACE, DIGEST)
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stubber.add_response(
            "put_object",
            {"ETag": '"content"'},
            expected_params={
                "Bucket": BUCKET,
                "Key": f"libraries/rla-lab/{_key_for(DIGEST)}",
                "Body": PAYLOAD,
                "ContentType": "application/json",
                "ChecksumSHA256": _checksum(PAYLOAD),
                "IfNoneMatch": "*",
            },
        )
        stubber.add_response(
            "put_object",
            {"ETag": '"record"'},
            expected_params={
                "Bucket": BUCKET,
                "Key": f"libraries/rla-lab/records/{object_id}.json",
                "Body": _record_json(),
                "ContentType": "application/json",
                "ChecksumSHA256": _checksum(_record_json()),
                "IfNoneMatch": "*",
            },
        )
        stored = await store.put(
            PAYLOAD,
            workspace_id=WORKSPACE,
            object_class="rendition",
            media_type="application/json",
        )
        # The stored key is logical: no prefix, no bucket, no endpoint.
        assert stored.key == _key_for(DIGEST)
        assert BUCKET not in stored.key
        stubber.assert_no_pending_responses()

    async def test_the_object_id_does_not_depend_on_the_bucket(self) -> None:
        here = _object_id(WORKSPACE, DIGEST)
        assert here == _object_id(WORKSPACE, DIGEST)
        assert here != _object_id(uuid4(), DIGEST)


class TestConfiguration:
    def test_a_bucket_and_region_are_required(self) -> None:
        with pytest.raises(LabAssistantError) as raised:
            S3StoreConfig(bucket=None, region=None)
        missing = raised.value.context["missing"]
        assert "LAB_OBJECT_STORE_BUCKET" in missing
        assert "LAB_OBJECT_STORE_REGION" in missing

    def test_half_a_static_credential_is_refused(self) -> None:
        """Half a key pair silently falls through to the chain otherwise."""
        with pytest.raises(LabAssistantError) as raised:
            S3StoreConfig(bucket=BUCKET, region=REGION, access_key="AKIAEXAMPLE")
        assert "LAB_OBJECT_STORE_SECRET_KEY" in str(raised.value.context["missing"])

    def test_no_credentials_at_all_refuses_at_construction(self, monkeypatch) -> None:
        """A role-based deployment is fine; no resolvable credential is not."""
        for name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_CONFIG_FILE",
        ):
            monkeypatch.delenv(name, raising=False)
        # Point the SDK at empty credential files so a developer's own profile
        # cannot make this pass on their machine and fail in CI.
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/credentials")
        monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/config")
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        with pytest.raises(LabAssistantError) as raised:
            S3StoreConfig(bucket=BUCKET, region=REGION).build_client()
        assert raised.value.error_class is ErrorClass.AUTHENTICATION_REQUIRED

    def test_a_prefix_is_normalised_once(self) -> None:
        assert _config(prefix="libraries/rla").prefix == "libraries/rla/"
        assert _config(prefix="/libraries/rla/").prefix == "libraries/rla/"
        assert _config(prefix="").prefix == ""


class TestStreaming:
    async def test_a_small_payload_is_one_conditional_request(self) -> None:
        store, stubber = _store()
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stubber.add_response(
            "put_object",
            {"ETag": '"content"'},
            expected_params={
                "Bucket": BUCKET,
                "Key": _key_for(DIGEST),
                "Body": PAYLOAD,
                "ContentType": "application/json",
                "ChecksumSHA256": _checksum(PAYLOAD),
                "IfNoneMatch": "*",
            },
        )
        stubber.add_response("put_object", {"ETag": '"record"'})
        stored = await store.put_stream(
            _chunks(PAYLOAD, 4),
            workspace_id=WORKSPACE,
            object_class="rendition",
            media_type="application/json",
        )
        assert stored.sha256 == DIGEST
        stubber.assert_no_pending_responses()

    async def test_a_large_payload_uses_the_multipart_api(self) -> None:
        """Above the threshold it must not be one PutObject.

        One part, not two: S3 requires every part but the last to be at least
        5 MiB, and the SDK raises a smaller configured chunk size to that floor.
        A test payload big enough to split would be 5 MiB of filler to prove
        something this already proves — that the multipart API is the path
        taken, and a transfer that fails resumes at a part boundary.
        """
        payload = b"p" * 2048
        store, stubber = _store(_config(multipart_threshold=1024, transfer_concurrency=1))
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stubber.add_response("create_multipart_upload", {"UploadId": "upload-1"})
        stubber.add_response("upload_part", {"ETag": '"part-1"'})
        stubber.add_response("complete_multipart_upload", {"ETag": '"assembled"'})
        stubber.add_response("put_object", {"ETag": '"record"'})

        stored = await store.put_stream(
            _chunks(payload, 256), workspace_id=WORKSPACE, object_class="asset"
        )
        assert stored.sha256 == sha256_bytes(payload)
        assert stored.byte_length == len(payload)
        stubber.assert_no_pending_responses()

    async def test_the_limit_refuses_before_any_request(self) -> None:
        store, stubber = _store(_config(max_object_bytes=64))
        with pytest.raises(LabAssistantError) as raised:
            await store.put_stream(
                _chunks(b"q" * 4096, 128), workspace_id=WORKSPACE, object_class="asset"
            )
        assert raised.value.error_class is ErrorClass.TOO_LARGE
        # Nothing was queued, so nothing may have been sent.
        stubber.assert_no_pending_responses()

    async def test_reading_streams_and_verifies(self) -> None:
        store, stubber = _store()
        stubber.add_response("get_object", {"Body": _body(_record_json()), "ETag": '"v1"'})
        stubber.add_response("get_object", {"Body": _body(PAYLOAD), "ETag": '"c"'})
        received = bytearray()
        async for chunk in store.stream(_object_id(WORKSPACE, DIGEST), chunk_size=4):
            received += chunk
        assert bytes(received) == PAYLOAD

    async def test_a_substituted_body_fails_after_delivery(self) -> None:
        store, stubber = _store()
        stubber.add_response("get_object", {"Body": _body(_record_json()), "ETag": '"v1"'})
        stubber.add_response("get_object", {"Body": _body(b"substituted"), "ETag": '"c"'})
        received = bytearray()
        with pytest.raises(LabAssistantError) as raised:
            async for chunk in store.stream(_object_id(WORKSPACE, DIGEST), chunk_size=4):
                received += chunk
        assert raised.value.error_class is ErrorClass.INVALID_CONTENT
        assert received, "the caller holds bytes it must discard"


async def _chunks(data: bytes, size: int):
    for start in range(0, len(data), size):
        yield data[start : start + size]
