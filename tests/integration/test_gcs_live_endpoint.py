"""The GCS adapter against a real endpoint. Opt-in, never in the offline suite.

The fake client in `tests/contract/test_gcs_object_store.py` proves the adapter
*sends* `if_generation_match`. It cannot prove GCS honours it, that a real 404
arrives as `NotFound`, or that a resumable upload reassembles into the bytes it
was given. Only a provider can, and the whole reason ADR-0023 exists is that a
hand-written signer passed every test anyone had written and was wrong in four
ways a provider would have caught immediately.

Run against `fsouza/fake-gcs-server`, or a scratch bucket in a real project::

    docker run -d -p 4443:4443 fsouza/fake-gcs-server:latest \\
        -scheme http -public-host localhost:4443 -port 4443

    LAB_TEST_GCS_ENDPOINT=http://localhost:4443 \\
    LAB_TEST_GCS_BUCKET=lab-assistant-scratch \\
    .venv/bin/pytest -q tests/integration/test_gcs_live_endpoint.py

Point it at a disposable bucket. It writes under a random prefix, but it is a
test and the bucket is yours.

A pass here is evidence about *the endpoint it ran against*. An emulator
passing is not Google passing: it does not enforce IAM, and its signing is not
Google's. What it cannot demonstrate is skipped with a reason rather than
asserted — the same rule the S3 suite follows.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("google.cloud.storage")

from lab_assistant.adapters.object_store.gcs import (
    GcsObjectStore,
    GcsStoreConfig,
)
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

pytestmark = pytest.mark.object_store

ENDPOINT = os.environ.get("LAB_TEST_GCS_ENDPOINT")
BUCKET = os.environ.get("LAB_TEST_GCS_BUCKET")
PROJECT = os.environ.get("LAB_TEST_GCS_PROJECT", "lab-assistant-test")

if not BUCKET:
    pytest.skip(
        "LAB_TEST_GCS_BUCKET is not set; live object-store tests skipped",
        allow_module_level=True,
    )


@pytest.fixture
def prefix() -> str:
    """One run writes under its own prefix, so parallel runs cannot collide."""
    return f"lab-test/{uuid4()}/"


@pytest.fixture
async def store(prefix: str):
    store = GcsObjectStore(
        GcsStoreConfig(bucket=BUCKET, project=PROJECT, endpoint=ENDPOINT, prefix=prefix)
    )
    yield store
    await store.aclose()


@pytest.fixture(scope="module")
def signs() -> bool:
    """Can this endpoint produce a signed URL at all?

    A V4 signature needs a service-account key or IAM `signBlob`. An emulator
    has neither, and an anonymous credential cannot sign. Asserting against an
    endpoint that cannot sign would prove nothing; skipping says so.
    """
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    if ENDPOINT:
        client = storage.Client(
            project=PROJECT,
            credentials=AnonymousCredentials(),
            client_options={"api_endpoint": ENDPOINT},
        )
    else:  # pragma: no cover - only on a real project
        client = storage.Client(project=PROJECT)
    try:
        from datetime import timedelta

        client.bucket(BUCKET).blob("probe").generate_signed_url(
            version="v4", expiration=timedelta(seconds=60), method="GET"
        )
    except Exception:
        return False
    return True


class TestRealRoundTrip:
    async def test_bytes_survive_a_round_trip(self, store) -> None:
        workspace = uuid4()
        payload = b"evidence that must come back exactly"
        stored = await store.put(payload, workspace_id=workspace, object_class="asset")
        assert await store.get(stored.id) == payload
        assert await store.verify(stored.id) is True

    async def test_an_unknown_object_is_absent_not_an_error(self, store) -> None:
        """A real 404 must arrive as an absence, not as a provider failure —
        the caller's next move differs completely."""
        assert await store.head(uuid4()) is None

    async def test_readiness_reaches_the_bucket(self, store) -> None:
        await store.ping()

    async def test_a_streamed_payload_round_trips(self, store) -> None:
        """Large enough to take the resumable path, read back in chunks."""

        async def source():
            for _ in range(600):
                yield b"y" * 1024

        stored = await store.put_stream(source(), workspace_id=uuid4(), object_class="asset")
        assert stored.byte_length == 600 * 1024

        seen = 0
        async for chunk in store.stream(stored.id):
            seen += len(chunk)
        assert seen == stored.byte_length
        assert await store.verify(stored.id) is True


