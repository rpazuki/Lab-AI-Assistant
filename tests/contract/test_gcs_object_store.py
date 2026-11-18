"""The GCS adapter's contract, offline (ADR-0024).

CI has no emulator, so this is the layer that has to carry the properties which
fail *silently* when they are wrong:

- the precondition arguments are actually sent. A create without
  `if_generation_match=0` is a check-then-put that works perfectly until two
  writers overlap, and a record update without a generation is how a stale
  writer resurrects a tombstone. Neither shows up in a round-trip test;
- a provider failure maps onto the right class. "Denied" read as "absent" sends
  a reader back to the source to pay for bytes already in the bucket, and no
  functional test catches it because both look like "no object";
- the key convention and prefix are built the same way as every other adapter,
  because the database stores the logical key and a change here orphans it.

What a fake client cannot prove — that GCS honours a generation precondition,
that its 404 really is a `NotFound` — belongs to
`tests/integration/test_gcs_live_endpoint.py`, which runs against a real GCS
HTTP implementation.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from lab_assistant.adapters.object_store.gcs import (
    GcsObjectStore,
    GcsStoreConfig,
    _translate,
)
from lab_assistant.contracts.canonical import sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

pytest.importorskip("google.cloud.storage")

WORKSPACE = UUID("11111111-2222-3333-4444-555555555555")


class FakeBlob:
    def __init__(self, bucket: FakeBucket, key: str) -> None:
        self._bucket = bucket
        self.key = key
        self.md5_hash: str | None = None
        self.chunk_size: int | None = None

    # ── what the adapter calls ──────────────────────────────────────────
    @property
    def generation(self) -> int | None:
        stored = self._bucket.objects.get(self.key)
        return stored[1] if stored else None

    def upload_from_string(self, data, *, content_type, if_generation_match, timeout=None):
        self._bucket.calls.append(
            {
                "op": "upload_from_string",
                "key": self.key,
                "if_generation_match": if_generation_match,
                "content_type": content_type,
                "md5": self.md5_hash,
            }
        )
        self._bucket.precondition(self.key, if_generation_match)
        payload = data if isinstance(data, bytes) else data.encode("utf-8")
        self._bucket.write(self.key, payload)

    def upload_from_file(self, handle, *, size, content_type, if_generation_match, timeout=None):
        self._bucket.calls.append(
            {
                "op": "upload_from_file",
                "key": self.key,
                "if_generation_match": if_generation_match,
                "chunk_size": self.chunk_size,
                "md5": self.md5_hash,
            }
        )
        self._bucket.precondition(self.key, if_generation_match)
        self._bucket.write(self.key, handle.read())

    def download_as_bytes(self):
        stored = self._bucket.objects.get(self.key)
        if stored is None:
            from google.api_core import exceptions as api

            raise api.NotFound(f"no such object: {self.key}")
        return stored[0]

    def open(self, mode="rb"):
        import io

        return io.BytesIO(self.download_as_bytes())

    def generate_signed_url(self, *, version, expiration, method):
        return f"https://signed.invalid/{self.key}?v={version}&m={method}"


class FakeBucket:
    def __init__(self) -> None:
        #: key -> (payload, generation)
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.calls: list[dict[str, Any]] = []
        self._next_generation = 1

    def blob(self, key: str, generation: int | None = None) -> FakeBlob:
        return FakeBlob(self, key)

    def precondition(self, key: str, if_generation_match: int | None) -> None:
        """The emulated half of GCS's precondition contract."""
        from google.api_core import exceptions as api

        current = self.objects.get(key)
        if if_generation_match is None:
            return
        if if_generation_match == 0 and current is not None:
            raise api.PreconditionFailed(f"object exists: {key}")
        if if_generation_match != 0 and (current is None or current[1] != if_generation_match):
            raise api.PreconditionFailed(f"generation mismatch: {key}")

    def write(self, key: str, payload: bytes) -> None:
        self.objects[key] = (payload, self._next_generation)
        self._next_generation += 1


class FakeClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket())

    def list_blobs(self, bucket, *, prefix=None, max_results=None):
        return list(bucket.objects)[:max_results]


@pytest.fixture
def store() -> GcsObjectStore:
    client = FakeClient()
    config = GcsStoreConfig(bucket="corpus", project="lab", prefix="lab/")
    return GcsObjectStore(config, client=client)


def _bucket(store: GcsObjectStore) -> FakeBucket:
    return store._client.bucket("corpus")


