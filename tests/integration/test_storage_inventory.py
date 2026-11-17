"""The sweep between the database and the object store, and its repair.

Two directions, two failure modes, and they are not interchangeable: a row that
points at an unregistered object, and a registered object whose bytes are gone.
A check that answered only one would pass on a restore that brought the database
and not the bucket, or on the broken graph the registry was added to fix.

One fact shapes every test here. On PostgreSQL the foreign keys are real, so a
dangling reference **cannot be created** — the insert is refused. That state
arises from a restore that did not recreate the constraints, from a database
whose schema predates them, or from an adapter that does not enforce them. So
the dangling cases are built over the in-memory store, which models the rows
without the constraint, and the PostgreSQL tests assert the other half: that the
sweep sees every source, every object, and every byte.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from lab_assistant.adapters.db.engine import create_schema, make_engine
from lab_assistant.adapters.db.repositories import sql_uow_factory
from lab_assistant.adapters.db.search import PostgresSearchIndex
from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
from lab_assistant.application.objects import register_object
from lab_assistant.application.storage_inventory import (
    FindingKind,
    PayloadCheck,
    StorageInventoryService,
)
from lab_assistant.config import LabEnv, Settings
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor, publish_demo_corpus, reset_database

DATABASE_URLS = ["sqlite+aiosqlite:///:memory:"]
if os.environ.get("LAB_TEST_DATABASE_URL"):
    DATABASE_URLS.append(os.environ["LAB_TEST_DATABASE_URL"])

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)


async def _provision(harness):
    user = await harness.accounts.create_user(
        email="steward@lab.invalid", password="correct horse battery"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
    )
    admin = make_actor(user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN)
    grants = [
        await harness.accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=action,
            effect=Effect.ALLOW,
            policy_basis="sweep_test",
        )
        for action in GRANTED
    ]
    return make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )


@pytest.fixture
async def seeded():
    """A published corpus over the in-memory store, which has no constraints."""
    harness = build_harness()
    actor = await _provision(harness)
    await publish_demo_corpus(harness, actor)
    return harness, actor


@pytest.fixture(params=DATABASE_URLS, ids=lambda url: url.split(":")[0])
async def sql_seeded(request, tmp_path):
    """The same corpus over a real database and a real store on disk."""
    settings = Settings(
        env=LabEnv.LOCAL,
        database_url=request.param,
        object_store_root=tmp_path / "objects",
        llm_provider="fake",
        embedding_profile="hashing-v1",
    )
    engine = make_engine(settings)
    await create_schema(engine, dimensions=(64,))
    await reset_database(engine)
    harness = build_harness(
        uow_factory=sql_uow_factory(engine),
        object_store=FilesystemObjectStore(settings.object_store_root),
        search_index=PostgresSearchIndex(engine),
    )
    actor = await _provision(harness)
    await publish_demo_corpus(harness, actor)
    try:
        yield harness, actor
    finally:
        await engine.dispose()


class TestSweepOverARealDatabase:
    async def test_a_healthy_corpus_has_nothing_blocking(self, sql_seeded) -> None:
        harness, actor = sql_seeded
        report = await StorageInventoryService(harness.context).sweep(
            actor.workspace_id, payload_check=PayloadCheck.DIGEST
        )
        assert report.ok
        assert report.blocking == ()
        assert report.references_checked > 0
        assert report.payloads_checked == report.objects_registered

    async def test_every_schema_source_is_swept(self, sql_seeded) -> None:
        """A sweep that quietly skips a column reports health it did not check."""
        harness, actor = sql_seeded
        report = await StorageInventoryService(harness.context).sweep(actor.workspace_id)
        assert report.sources_unswept == ()
        assert "assets.object_id" in report.sources_swept
        assert "renditions.object_id" in report.sources_swept
        assert "audit_events.before_object_id" in report.sources_swept
        assert len(report.sources_swept) == 12

    async def test_missing_bytes_are_found_and_block(self, sql_seeded) -> None:
        """The restore that brought the database and not the bucket."""
        harness, actor = sql_seeded
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        assert records
        (harness.object_store.root / records[0].storage_key).unlink()

        report = await StorageInventoryService(harness.context).sweep(
            actor.workspace_id, payload_check=PayloadCheck.DIGEST
        )
        assert not report.ok
        kinds = {finding.kind for finding in report.blocking}
        assert kinds <= {FindingKind.MISSING_PAYLOAD, FindingKind.CORRUPT_PAYLOAD}

    async def test_corrupt_bytes_are_found_only_by_the_digest_check(self, sql_seeded) -> None:
        harness, actor = sql_seeded
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        path = harness.object_store.root / records[0].storage_key
        path.write_bytes(b"substituted")

        cheap = await StorageInventoryService(harness.context).sweep(
            actor.workspace_id, payload_check=PayloadCheck.EXISTS
        )
        assert cheap.ok, "an existence check cannot see substituted bytes, and says so"

        thorough = await StorageInventoryService(harness.context).sweep(
            actor.workspace_id, payload_check=PayloadCheck.DIGEST
        )
        assert not thorough.ok
        assert any(finding.kind is FindingKind.CORRUPT_PAYLOAD for finding in thorough.blocking)

    async def test_a_dangling_reference_cannot_even_be_created(self, sql_seeded) -> None:
        """The constraint is the first line of defence; the sweep is the second."""
        from sqlalchemy.exc import IntegrityError

        from lab_assistant.domain.assets import Asset

        harness, actor = sql_seeded
        with pytest.raises(IntegrityError):
            async with harness.context.uow_factory() as uow:
                await uow.assets.add_asset(
                    Asset(
                        id=uuid4(),
                        workspace_id=actor.workspace_id,
                        sha256="0" * 64,
                        byte_length=1,
                        object_id=uuid4(),
                        media_type_detected="application/xml",
                    )
                )
                await uow.commit()


class TestSweepFindsDanglingReferences:
    """Built over the store without constraints, which is the state a restore
    that dropped them leaves behind."""

    async def test_a_required_dangling_reference_blocks(self, seeded) -> None:
        harness, actor = seeded
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        asset_record = next(r for r in records if r.object_class == "asset")
        harness.db.objects.pop(asset_record.id)

        report = await StorageInventoryService(harness.context).sweep(actor.workspace_id)
        dangling = [
            finding for finding in report.findings if finding.kind is FindingKind.DANGLING_REFERENCE
        ]
        assert dangling
        assert any(finding.source == "assets.object_id" for finding in dangling)
        assert not report.ok, "assets.object_id cannot be null, so a dangling one is broken"

    async def test_an_optional_dangling_reference_is_recorded_without_blocking(
        self, seeded
    ) -> None:
        """A missing log is not a missing paper, and must not read like one."""
        harness, actor = seeded
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        rendition = next(r for r in records if r.object_class == "rendition")
        harness.db.objects.pop(rendition.id)

        report = await StorageInventoryService(harness.context).sweep(
            actor.workspace_id, payload_check=PayloadCheck.NONE
        )
        dangling = [
            finding for finding in report.findings if finding.kind is FindingKind.DANGLING_REFERENCE
        ]
        assert dangling
        assert all(not finding.required for finding in dangling)
        assert report.ok

    async def test_orphans_are_reported_and_do_not_block(self, seeded) -> None:
        harness, actor = seeded
        stored = await harness.object_store.put(
            b"uploaded by a transaction that rolled back",
            workspace_id=actor.workspace_id,
            object_class="asset",
        )
        async with harness.context.uow_factory() as uow:
            await register_object(uow, stored)
            await uow.commit()

        report = await StorageInventoryService(harness.context).sweep(actor.workspace_id)
        orphans = [
            finding for finding in report.findings if finding.kind is FindingKind.ORPHAN_OBJECT
        ]
        assert any(finding.object_id == stored.id for finding in orphans)
        assert report.ok

    async def test_orphan_detection_can_be_turned_off(self, seeded) -> None:
        """It is the one part whose memory grows with the corpus."""
        harness, actor = seeded
        report = await StorageInventoryService(harness.context).sweep(
            actor.workspace_id, report_orphans=False
        )
        assert not any(finding.kind is FindingKind.ORPHAN_OBJECT for finding in report.findings)


class TestBackfill:
    async def test_a_dry_run_changes_nothing(self, seeded) -> None:
        harness, actor = seeded
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        asset_record = next(r for r in records if r.object_class == "asset")
        harness.db.objects.pop(asset_record.id)

        report = await StorageInventoryService(harness.context).backfill(actor.workspace_id)
        assert report.dry_run is True
        assert report.dangling_found == 1
        assert report.registered == 1
        async with harness.context.uow_factory() as uow:
            assert await uow.objects.get(asset_record.id) is None

    async def test_applying_rebuilds_the_row_from_the_store(self, seeded) -> None:
        """The store kept digest, length, media type and class: no guessing."""
        harness, actor = seeded
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        asset_record = next(r for r in records if r.object_class == "asset")
        harness.db.objects.pop(asset_record.id)

        report = await StorageInventoryService(harness.context).backfill(
            actor.workspace_id, dry_run=False
        )
        assert report.registered == 1
        assert report.unrecoverable == ()

        async with harness.context.uow_factory() as uow:
            restored = await uow.objects.get(asset_record.id)
        assert restored is not None
        assert restored.sha256 == asset_record.sha256
        assert restored.byte_length == asset_record.byte_length
        assert restored.object_class == asset_record.object_class
        assert restored.storage_key == asset_record.storage_key

        after = await StorageInventoryService(harness.context).sweep(actor.workspace_id)
        assert not any(finding.kind is FindingKind.DANGLING_REFERENCE for finding in after.findings)

    async def test_what_the_store_does_not_have_is_reported_not_invented(self, seeded) -> None:
        """A placeholder row would make the next sweep look clean. It is not."""
        harness, actor = seeded
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        asset_record = next(r for r in records if r.object_class == "asset")
        harness.db.objects.pop(asset_record.id)
        harness.object_store._records.pop(asset_record.id)
        harness.object_store._objects.pop(asset_record.id)

        report = await StorageInventoryService(harness.context).backfill(
            actor.workspace_id, dry_run=False
        )
        assert report.registered == 0
        assert len(report.unrecoverable) == 1
        assert "does not have it either" in report.unrecoverable[0].detail

    async def test_nothing_to_do_is_not_an_error(self, seeded) -> None:
        harness, actor = seeded
        report = await StorageInventoryService(harness.context).backfill(
            actor.workspace_id, dry_run=False
        )
        assert report.dangling_found == 0
        assert report.registered == 0
        assert report.unrecoverable == ()


class TestPaging:
    async def test_a_page_size_is_not_a_corpus_limit(self, seeded) -> None:
        """Seven objects, three to a page: a sweep that stops at one page lies."""
        harness, actor = seeded
        async with harness.context.uow_factory() as uow:
            before = await uow.objects.count_objects(actor.workspace_id)
        for index in range(7):
            stored = await harness.object_store.put(
                f"extra payload {index}".encode(),
                workspace_id=actor.workspace_id,
                object_class="asset",
            )
            async with harness.context.uow_factory() as uow:
                await register_object(uow, stored)
                await uow.commit()

        report = await StorageInventoryService(harness.context).sweep(
            actor.workspace_id, payload_check=PayloadCheck.DIGEST, page_size=3
        )
        assert report.objects_registered == before + 7
        assert report.payloads_checked == before + 7
