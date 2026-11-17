"""Moving the object store, and the ways that goes wrong.

Changing `LAB_OBJECT_STORE_KIND` points the application at an empty bucket. The
evidence stays where it was, every citation stops resolving, and the next write
goes somewhere with no history. These tests are about the alternative: a copy
that preserves identity, carries creation time and tombstones, verifies each
object, reports what it could not move, and resumes when it is interrupted.

They run filesystem-to-filesystem. That is not a weaker case than
filesystem-to-S3: the service talks to both ends through the same port, and the
provider-specific half — conditional writes, multipart, error codes — is covered
by the S3 adapter's own stubbed tests. What is specific to migration is the
bookkeeping, and the bookkeeping is what these exercise.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from lab_assistant.adapters.object_store.filesystem import (
    FilesystemObjectStore,
    InMemoryObjectStore,
)
from lab_assistant.application.objects import register_object
from lab_assistant.application.storage_migration import (
    MigrationJournal,
    StorageMigrationService,
    VerifyLevel,
)
from lab_assistant.contracts.errors import LabAssistantError
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor, publish_demo_corpus

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)


@pytest.fixture
async def source(tmp_path):
    """A populated installation on a filesystem store."""
    harness = build_harness(object_store=FilesystemObjectStore(tmp_path / "source"))
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
            policy_basis="migration_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    await publish_demo_corpus(harness, actor)
    return harness, actor


@pytest.fixture
def destination(tmp_path):
    return FilesystemObjectStore(tmp_path / "destination")


def _service(source_fixture, destination):
    harness, _actor = source_fixture
    return StorageMigrationService(harness.context, destination=destination)


class TestPlanning:
    async def test_the_plan_counts_both_sides_without_copying(self, source, destination) -> None:
        harness, actor = source
        plan = await _service(source, destination).plan()
        async with harness.context.uow_factory() as uow:
            registered = await uow.objects.count_objects(actor.workspace_id)
        assert plan.objects == registered
        assert plan.bytes_total > 0
        assert plan.already_at_destination == 0
        assert plan.bytes_to_copy == plan.bytes_total
        assert plan.missing_at_source == ()
        # Nothing was written.
        assert not (destination.root / "index.jsonl").exists()

    async def test_a_payload_the_database_does_not_know_is_reported(
        self, source, destination
    ) -> None:
        """Usually a transaction that rolled back after its upload."""
        harness, actor = source
        orphan = await harness.object_store.put(
            b"uploaded, never committed",
            workspace_id=actor.workspace_id,
            object_class="asset",
        )
        plan = await _service(source, destination).plan()
        assert plan.source_enumerable is True
        assert orphan.id in plan.source_only

    async def test_an_object_the_source_has_no_record_of_is_named(
        self, source, destination
    ) -> None:
        """Distinct from bytes gone from under a record the store still has:
        that one is found when the copy tries to read them, because the plan's
        probe asks the index and asking the disk would read the corpus twice."""
        harness, actor = source
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        index = harness.object_store.root / "index.jsonl"
        kept = [
            line
            for line in index.read_text(encoding="utf-8").splitlines()
            if str(records[0].id) not in line
        ]
        index.write_text("\n".join(kept) + "\n", encoding="utf-8")

        plan = await StorageMigrationService(
            harness.context,
            destination=destination,
            source=FilesystemObjectStore(harness.object_store.root),
        ).plan()
        assert records[0].id in plan.missing_at_source

    async def test_a_store_that_cannot_enumerate_itself_says_so(self, source) -> None:
        """A bucket answers with LIST, which a narrow reader may not hold."""

        class _Opaque(InMemoryObjectStore):
            inventory = None  # type: ignore[assignment]

        plan = await _service(source, _Opaque()).plan()
        assert plan.objects > 0
        assert plan.source_enumerable is True  # the *source* can; the destination need not


class TestCopying:
    async def test_every_object_arrives_with_its_identity_intact(self, source, destination) -> None:
        harness, _actor = source
        report = await _service(source, destination).migrate(dry_run=False)
        assert report.ok
        assert report.copied == report.plan.objects
        assert report.verified == report.copied

        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_all_objects(limit=500)
        for record in records:
            arrived = await destination.head(record.id)
            assert arrived is not None, f"{record.object_class} did not arrive"
            assert arrived.sha256 == record.sha256
            assert arrived.byte_length == record.byte_length
            assert arrived.media_type == record.media_type
            assert arrived.object_class == record.object_class
            assert arrived.key == record.storage_key

    async def test_creation_time_travels_with_the_object(self, source, destination) -> None:
        """Evidence restamped with the hour it was moved has lost something."""
        harness, _actor = source
        await _service(source, destination).migrate(dry_run=False)
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_all_objects(limit=500)
        for record in records:
            arrived = await destination.head(record.id)
            assert arrived is not None
            assert arrived.created_at == record.created_at

    async def test_tombstoned_bytes_stay_behind_and_are_named(self, source, destination) -> None:
        """The denial lives in the database, which a migration does not move.

        So the destination does not need the bytes to keep denying them, and
        copying withdrawn material into a new store would be the wrong default.
        They stay in the old store — which is retained read-only anyway — and
        the report names them, because "the old bucket can go now" has to be a
        statement somebody can check against something.
        """
        harness, actor = source
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
            await uow.objects.tombstone(records[0].id, reason="rights withdrawn")
            await uow.commit()
        await harness.object_store.tombstone(records[0].id, reason="rights withdrawn")

        report = await _service(source, destination).migrate(dry_run=False)
        assert report.ok
        assert records[0].id in report.plan.tombstoned_not_copied
        assert await destination.head(records[0].id) is None

        # And the denial is still a denial, because SQL still says so.
        async with harness.context.uow_factory() as uow:
            still = await uow.objects.get(records[0].id)
        assert still is not None and still.tombstoned

    async def test_a_dry_run_writes_nothing(self, source, destination) -> None:
        report = await _service(source, destination).migrate(dry_run=True)
        assert report.dry_run
        assert report.copied > 0, "it still says what it would do"
        assert not (destination.root / "index.jsonl").exists()

    async def test_running_twice_copies_nothing_the_second_time(self, source, destination) -> None:
        service = _service(source, destination)
        first = await service.migrate(dry_run=False)
        second = await service.migrate(dry_run=False)
        assert second.copied == 0
        assert second.skipped_present == first.copied
        assert second.ok

    async def test_the_source_and_destination_may_not_be_the_same_store(self, source) -> None:
        harness, _actor = source
        with pytest.raises(LabAssistantError):
            StorageMigrationService(harness.context, destination=harness.object_store)


class TestVerification:
    async def test_a_damaged_destination_is_caught(self, source, destination) -> None:
        """What a truncated transfer looks like, and why digest is the default."""
        harness, _actor = source
        service = _service(source, destination)
        await service.migrate(dry_run=False)

        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_all_objects(limit=10)
        (destination.root / records[0].storage_key).write_bytes(b"truncated")

        # A fresh destination store, so the damage is read rather than remembered.
        reopened = FilesystemObjectStore(destination.root)
        again = StorageMigrationService(harness.context, destination=reopened)
        report = await again.migrate(dry_run=False)
        assert not report.ok
        assert any(problem.kind == "destination_disagrees" for problem in report.problems) or any(
            problem.kind == "verification_failed" for problem in report.problems
        )

    async def test_metadata_verification_is_cheaper_and_blinder(self, source, destination) -> None:
        report = await _service(source, destination).migrate(
            dry_run=False, verify=VerifyLevel.METADATA
        )
        assert report.ok
        assert report.verified == report.copied

    async def test_an_object_whose_bytes_are_gone_is_a_problem_not_a_crash(
        self, source, destination
    ) -> None:
        harness, actor = source
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=50)
        (harness.object_store.root / records[0].storage_key).unlink()

        report = await _service(source, destination).migrate(dry_run=False)
        assert not report.ok
        assert any(problem.kind == "missing_at_source" for problem in report.problems)
        # The rest still moved.
        assert report.copied >= report.plan.objects - 1


class TestResume:
    async def test_an_interrupted_run_picks_up_where_it_stopped(
        self, source, destination, tmp_path
    ) -> None:
        harness, _actor = source
        journal = MigrationJournal(tmp_path / "journal.jsonl")

        # Simulate a run that got through two objects before dying.
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_all_objects(limit=500)
        for record in records[:2]:
            journal.record("copied", object_id=str(record.id), bytes=record.byte_length)

        report = await _service(source, destination).migrate(dry_run=False, journal=journal)
        assert report.resumed_from == 2
        assert report.copied == len(records) - 2
        assert report.ok

    async def test_the_journal_survives_a_torn_final_line(
        self, source, destination, tmp_path
    ) -> None:
        """A crash mid-write is the normal shape of the thing it protects against."""
        path = tmp_path / "journal.jsonl"
        journal = MigrationJournal(path)
        harness, _actor = source
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_all_objects(limit=500)
        journal.record("copied", object_id=str(records[0].id), bytes=1)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "copied", "object_i')

        assert journal.completed() == {records[0].id}

    async def test_the_journal_holds_no_secrets(self, source, destination, tmp_path) -> None:
        journal = MigrationJournal(tmp_path / "journal.jsonl")
        await _service(source, destination).migrate(dry_run=False, journal=journal)
        text = journal.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            row = json.loads(line)
            assert set(row) <= {
                "event",
                "object_id",
                "bytes",
                "run_id",
                "at",
                "objects",
                "bytes_total",
                "copied",
                "problems",
                "detail",
            }

    async def test_a_resumed_run_does_not_recopy_what_is_there(
        self, source, destination, tmp_path
    ) -> None:
        journal = MigrationJournal(tmp_path / "journal.jsonl")
        service = _service(source, destination)
        await service.migrate(dry_run=False, journal=journal)
        second = await service.migrate(dry_run=False, journal=journal)
        assert second.copied == 0
        assert second.resumed_from > 0


class TestSourceOnlyPayloads:
    async def test_they_are_carried_by_default(self, source, destination) -> None:
        harness, actor = source
        orphan = await harness.object_store.put(
            b"uploaded, never committed",
            workspace_id=actor.workspace_id,
            object_class="asset",
        )
        report = await _service(source, destination).migrate(dry_run=False)
        assert await destination.head(orphan.id) is not None
        assert report.ok

    async def test_leaving_them_is_a_decision_rather_than_an_omission(
        self, source, destination
    ) -> None:
        harness, actor = source
        orphan = await harness.object_store.put(
            b"uploaded, never committed",
            workspace_id=actor.workspace_id,
            object_class="asset",
        )
        await _service(source, destination).migrate(dry_run=False, include_source_only=False)
        assert await destination.head(orphan.id) is None

    async def test_a_shared_object_with_no_workspace_still_moves(self, source, destination) -> None:
        """A per-workspace sweep would leave these behind every time."""
        harness, _actor = source
        shared = await harness.object_store.put(
            b"a definition every workspace reads", workspace_id=None, object_class="config"
        )
        async with harness.context.uow_factory() as uow:
            await register_object(uow, shared)
            await uow.commit()

        await _service(source, destination).migrate(dry_run=False)
        arrived = await destination.head(shared.id)
        assert arrived is not None
        assert arrived.workspace_id is None
        assert arrived.id != uuid4()
