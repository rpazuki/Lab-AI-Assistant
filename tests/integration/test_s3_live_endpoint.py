"""The S3 adapter against a real endpoint. Opt-in, never in the offline suite.

`botocore.stub.Stubber` validates request parameters and error shapes, which is
most of the contract — but it never signs a request and never parses a real
response, and the defect that did the most damage in the signer this adapter
replaced was exactly of that kind: a presigned URL whose signature did not cover
its own query string. No parameter assertion can catch that. Only a provider
rejecting the URL can.

Run against MinIO, Ceph, an institutional gateway or a scratch AWS bucket::

    LAB_TEST_S3_ENDPOINT=http://localhost:9000 \\
    LAB_TEST_S3_BUCKET=lab-assistant-scratch \\
    LAB_TEST_S3_ACCESS_KEY=... LAB_TEST_S3_SECRET_KEY=... \\
    .venv/bin/pytest -q tests/integration/test_s3_live_endpoint.py

Point it at a disposable bucket. It writes objects under a random prefix and
removes them, but it is a test and the bucket is yours.

A pass here is evidence about *the endpoint it ran against*. MinIO passing is not
AWS passing: conditional writes in particular are implemented unevenly across
S3-compatible gateways, which is the whole reason the adapter has a flag for
them.
"""

from __future__ import annotations

import contextlib
import os
from uuid import uuid4

import pytest

boto3 = pytest.importorskip("boto3")
import httpx  # noqa: E402

from lab_assistant.adapters.object_store.s3 import S3ObjectStore, S3StoreConfig  # noqa: E402
from lab_assistant.contracts.canonical import sha256_bytes  # noqa: E402
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError  # noqa: E402

pytestmark = pytest.mark.object_store

ENDPOINT = os.environ.get("LAB_TEST_S3_ENDPOINT")
BUCKET = os.environ.get("LAB_TEST_S3_BUCKET")
ACCESS_KEY = os.environ.get("LAB_TEST_S3_ACCESS_KEY")
SECRET_KEY = os.environ.get("LAB_TEST_S3_SECRET_KEY")
REGION = os.environ.get("LAB_TEST_S3_REGION", "us-east-1")

if not (BUCKET and ACCESS_KEY and SECRET_KEY):
    pytest.skip(
        "LAB_TEST_S3_BUCKET/ACCESS_KEY/SECRET_KEY are not set; live object-store tests skipped",
        allow_module_level=True,
    )


@pytest.fixture
def prefix() -> str:
    """One run writes under its own prefix, so parallel runs cannot collide."""
    return f"lab-test/{uuid4()}/"


@pytest.fixture
def store(prefix: str):
    config = S3StoreConfig(
        bucket=BUCKET,
        region=REGION,
        endpoint=ENDPOINT,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        prefix=prefix,
    )
    client = config.build_client()
    with contextlib.suppress(Exception):  # already exists, or not ours to create
        client.create_bucket(Bucket=BUCKET)
    yield S3ObjectStore(config, client=client)
    # Leave the bucket as we found it.
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        keys = [{"Key": row["Key"]} for row in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})


class TestRealRoundTrip:
    async def test_bytes_survive_a_signed_round_trip(self, store) -> None:
        workspace = uuid4()
        payload = b"a real rendition over a real signature"
        stored = await store.put(
            payload, workspace_id=workspace, object_class="rendition", media_type="text/plain"
        )
        assert stored.sha256 == sha256_bytes(payload)
        assert await store.get(stored.id) == payload
        assert await store.verify(stored.id) is True
        head = await store.head(stored.id)
        assert head is not None and head.byte_length == len(payload)

    async def test_an_unknown_object_is_absent_not_an_error(self, store) -> None:
        assert await store.head(uuid4()) is None

    async def test_readiness_reaches_the_bucket(self, store) -> None:
        await store.ping()


class TestPresignedUrlsAreAccepted:
    """The defect no offline test could have found."""

    async def test_a_presigned_url_actually_fetches_the_bytes(self, store) -> None:
        workspace = uuid4()
        payload = b"bytes that must arrive through the signed URL"
        stored = await store.put(payload, workspace_id=workspace, object_class="asset")
        url = await store.signed_url(stored.id, expires_seconds=120)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
        assert response.status_code == 200, response.text[:400]
        assert response.content == payload

    async def test_a_tampered_presigned_url_is_rejected(self, store) -> None:
        """The signature covers the query, so an added parameter invalidates it."""
        workspace = uuid4()
        stored = await store.put(b"payload", workspace_id=workspace, object_class="asset")
        url = await store.signed_url(stored.id, expires_seconds=120)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{url}&x-lab-added-after-signing=1")
        assert response.status_code in (400, 403)


class TestDeniedIsNotMissing:
    async def test_a_wrong_credential_is_denied_rather_than_absent(self, prefix: str) -> None:
        """The conflation that sent a reader back to the source for free bytes."""
        config = S3StoreConfig(
            bucket=BUCKET,
            region=REGION,
            endpoint=ENDPOINT,
            access_key=ACCESS_KEY,
            secret_key="definitely-not-the-secret",
            prefix=prefix,
        )
        store = S3ObjectStore(config)
        with pytest.raises(LabAssistantError) as raised:
            await store.head(uuid4())
        assert raised.value.error_class is ErrorClass.STORAGE_DENIED


class TestConditionalWrites:
    async def test_the_same_bytes_twice_is_a_no_op(self, store) -> None:
        workspace = uuid4()
        payload = b"stored once, registered twice"
        first = await store.put(payload, workspace_id=workspace, object_class="asset")
        second = await store.put(payload, workspace_id=workspace, object_class="asset")
        assert first.id == second.id
        assert await store.get(first.id) == payload

    async def test_a_tombstone_holds_against_a_repeat_write(self, store) -> None:
        """Scenario S42, against a provider rather than a stub."""
        workspace = uuid4()
        payload = b"withdrawn material"
        stored = await store.put(payload, workspace_id=workspace, object_class="asset")
        await store.tombstone(stored.id, reason="rights withdrawn")
        again = await store.put(payload, workspace_id=workspace, object_class="asset")
        assert again.tombstoned is True
        with pytest.raises(FileNotFoundError):
            await store.get(stored.id)

    async def test_the_endpoint_honours_if_none_match(self, store) -> None:
        """If this fails, set object_store_conditional_writes=False knowingly."""
        workspace = uuid4()
        stored = await store.put(b"conditional", workspace_id=workspace, object_class="asset")
        # Reaching past the facade on purpose: the assertion is about what the
        # endpoint does with the header, not about the adapter's own handling.
        key = store._object_key(stored.key)
        with pytest.raises(Exception) as raised:
            store._client.put_object(Bucket=BUCKET, Key=key, Body=b"different", IfNoneMatch="*")
        assert "PreconditionFailed" in str(raised.value) or "412" in str(raised.value)