class TestGenerationPreconditions:
    """The behaviour the whole adapter rests on, against a provider that
    actually implements it."""

    async def test_the_same_bytes_twice_is_a_no_op(self, store) -> None:
        workspace = uuid4()
        payload = b"stored once, registered twice"
        first = await store.put(payload, workspace_id=workspace, object_class="asset")
        second = await store.put(payload, workspace_id=workspace, object_class="asset")
        assert first.id == second.id
        assert await store.get(first.id) == payload

    async def test_a_tombstone_holds_against_a_repeat_write(self, store) -> None:
        """Scenario S42, against a provider rather than a fake: re-storing
        identical bytes must not clear a withdrawal."""
        workspace = uuid4()
        stored = await store.put(b"withdrawn", workspace_id=workspace, object_class="asset")
        await store.tombstone(stored.id, reason="rights revoked")

        again = await store.put(b"withdrawn", workspace_id=workspace, object_class="asset")
        assert again.tombstoned is True
        with pytest.raises(FileNotFoundError):
            await store.get(stored.id)

    async def test_the_endpoint_refuses_a_stale_generation(self, store) -> None:
        """Compare-and-swap, enforced by the provider.

        If this passes against a fake and fails here, the endpoint does not
        implement generation preconditions and nothing in this adapter's
        concurrency story holds on it.
        """
        stored = await store.put(b"contested", workspace_id=uuid4(), object_class="asset")
        record, generation = await store._read_record(stored.id)
        assert record is not None and generation is not None

        await store.tombstone(stored.id, reason="the first writer wins")

        with pytest.raises(LabAssistantError) as raised:
            await store._write_record(record, expected_generation=generation)
        assert raised.value.error_class is ErrorClass.CONFLICT

    async def test_a_create_refuses_an_existing_record(self, store) -> None:
        stored = await store.put(b"once", workspace_id=uuid4(), object_class="asset")
        record, _generation = await store._read_record(stored.id)
        assert record is not None
        with pytest.raises(LabAssistantError) as raised:
            await store._write_record(record, create_only=True)
        assert raised.value.error_class is ErrorClass.CONFLICT


class TestIntegrityIsCheckedByThisApplication:
    async def test_a_substituted_object_is_detected_on_read(self, store, prefix) -> None:
        """A provider checksum protects the transfer. The citation depends on
        the application digest, so that is what is checked."""
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import storage

        if not ENDPOINT:  # pragma: no cover - only meaningful where we can overwrite
            pytest.skip("substituting an object needs the emulator")

        stored = await store.put(b"evidence", workspace_id=uuid4(), object_class="asset")
        client = storage.Client(
            project=PROJECT,
            credentials=AnonymousCredentials(),
            client_options={"api_endpoint": ENDPOINT},
        )
        blob = client.bucket(BUCKET).blob(f"{prefix}{stored.key}")
        blob.upload_from_string(b"substituted", content_type="application/octet-stream")

        with pytest.raises(LabAssistantError) as raised:
            await store.get(stored.id)
        assert raised.value.error_class is ErrorClass.INVALID_CONTENT
        assert await store.verify(stored.id) is False


class TestSignedUrls:
    async def test_a_signed_url_is_produced(self, store, signs) -> None:
        if not signs:
            pytest.skip(
                "this endpoint cannot sign: a V4 signature needs a service-account key "
                "or IAM signBlob, and an emulator has neither. Run against a real "
                "project before relying on presigned downloads."
            )
        stored = await store.put(b"signed", workspace_id=uuid4(), object_class="asset")
        url = await store.signed_url(stored.id, expires_seconds=120)
        assert url.startswith("http")

    async def test_signing_fails_loudly_rather_than_handing_out_a_bad_url(
        self, store, signs
    ) -> None:
        """A URL that 403s when somebody clicks it is a worse answer than an
        error here: the caller treats a failure to sign as a reason to serve
        the bytes through the API instead."""
        if signs:
            pytest.skip("this endpoint can sign, so there is no failure to observe")
        stored = await store.put(b"unsignable", workspace_id=uuid4(), object_class="asset")
        with pytest.raises(Exception) as raised:
            await store.signed_url(stored.id, expires_seconds=120)
        assert not isinstance(raised.value, FileNotFoundError)