class TestWritesCarryTheirPrecondition:
    """A create without `if_generation_match=0` is a check-then-put that works
    until two writers overlap, and nothing functional distinguishes them."""

    async def test_content_is_created_only_if_absent(self, store) -> None:
        await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        content = [
            call
            for call in _bucket(store).calls
            if call["key"].startswith("lab/sha256/") and call["op"] == "upload_from_string"
        ]
        assert content, "no content write was issued"
        assert all(call["if_generation_match"] == 0 for call in content)

    async def test_the_record_is_created_only_if_absent(self, store) -> None:
        await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        records = [call for call in _bucket(store).calls if "/records/" in call["key"]]
        assert records
        assert records[0]["if_generation_match"] == 0

    async def test_a_tombstone_is_a_compare_and_swap(self, store) -> None:
        """Conditional on the generation just read, so a slow writer holding an
        old record cannot put it back."""
        stored = await store.put(b"withdrawn", workspace_id=WORKSPACE, object_class="asset")
        _bucket(store).calls.clear()
        await store.tombstone(stored.id, reason="rights revoked")

        update = next(call for call in _bucket(store).calls if "/records/" in call["key"])
        assert update["if_generation_match"] not in (None, 0), (
            "the record update carried no generation, so it is an unconditional "
            "overwrite and a stale writer can resurrect the tombstone"
        )

    async def test_a_stale_writer_is_refused(self, store) -> None:
        stored = await store.put(b"contested", workspace_id=WORKSPACE, object_class="asset")
        record, generation = await store._read_record(stored.id)
        assert record is not None
        # Somebody else writes in between, moving the generation on.
        await store.tombstone(stored.id, reason="first writer")
        with pytest.raises(LabAssistantError) as raised:
            await store._write_record(record, expected_generation=generation)
        assert raised.value.error_class is ErrorClass.CONFLICT

    async def test_storing_the_same_bytes_twice_is_a_no_op(self, store) -> None:
        first = await store.put(b"same", workspace_id=WORKSPACE, object_class="asset")
        second = await store.put(b"same", workspace_id=WORKSPACE, object_class="asset")
        assert first.id == second.id
        assert first.sha256 == second.sha256

    async def test_a_tombstone_survives_the_same_bytes_arriving_again(self, store) -> None:
        """Re-storing identical bytes must not clear a withdrawal."""
        stored = await store.put(b"withdrawn", workspace_id=WORKSPACE, object_class="asset")
        await store.tombstone(stored.id, reason="rights revoked")
        again = await store.put(b"withdrawn", workspace_id=WORKSPACE, object_class="asset")
        assert again.tombstoned is True

    async def test_the_provider_gets_an_md5_to_check(self, store) -> None:
        """GCS has no SHA-256 field, so MD5 is what makes it reject a transfer
        damaged in flight. Identity is still the application's SHA-256."""
        await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        content = next(
            call for call in _bucket(store).calls if call["key"].startswith("lab/sha256/")
        )
        assert content["md5"], "no checksum was handed to the provider"


class TestKeysAndPrefix:
    async def test_the_logical_key_is_provider_independent(self, store) -> None:
        """The database stores the logical key; the prefix is an address."""
        stored = await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        assert stored.key.startswith("sha256/")
        assert "lab/" not in stored.key
        assert f"lab/{stored.key}" in _bucket(store).objects

    async def test_the_record_lives_in_its_own_key_space(self, store) -> None:
        stored = await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        assert f"lab/records/{stored.id}.json" in _bucket(store).objects

    async def test_the_digest_decides_the_key(self, store) -> None:
        stored = await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        digest = sha256_bytes(b"evidence")
        assert stored.key == f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    async def test_the_same_bytes_in_two_workspaces_are_two_objects(self, store) -> None:
        left = await store.put(b"shared", workspace_id=uuid4(), object_class="asset")
        right = await store.put(b"shared", workspace_id=uuid4(), object_class="asset")
        assert left.id != right.id
        assert left.key == right.key, "content is deduplicated; identity is not"


class TestReadsAreVerified:
    async def test_a_substituted_object_is_detected(self, store) -> None:
        """A provider checksum is a transport check. The citation depends on
        the application digest, so that is what is checked on read."""
        stored = await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        bucket = _bucket(store)
        bucket.objects[f"lab/{stored.key}"] = (b"substituted", 99)

        with pytest.raises(LabAssistantError) as raised:
            await store.get(stored.id)
        assert raised.value.error_class is ErrorClass.INVALID_CONTENT

    async def test_verify_reports_corruption_without_raising(self, store) -> None:
        stored = await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        _bucket(store).objects[f"lab/{stored.key}"] = (b"substituted", 99)
        assert await store.verify(stored.id) is False

    async def test_an_unregistered_object_is_absent(self, store) -> None:
        assert await store.head(uuid4()) is None
        assert await store.verify(uuid4()) is False

    async def test_a_tombstoned_payload_is_not_served(self, store) -> None:
        stored = await store.put(b"withdrawn", workspace_id=WORKSPACE, object_class="asset")
        await store.tombstone(stored.id, reason="rights revoked")
        with pytest.raises(FileNotFoundError):
            await store.get(stored.id)


