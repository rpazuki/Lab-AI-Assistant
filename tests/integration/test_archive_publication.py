"""Moving the head exactly once, and surviving a crash on either side of it.

Three of phase 2's four acceptance criteria are here:

- faults before the head commit are invisible;
- a fault after the head commit is reconciled without duplicate publication;
- no publisher advances the head from a stale epoch.

The crash tests do not mock a crash. They stop calling — run the phases a real
publication runs, abandon the service mid-protocol, and then hand a *fresh*
service the same outbox and archive, which is exactly what a restarted process
has. A test that patched in an exception would be testing the patch.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from lab_assistant.adapters.object_store.control import (
    FilesystemControlStore,
    MemoryControlStore,
)
from lab_assistant.application.archive_catalog import ArchiveCatalogBuilder
from lab_assistant.application.archive_publish import PublicationService, library_prefix
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.contracts.publication import HEAD_LOCATION, PublicationState
from lab_assistant.domain.publication import CommitRefusal, ReconcileAction
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import publish_archive_fixture, replicating_rights_policy
from lab_assistant.testing import build_harness, make_actor

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)


@pytest.fixture
async def library(tmp_path):
    """A packaged snapshot, an empty archive, and a publisher that owns it."""
    harness = build_harness(rights_policy=replicating_rights_policy())
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
            policy_basis="publication_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    await publish_archive_fixture(harness, actor)

    library_id = uuid4()
    installation_id = uuid4()
    builder = ArchiveCatalogBuilder(
        harness.context, library_id=library_id, installation_id=installation_id
    )
    build = await builder.build(workspace.id, tmp_path / "snapshot")
    assert build.ok, build.problems

    control = FilesystemControlStore(tmp_path / "archive")

    def service(*, epoch: int = 1, installation=None):
        return PublicationService(
            harness.context,
            control=control,
            library_id=library_id,
            installation_id=installation or installation_id,
            writer_epoch=epoch,
        )

    return {
        "harness": harness,
        "workspace": workspace,
        "directory": tmp_path / "snapshot",
        "archive": tmp_path / "archive",
        "control": control,
        "library_id": library_id,
        "installation_id": installation_id,
        "service": service,
        "builder": builder,
        "snapshot_id": build.snapshot_id,
    }


def _head_path(library) -> Path:
    return library["archive"] / library_prefix(library["library_id"]) / HEAD_LOCATION


class TestPublishing:
    async def test_it_publishes_and_the_head_points_at_the_snapshot(self, library) -> None:
        report = await library["service"]().publish(library["workspace"].id, library["directory"])
        assert report.ok, report.problems
        assert report.published
        assert report.state is PublicationState.ACKNOWLEDGED

        head = json.loads(_head_path(library).read_text())
        assert head["snapshot_id"] == str(library["snapshot_id"])
        assert head["commit_sequence"] == 1

    async def test_a_dry_run_moves_nothing(self, library) -> None:
        report = await library["service"]().publish(
            library["workspace"].id, library["directory"], dry_run=True
        )
        assert report.ok
        assert not report.published
        assert not _head_path(library).exists()

    async def test_an_invalid_snapshot_never_reaches_the_archive(self, library) -> None:
        """Fixing a bad snapshot is cheap locally and expensive once uploaded."""
        shard = library["directory"] / "catalog" / "work.jsonl"
        shard.write_bytes(shard.read_bytes() + b'{"broken": true}\n')

        report = await library["service"]().publish(library["workspace"].id, library["directory"])
        assert not report.ok
        assert any("does not validate" in problem for problem in report.problems)
        assert not _head_path(library).exists()

    async def test_republishing_the_same_snapshot_is_refused(self, library) -> None:
        """Twice published is two sequences for one piece of work."""
        service = library["service"]()
        assert (await service.publish(library["workspace"].id, library["directory"])).published

        again = await service.publish(library["workspace"].id, library["directory"])
        assert not again.ok
        assert again.verdict.refusal is CommitRefusal.ALREADY_PUBLISHED

    async def test_a_second_snapshot_advances_the_sequence(self, library, tmp_path) -> None:
        service = library["service"]()
        await service.publish(library["workspace"].id, library["directory"])

        second = await library["builder"].build(library["workspace"].id, tmp_path / "second")
        assert second.ok
        report = await service.publish(library["workspace"].id, tmp_path / "second")
        assert report.published
        assert report.head.commit_sequence == 2


class TestEpochFencing:
    """No publisher advances the head from a stale epoch."""

    async def test_a_fenced_publisher_is_refused(self, library, tmp_path) -> None:
        await library["service"](epoch=2).publish(library["workspace"].id, library["directory"])

        second = await library["builder"].build(library["workspace"].id, tmp_path / "second")
        assert second.ok
        stale = await library["service"](epoch=1).publish(
            library["workspace"].id, tmp_path / "second"
        )

        assert not stale.ok
        assert stale.verdict.refusal is CommitRefusal.FENCED_BY_EPOCH
        assert not stale.published

    async def test_the_refusal_tells_the_operator_not_to_adopt_the_new_epoch(
        self, library, tmp_path
    ) -> None:
        """The trap the architecture proposal names: re-reading the head, seeing
        a higher epoch and retrying with it is a fenced writer taking the
        library back, and it would succeed."""
        await library["service"](epoch=3).publish(library["workspace"].id, library["directory"])
        second = await library["builder"].build(library["workspace"].id, tmp_path / "second")
        assert second.ok

        stale = await library["service"](epoch=1).publish(
            library["workspace"].id, tmp_path / "second"
        )
        assert any("do NOT re-run" in step for step in stale.next_steps)

    async def test_a_foreign_owner_at_the_same_epoch_is_refused(self, library, tmp_path) -> None:
        """Two installations claiming one library is a configuration fault, and
        picking one would make it permanent."""
        await library["service"]().publish(library["workspace"].id, library["directory"])
        second = await library["builder"].build(library["workspace"].id, tmp_path / "second")
        assert second.ok

        other = await library["service"](installation=uuid4()).publish(
            library["workspace"].id, tmp_path / "second"
        )
        assert not other.ok
        assert other.verdict.refusal is CommitRefusal.FOREIGN_OWNER

    async def test_an_epoch_ahead_of_the_head_is_refused(self, library, tmp_path) -> None:
        """It may be a legitimate handover and this process cannot establish
        that, so it refuses rather than seizing the library."""
        await library["service"](epoch=1).publish(library["workspace"].id, library["directory"])
        second = await library["builder"].build(library["workspace"].id, tmp_path / "second")
        assert second.ok

        ahead = await library["service"](epoch=5).publish(
            library["workspace"].id, tmp_path / "second"
        )
        assert not ahead.ok
        assert ahead.verdict.refusal is CommitRefusal.EPOCH_AHEAD_OF_HEAD


class TestCrashBeforeTheCommit:
    """Faults before the head commit are invisible."""

    async def test_an_interrupted_upload_publishes_nothing(self, library) -> None:
        """The publisher dies after uploading and before the swap. A reader
        resolves the head and never sees the snapshot: litter, not a
        publication."""
        service = library["service"]()
        manifest, problems = service._load(library["directory"])
        assert manifest is not None, problems
        head, version = await service.read_head()
        from lab_assistant.domain.publication import evaluate_commit

        verdict = evaluate_commit(
            head,
            library_id=library["library_id"],
            owner_installation_id=library["installation_id"],
            writer_epoch=1,
            snapshot_id=manifest.snapshot_id,
        )
        record = await service._prepare(library["workspace"].id, manifest, verdict, version)
        await service._upload(record, library["directory"], manifest)
        # ...and the process dies here.

        assert not _head_path(library).exists(), "an interrupted upload published something"

        fresh = library["service"]()
        verdicts = await fresh.reconcile()
        assert [v.action for v in verdicts] == [ReconcileAction.RESUME]

    async def test_the_interrupted_attempt_can_be_resumed(self, library) -> None:
        service = library["service"]()
        manifest, _ = service._load(library["directory"])
        head, version = await service.read_head()
        from lab_assistant.domain.publication import evaluate_commit

        verdict = evaluate_commit(
            head,
            library_id=library["library_id"],
            owner_installation_id=library["installation_id"],
            writer_epoch=1,
            snapshot_id=manifest.snapshot_id,
        )
        record = await service._prepare(library["workspace"].id, manifest, verdict, version)
        await service._upload(record, library["directory"], manifest)

        # A restarted publisher completes it. The upload is create-only, so
        # re-uploading the same bytes is a no-op rather than a conflict.
        report = await library["service"]().publish(library["workspace"].id, library["directory"])
        assert report.published
        assert report.uploaded == 0, "a resumed upload rewrote objects that were already there"


class TestCrashAfterTheCommit:
    """A fault after the head commit is reconciled without duplicate publication."""

    async def test_a_committed_snapshot_is_acknowledged_not_republished(self, library) -> None:
        """The head moved and the publisher died before writing that down. The
        outbox exists for exactly this: republishing would claim a second
        sequence for one piece of work.
        """
        service = library["service"]()
        manifest, _ = service._load(library["directory"])
        head, version = await service.read_head()
        from lab_assistant.domain.publication import evaluate_commit

        verdict = evaluate_commit(
            head,
            library_id=library["library_id"],
            owner_installation_id=library["installation_id"],
            writer_epoch=1,
            snapshot_id=manifest.snapshot_id,
        )
        record = await service._prepare(library["workspace"].id, manifest, verdict, version)
        record, _, _ = await service._upload(record, library["directory"], manifest)

        # The swap lands, and the process dies before the outbox catches up.
        descriptor_before = (
            json.loads(_head_path(library).read_text()) if _head_path(library).exists() else None
        )
        assert descriptor_before is None
        await library["control"].create(
            f"{library_prefix(library['library_id'])}{HEAD_LOCATION}",
            _head_bytes(library, record, manifest),
        )

        fresh = library["service"]()
        verdicts = await fresh.reconcile(apply=True)
        assert [v.action for v in verdicts] == [ReconcileAction.ACKNOWLEDGE]

        head_now = json.loads(_head_path(library).read_text())
        assert head_now["commit_sequence"] == 1, "the head was advanced twice"

        async with library["harness"].context.uow_factory() as uow:
            stored = await uow.publications.get(library["workspace"].id, record.id)
        assert stored.state is PublicationState.ACKNOWLEDGED

    async def test_an_attempt_overtaken_by_another_is_abandoned(self, library, tmp_path) -> None:
        """Interrupted, and somebody else published in the meantime. The head
        is the authority, so the stale attempt is abandoned rather than
        replayed over the top."""
        service = library["service"]()
        manifest, _ = service._load(library["directory"])
        head, version = await service.read_head()
        from lab_assistant.domain.publication import evaluate_commit

        verdict = evaluate_commit(
            head,
            library_id=library["library_id"],
            owner_installation_id=library["installation_id"],
            writer_epoch=1,
            snapshot_id=manifest.snapshot_id,
        )
        record = await service._prepare(library["workspace"].id, manifest, verdict, version)
        await service._upload(record, library["directory"], manifest)

        # Another snapshot takes sequence 1 while this attempt is interrupted.
        second = await library["builder"].build(library["workspace"].id, tmp_path / "second")
        assert second.ok
        await library["service"]().publish(library["workspace"].id, tmp_path / "second")

        verdicts = await library["service"]().reconcile(apply=True)
        actions = {v.action for v in verdicts}
        assert ReconcileAction.ABANDON in actions


class TestTheSwapIsNotRetried:
    async def test_a_head_that_moved_underneath_fails_the_swap(self, library) -> None:
        """And the attempt is recorded as failed rather than forced through."""
        service = library["service"]()
        manifest, _ = service._load(library["directory"])
        head, version = await service.read_head()
        from lab_assistant.domain.publication import evaluate_commit

        verdict = evaluate_commit(
            head,
            library_id=library["library_id"],
            owner_installation_id=library["installation_id"],
            writer_epoch=1,
            snapshot_id=manifest.snapshot_id,
        )
        record = await service._prepare(library["workspace"].id, manifest, verdict, version)
        record, _, _ = await service._upload(record, library["directory"], manifest)

        # Somebody creates the head between the read and the swap.
        await library["control"].create(
            f"{library_prefix(library['library_id'])}{HEAD_LOCATION}",
            _head_bytes(library, record, manifest, snapshot_id=uuid4()),
        )

        with pytest.raises(LabAssistantError) as raised:
            await service._commit(record, manifest, None, None)
        assert raised.value.error_class is ErrorClass.CONFLICT

        async with library["harness"].context.uow_factory() as uow:
            stored = await uow.publications.get(library["workspace"].id, record.id)
        assert stored.state is PublicationState.FAILED


class TestTheControlStoreSemantics:
    async def test_create_refuses_an_existing_key(self) -> None:
        store = MemoryControlStore()
        await store.create("control/head.json", b"{}")
        with pytest.raises(LabAssistantError) as raised:
            await store.create("control/head.json", b"{}")
        assert raised.value.error_class is ErrorClass.CONFLICT

    async def test_a_stale_swap_is_refused(self, tmp_path) -> None:
        store = FilesystemControlStore(tmp_path / "control")
        first = await store.create("head.json", b'{"seq":1}')
        await store.compare_and_swap("head.json", b'{"seq":2}', expected_version=first)
        with pytest.raises(LabAssistantError) as raised:
            await store.compare_and_swap("head.json", b'{"seq":3}', expected_version=first)
        assert raised.value.error_class is ErrorClass.CONFLICT


def _head_bytes(library, record, manifest, *, snapshot_id=None) -> bytes:
    from lab_assistant.domain.publication import next_head

    return next_head(
        None,
        library_id=library["library_id"],
        owner_installation_id=library["installation_id"],
        writer_epoch=1,
        snapshot_id=snapshot_id or record.snapshot_id,
        snapshot_manifest_digest=record.snapshot_manifest_digest,
        commit_sequence=1,
    ).serialise()