class TestTheErrorTaxonomy:
    """Denied is not missing, and an unknown failure is not an answer.

    No functional test catches these: a denial and an absence both look like
    "no object", and the difference is whether a reader goes back to the source
    and pays for bytes that are sitting in the bucket.
    """

    @pytest.mark.parametrize(
        ("exception", "expected"),
        [
            ("NotFound", ErrorClass.NOT_FOUND),
            ("Forbidden", ErrorClass.STORAGE_DENIED),
            ("Unauthorized", ErrorClass.STORAGE_DENIED),
            ("PreconditionFailed", ErrorClass.CONFLICT),
            ("Conflict", ErrorClass.CONFLICT),
            ("TooManyRequests", ErrorClass.RATE_LIMITED),
            ("ServiceUnavailable", ErrorClass.SOURCE_UNAVAILABLE),
            ("GatewayTimeout", ErrorClass.TIMEOUT),
            ("DeadlineExceeded", ErrorClass.TIMEOUT),
        ],
    )
    def test_a_provider_failure_maps_to_its_class(self, exception, expected) -> None:
        from google.api_core import exceptions as api

        error = getattr(api, exception)("provider said so")
        translated = _translate(error, operation="download", key="sha256/aa/bb/cc")
        assert translated.error_class is expected

    def test_an_unknown_failure_is_a_failure_not_an_absence(self) -> None:
        translated = _translate(RuntimeError("something else"), operation="upload", key=None)
        assert translated.error_class is ErrorClass.PROVIDER_ERROR

    def test_the_operation_and_key_are_carried(self) -> None:
        from google.api_core import exceptions as api

        translated = _translate(api.NotFound("gone"), operation="download", key="records/x.json")
        assert translated.context["operation"] == "download"
        assert translated.context["key"] == "records/x.json"


class TestConfigurationIsValidatedAtConstruction:
    """A store that cannot work refuses to exist, rather than failing during an
    acquisition run hours after the deploy that broke it."""

    def test_a_missing_bucket_is_refused(self) -> None:
        with pytest.raises(LabAssistantError) as raised:
            GcsStoreConfig(bucket=None)
        assert raised.value.error_class is ErrorClass.INVALID_CONTENT

    def test_an_absolute_prefix_is_refused(self) -> None:
        """A key prefix is not a path, and '/lab' would create an empty first
        segment in every key."""
        with pytest.raises(LabAssistantError):
            GcsStoreConfig(bucket="corpus", prefix="/lab")

    def test_a_prefix_is_normalised(self) -> None:
        assert GcsStoreConfig(bucket="corpus", prefix="lab").prefix == "lab/"
        assert GcsStoreConfig(bucket="corpus", prefix="lab/").prefix == "lab/"
        assert GcsStoreConfig(bucket="corpus").prefix == ""

    def test_settings_select_the_adapter(self) -> None:
        from lab_assistant.config import Settings

        config = GcsStoreConfig.from_settings(
            Settings(
                object_store_kind="gcs",
                object_store_bucket="corpus",
                object_store_prefix="lab",
            )
        )
        assert config.bucket == "corpus"
        assert config.prefix == "lab/"


class TestTheRecordRoundTrips:
    async def test_every_field_survives(self, store) -> None:
        stored = await store.put(
            b"evidence", workspace_id=WORKSPACE, object_class="rendition", media_type="text/xml"
        )
        read = await store.head(stored.id)
        assert read is not None
        assert read.id == stored.id
        assert read.workspace_id == WORKSPACE
        assert read.sha256 == stored.sha256
        assert read.byte_length == stored.byte_length
        assert read.media_type == "text/xml"
        assert read.object_class == "rendition"
        assert read.key == stored.key

    async def test_a_shared_object_has_no_workspace(self, store) -> None:
        stored = await store.put(b"shared", workspace_id=None, object_class="asset")
        read = await store.head(stored.id)
        assert read is not None and read.workspace_id is None

    async def test_the_record_is_json_a_person_can_read(self, store) -> None:
        """An operator looking at a bucket during an incident should not need
        this repository to work out what an object is."""
        stored = await store.put(b"evidence", workspace_id=WORKSPACE, object_class="asset")
        payload, _generation = _bucket(store).objects[f"lab/records/{stored.id}.json"]
        parsed = json.loads(payload)
        assert parsed["sha256"] == stored.sha256
        assert parsed["object_class"] == "asset"
