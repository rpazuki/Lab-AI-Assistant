"""Taking a private backup, checking one, and putting one back.

The predecessor's scripts could be run and could not be questioned: they copied
files into volumes they named by hand, and every compatibility question they
needed to ask, they assumed. So these tests are mostly about the questions —
what a restore establishes *before* it touches the destination, and what it
refuses.

The database dump is faked here, deliberately. `pg_dump` is an external program;
what it produces is PostgreSQL's problem and is exercised against a real server
in `TestAgainstRealPostgres` below. What is specific to *this* code is the
bookkeeping — the manifest, the digests, the compatibility verdict, the
one-writer rule — and the bookkeeping is what these exercise. The fake writes a
real file and the restore reads it back, so a service that lost track of which
file it wrote fails here rather than during a drill.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from lab_assistant.adapters.db.migrations import migration_order
from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
from lab_assistant.application.backup import (
    MANIFEST_DIGEST_NAME,
    MANIFEST_NAME,
    BackupService,
    installation_identity,
    secret_references,
)
from lab_assistant.application.ports import DatabaseFingerprint, DumpResult
from lab_assistant.config import Settings
from lab_assistant.contracts.backup import BackupManifest, ComponentKind
from lab_assistant.contracts.errors import LabAssistantError
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.recovery import RestoreIntent, evaluate_restore
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor, publish_demo_corpus

ROOT = Path(__file__).resolve().parents[2]

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)

#: What the fake dump writes. Arbitrary, and the point is that it is *stable*:
#: the restore recomputes it, so a service that handed the restore a different
#: path than the one it dumped fails rather than passing quietly.
DUMP_PAYLOAD = b"PGDMP\x00fake custom-format dump for the backup bookkeeping tests\n"


class FakeDatabaseSnapshot:
    """A `DatabaseSnapshot` that writes a real file and refuses a wrong one.

    It does not accept a restore of bytes it did not write. A permissive fake
    here would let the service pass a path it had never dumped to — which is
    exactly the defect the component digests exist to catch — and the suite
    would prove nothing about the one thing it is testing.
    """

    def __init__(
        self,
        *,
        server_version: str = "16.13",
        extensions: dict[str, str] | None = None,
        schema_revision: str | None = "0001_initial",
        payload: bytes = DUMP_PAYLOAD,
        relation_count: int = 0,
        on_restore: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._fingerprint = DatabaseFingerprint(
            server_version=server_version,
            extensions=dict(extensions if extensions is not None else {"vector": "0.8.2"}),
            schema_revision=schema_revision,
            relation_count=relation_count,
        )
        self._payload = payload
        #: What replaying the dump brings back. A real `pg_restore` repopulates
        #: the object registry, and the restore's object copy is driven by that
        #: registry — so a fake that did not model it would let an incorrectly
        #: ordered restore pass here and fail against a real database. It did,
        #: once.
        self._on_restore = on_restore
        self.dumped: Path | None = None
        self.restored: Path | None = None
        self.restored_clean: bool | None = None

    async def fingerprint(self) -> DatabaseFingerprint:
        return self._fingerprint

    async def dump(self, destination: Path) -> DumpResult:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._payload)
        self.dumped = destination
        return DumpResult(
            path=str(destination),
            byte_length=len(self._payload),
            sha256=hashlib.sha256(self._payload).hexdigest(),
            tool_version="pg_dump (PostgreSQL) 16.13",
        )

    async def restore(self, source: Path, *, clean: bool = False) -> None:
        source = Path(source)
        if not source.is_file():
            raise AssertionError(f"restore was given {source}, which does not exist")
        if source.read_bytes() != self._payload:
            raise AssertionError(
                f"restore was given {source}, whose bytes are not the dump that was taken"
            )
        self.restored = source
        self.restored_clean = clean
        if self._on_restore is not None:
            await self._on_restore()


@pytest.fixture
async def installation(tmp_path):
    """A populated installation on a filesystem store, as a backup finds it."""
    harness = build_harness(
        object_store=FilesystemObjectStore(tmp_path / "live-objects"),
        settings_overrides={"object_store_root": tmp_path / "live-objects"},
    )
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
            policy_basis="backup_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    await publish_demo_corpus(harness, actor)
    return harness, actor


def _service(harness, snapshot: FakeDatabaseSnapshot, **overrides) -> BackupService:
    installation_id, source = installation_identity(harness.context.settings)
    return BackupService(
        harness.context,
        snapshot=snapshot,
        installation_id=overrides.pop("installation_id", installation_id),
        store_factory=FilesystemObjectStore,
        identity_source=overrides.pop("identity_source", source),
        **overrides,
    )


class TestCreate:
    async def test_a_backup_holds_the_control_plane_and_every_object(
        self, installation, tmp_path
    ) -> None:
        harness, actor = installation
        snapshot = FakeDatabaseSnapshot()
        report = await _service(harness, snapshot).create(tmp_path / "backup")

        assert report.ok
        manifest = report.manifest
        assert manifest is not None
        assert manifest.complete, manifest.incomplete

        dump = manifest.component(ComponentKind.DATABASE_DUMP)
        payloads = manifest.component(ComponentKind.OBJECT_PAYLOADS)
        assert dump is not None and payloads is not None
        assert (tmp_path / "backup" / dump.path).read_bytes() == DUMP_PAYLOAD

        async with harness.context.uow_factory() as uow:
            registered = await uow.objects.count_objects(actor.workspace_id)
        assert payloads.item_count == registered
        assert registered > 0

    async def test_the_manifest_records_counts_a_drill_can_read_out_loud(
        self, installation, tmp_path
    ) -> None:
        harness, actor = installation
        report = await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")
        manifest = report.manifest
        assert manifest is not None

        inventory = {row.slug: row for row in manifest.workspaces}
        assert "rla-lab" in inventory
        async with harness.context.uow_factory() as uow:
            objects = await uow.objects.count_objects(actor.workspace_id)
            releases = await uow.releases.list_releases(actor.workspace_id)
        assert inventory["rla-lab"].objects == objects
        assert inventory["rla-lab"].releases == len(releases)
        assert inventory["rla-lab"].object_bytes > 0

    async def test_the_payloads_are_the_same_bytes(self, installation, tmp_path) -> None:
        """Identity is preserved by construction: same workspace, same digest,
        same object id, so the backup needs no mapping table to get wrong."""
        harness, actor = installation
        await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")

        backup_store = FilesystemObjectStore(tmp_path / "backup" / "objects")
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=500)
        assert records
        for record in records:
            if record.tombstoned:
                continue
            assert await backup_store.get(record.id) == await harness.object_store.get(record.id)

    async def test_the_backup_carries_no_secret_value(self, installation, tmp_path) -> None:
        """The checklist travels; the values do not.

        An archive containing the database password makes every off-site copy a
        credential leak, so this greps the whole backup for the values this
        installation actually holds rather than trusting the manifest's shape.
        """
        harness, _actor = installation
        settings = harness.context.settings
        await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")

        secrets = [settings.secret_key, settings.database_url]
        blob = b""
        for path in (tmp_path / "backup").rglob("*"):
            if path.is_file():
                blob += path.read_bytes()
        for secret in secrets:
            assert secret.encode("utf-8") not in blob, "a secret value reached the backup"

        names = {reference.name for reference in secret_references(settings)}
        assert "LAB_SECRET_KEY" in names
        assert "LAB_DATABASE_URL" in names

    async def test_the_checklist_follows_the_configuration(self, installation) -> None:
        """An installation on a filesystem store must not be sent looking for S3
        credentials, and one with the external model enabled must be. A checklist
        that is wrong in either direction stops being read."""
        filesystem = secret_references(Settings(object_store_kind="filesystem"))
        assert not any("OBJECT_STORE_ACCESS_KEY" in ref.name for ref in filesystem)

        bucket = secret_references(
            Settings(object_store_kind="s3", external_model_enabled=True)
        )
        assert any("OBJECT_STORE_ACCESS_KEY" in ref.name for ref in bucket)
        assert any(ref.name == "LAB_LLM_API_KEY" for ref in bucket)

    async def test_a_dry_run_writes_nothing(self, installation, tmp_path) -> None:
        harness, _actor = installation
        snapshot = FakeDatabaseSnapshot()
        report = await _service(harness, snapshot).create(tmp_path / "backup", dry_run=True)
        assert report.dry_run
        assert snapshot.dumped is None
        assert not (tmp_path / "backup").exists()
        # It still fingerprints and inventories, which is the point of running it.
        assert report.manifest is not None
        assert report.manifest.workspaces

    async def test_it_refuses_to_write_into_an_existing_backup(
        self, installation, tmp_path
    ) -> None:
        """Merging two runs produces a manifest that describes neither, and the
        mixture verifies as long as nobody looks closely."""
        harness, _actor = installation
        await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")
        with pytest.raises(LabAssistantError) as excinfo:
            await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")
        assert "already holds a backup" in str(excinfo.value)

    async def test_omitting_the_objects_marks_the_backup_incomplete(
        self, installation, tmp_path
    ) -> None:
        """A control-plane dump alone restores a database full of citations into
        a store that has none of the bytes."""
        harness, _actor = installation
        report = await _service(harness, FakeDatabaseSnapshot()).create(
            tmp_path / "backup", include_objects=False
        )
        assert report.manifest is not None
        assert not report.manifest.complete
        assert any("object payloads" in item for item in report.manifest.incomplete)


class TestVerify:
    async def test_a_good_backup_verifies(self, installation, tmp_path) -> None:
        harness, _actor = installation
        service = _service(harness, FakeDatabaseSnapshot())
        await service.create(tmp_path / "backup")
        verification = await service.verify(tmp_path / "backup")
        assert verification.ok, verification.problems
        assert verification.components_checked == 3
        assert verification.objects_checked > 0

    async def test_a_truncated_dump_is_caught(self, installation, tmp_path) -> None:
        """The failure a backup is most likely to be hiding: a plausible file
        that the disk filled up halfway through."""
        harness, _actor = installation
        service = _service(harness, FakeDatabaseSnapshot())
        report = await service.create(tmp_path / "backup")
        assert report.manifest is not None
        dump = report.manifest.component(ComponentKind.DATABASE_DUMP)
        assert dump is not None
        (tmp_path / "backup" / dump.path).write_bytes(DUMP_PAYLOAD[:10])

        verification = await service.verify(tmp_path / "backup")
        assert not verification.ok
        assert any("bytes" in problem for problem in verification.problems)

    async def test_an_edited_manifest_is_caught(self, installation, tmp_path) -> None:
        harness, _actor = installation
        service = _service(harness, FakeDatabaseSnapshot())
        await service.create(tmp_path / "backup")

        path = tmp_path / "backup" / MANIFEST_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["workspaces"][0]["objects"] = 99_999
        path.write_text(json.dumps(payload), encoding="utf-8")

        verification = await service.verify(tmp_path / "backup")
        assert not verification.ok
        assert any("edited after it was written" in problem for problem in verification.problems)

    async def test_a_missing_digest_file_is_a_finding(self, installation, tmp_path) -> None:
        """The digest file is how a verify tells an edited manifest from an
        intact one. Its absence is not the same as agreement."""
        harness, _actor = installation
        service = _service(harness, FakeDatabaseSnapshot())
        await service.create(tmp_path / "backup")
        (tmp_path / "backup" / MANIFEST_DIGEST_NAME).unlink()

        verification = await service.verify(tmp_path / "backup")
        assert not verification.ok
        assert any(MANIFEST_DIGEST_NAME in problem for problem in verification.problems)

    async def test_a_rotted_payload_is_caught(self, installation, tmp_path) -> None:
        """Bit rot in off-site storage. Only a re-digest finds it, which is why
        verifying objects is the expensive half and is on by default."""
        harness, actor = installation
        service = _service(harness, FakeDatabaseSnapshot())
        await service.create(tmp_path / "backup")

        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=5)
        store = FilesystemObjectStore(tmp_path / "backup" / "objects")
        stored = await store.head(records[0].id)
        assert stored is not None
        target = (tmp_path / "backup" / "objects" / stored.key)
        target.write_bytes(b"not the bytes that were stored here")

        verification = await service.verify(tmp_path / "backup")
        assert not verification.ok
        assert any("does not match its digest" in problem for problem in verification.problems)

    async def test_a_manifest_that_does_not_parse_is_a_finding(self, tmp_path) -> None:
        """Not a traceback. The same rule `verify-restore` learned when one bad
        release hid every finding after it."""
        harness = build_harness()
        (tmp_path / "backup").mkdir()
        (tmp_path / "backup" / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        verification = await _service(harness, FakeDatabaseSnapshot()).verify(tmp_path / "backup")
        assert not verification.ok
        assert any("does not parse" in problem for problem in verification.problems)

    async def test_a_directory_that_is_not_a_backup_says_so(self, tmp_path) -> None:
        harness = build_harness()
        (tmp_path / "empty").mkdir()
        verification = await _service(harness, FakeDatabaseSnapshot()).verify(tmp_path / "empty")
        assert not verification.ok
        assert any("is not a backup" in problem for problem in verification.problems)


def _manifest(tmp_path: Path) -> BackupManifest:
    return BackupManifest.model_validate(
        json.loads((tmp_path / "backup" / MANIFEST_NAME).read_text(encoding="utf-8"))
    )


class TestCompatibility:
    """What a restore establishes before it touches the destination.

    Every one of these was an assumption in `restore.sh`, and all but one of
    them fails late — after the migrations, usually after somebody has told the
    lab it is back.
    """

    @pytest.fixture
    async def taken(self, installation, tmp_path):
        harness, _actor = installation
        await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")
        return _manifest(tmp_path)

    def test_a_matching_destination_is_permitted(self, taken) -> None:
        verdict = evaluate_restore(
            taken,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            migration_order=migration_order(),
        )
        assert verdict.permitted, [f.detail for f in verdict.blocking]

    def test_a_missing_extension_blocks(self, taken) -> None:
        target = _target(taken).model_copy(
            update={"extensions": {}, "installation_id": uuid4()}
        )
        verdict = evaluate_restore(taken, target=target, migration_order=migration_order())
        assert not verdict.permitted
        assert any(f.code == "extension_missing" for f in verdict.blocking)

    def test_an_older_extension_blocks(self, taken) -> None:
        target = _target(taken).model_copy(
            update={"extensions": {"vector": "0.5.1"}, "installation_id": uuid4()}
        )
        verdict = evaluate_restore(taken, target=target, migration_order=migration_order())
        assert not verdict.permitted
        assert any(f.code == "extension_older" for f in verdict.blocking)

    def test_a_newer_extension_is_advisory(self, taken) -> None:
        target = _target(taken).model_copy(
            update={"extensions": {"vector": "0.9.0"}, "installation_id": uuid4()}
        )
        verdict = evaluate_restore(taken, target=target, migration_order=migration_order())
        assert verdict.permitted
        assert any(f.code == "extension_newer" for f in verdict.advisory)

    def test_an_older_postgresql_blocks(self, taken) -> None:
        target = _target(taken).model_copy(
            update={"database_server_version": "15.8", "installation_id": uuid4()}
        )
        verdict = evaluate_restore(taken, target=target, migration_order=migration_order())
        assert not verdict.permitted
        assert any(f.code == "server_older" for f in verdict.blocking)

    def test_a_newer_postgresql_is_permitted_and_noted(self, taken) -> None:
        target = _target(taken).model_copy(
            update={
                "database_server_version": "17.2 (Debian 17.2-1.pgdg120+1)",
                "installation_id": uuid4(),
            }
        )
        verdict = evaluate_restore(taken, target=target, migration_order=migration_order())
        assert verdict.permitted
        assert any(f.code == "server_newer" for f in verdict.advisory)

    def test_a_dump_from_a_newer_schema_blocks(self, taken) -> None:
        """Restoring it into older code succeeds and then serves wrong answers,
        because the code does not know about the columns the dump filled."""
        future = taken.model_copy(
            update={
                "origin": taken.origin.model_copy(update={"schema_revision": "0099_not_yet"})
            }
        )
        verdict = evaluate_restore(
            future,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            migration_order=migration_order(),
        )
        assert not verdict.permitted
        assert any(f.code == "schema_from_the_future" for f in verdict.blocking)

    def test_an_unreadable_migration_history_is_reported_not_assumed(self, taken) -> None:
        verdict = evaluate_restore(
            taken,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            migration_order=(),
        )
        assert any(f.code == "migration_order_unknown" for f in verdict.advisory)

    def test_a_backup_behind_the_code_asks_for_migrations(self, taken) -> None:
        order = (*migration_order(), "0002_later")
        verdict = evaluate_restore(
            taken,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            migration_order=order,
        )
        assert verdict.permitted
        assert verdict.migrations_required

    def test_a_populated_destination_blocks(self, taken) -> None:
        """Found by running the suite against a real PostgreSQL: replaying a
        dump over existing tables fails on the first `CREATE TABLE`, and it
        fails partway — after the restore has already copied every object into
        the destination's store."""
        verdict = evaluate_restore(
            taken,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            migration_order=migration_order(),
            target_relation_count=57,
        )
        assert not verdict.permitted
        assert any(f.code == "destination_not_empty" for f in verdict.blocking)

    def test_clean_is_the_deliberate_alternative(self, taken) -> None:
        verdict = evaluate_restore(
            taken,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            migration_order=migration_order(),
            target_relation_count=57,
            clean_requested=True,
        )
        assert verdict.permitted

    def test_a_backup_that_records_a_hole_blocks(self, taken) -> None:
        holed = taken.model_copy(update={"incomplete": ("object payloads were skipped",)})
        verdict = evaluate_restore(
            holed,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            migration_order=migration_order(),
        )
        assert not verdict.permitted
        assert any(f.code == "backup_incomplete" for f in verdict.blocking)


class TestOneWriter:
    """Restoring a backup does not confer the right to publish.

    This is the failure that does not fail: two installations from one backup
    both work, and they diverge until somebody notices a release on one that is
    missing from the other.
    """

    @pytest.fixture
    async def taken(self, installation, tmp_path):
        harness, _actor = installation
        await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")
        return _manifest(tmp_path)

    def test_a_clone_may_not_publish(self, taken) -> None:
        fresh = uuid4()
        verdict = evaluate_restore(
            taken,
            target=_target(taken).model_copy(update={"installation_id": fresh}),
            intent=RestoreIntent.CLONE,
            migration_order=migration_order(),
        )
        assert verdict.permitted
        assert not verdict.may_publish
        assert verdict.installation_id == fresh
        assert any(f.code == "clone_may_not_publish" for f in verdict.advisory)

    def test_a_clone_may_not_keep_the_origin_identity(self, taken) -> None:
        verdict = evaluate_restore(
            taken,
            target=_target(taken),  # same id as the origin
            intent=RestoreIntent.CLONE,
            migration_order=migration_order(),
        )
        assert not verdict.permitted
        assert any(f.code == "clone_keeps_origin_identity" for f in verdict.blocking)

    def test_taking_over_without_fencing_is_refused(self, taken) -> None:
        verdict = evaluate_restore(
            taken,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            intent=RestoreIntent.REPLACE,
            origin_is_fenced=False,
            migration_order=migration_order(),
        )
        assert not verdict.permitted
        assert any(f.code == "origin_not_fenced" for f in verdict.blocking)

    def test_a_fenced_takeover_publishes_as_the_origin(self, taken) -> None:
        verdict = evaluate_restore(
            taken,
            target=_target(taken).model_copy(update={"installation_id": uuid4()}),
            intent=RestoreIntent.REPLACE,
            origin_is_fenced=True,
            migration_order=migration_order(),
        )
        assert verdict.permitted
        assert verdict.may_publish
        assert verdict.installation_id == taken.origin.installation_id
        assert any(f.code == "writer_taken_over" for f in verdict.advisory)

    def test_a_fenced_takeover_of_an_incompatible_backup_still_may_not_publish(
        self, taken
    ) -> None:
        """`may_publish` is not independent of the rest of the verdict: a
        blocked restore that nonetheless reported a right to publish would be
        read by a script as permission."""
        target = _target(taken).model_copy(
            update={"extensions": {}, "installation_id": uuid4()}
        )
        verdict = evaluate_restore(
            taken,
            target=target,
            intent=RestoreIntent.REPLACE,
            origin_is_fenced=True,
            migration_order=migration_order(),
        )
        assert not verdict.permitted
        assert not verdict.may_publish

    def test_a_derived_identity_changes_with_the_address(self, tmp_path) -> None:
        """A restore onto a new host, pointed at a new database and a new store,
        derives a different id and is therefore recognised as the clone it is."""
        here, source = installation_identity(
            Settings(object_store_root=tmp_path / "a", database_url="postgresql://h:1/x")
        )
        there, _ = installation_identity(
            Settings(object_store_root=tmp_path / "b", database_url="postgresql://h:1/x")
        )
        assert source == "derived"
        assert here != there

    def test_a_configured_identity_survives_a_move(self, tmp_path) -> None:
        fixed = UUID("11111111-2222-3333-4444-555555555555")
        here, source = installation_identity(
            Settings(installation_id=fixed, object_store_root=tmp_path / "a")
        )
        there, _ = installation_identity(
            Settings(installation_id=fixed, object_store_root=tmp_path / "b")
        )
        assert source == "configured"
        assert here == there == fixed


class TestRestore:
    async def test_a_preflight_changes_nothing(self, installation, tmp_path) -> None:
        harness, _actor = installation
        snapshot = FakeDatabaseSnapshot()
        service = _service(harness, snapshot)
        await service.create(tmp_path / "backup")

        report = await service.restore(
            tmp_path / "backup",
            intent=RestoreIntent.CLONE,
            new_installation_id=uuid4(),
            migration_order=migration_order(),
        )
        assert not report.applied
        assert snapshot.restored is None
        assert report.verdict is not None and report.verdict.permitted
        assert any("re-run with --apply" in step for step in report.next_steps)

    async def test_a_restore_puts_the_objects_back_and_replays_the_dump(
        self, installation, tmp_path
    ) -> None:
        """A genuinely fresh destination: empty store, and a registry that only
        arrives with the dump.

        What this pins down is that the payloads and the control plane both
        arrive, and that the copy is driven by the restored registry rather than
        by the source's. It does **not** prove the ordering: an in-memory
        registry has its tables whether or not a dump has been replayed, so the
        copy falls back to enumerating the backup store and succeeds either way.
        The ordering is only observable against a database that has no schema
        until the dump creates one — see
        `TestAgainstRealPostgres::test_a_restore_into_a_fresh_database`, which is
        where running it the wrong way round actually fails.
        """
        harness, actor = installation
        await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")

        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(actor.workspace_id, limit=500)
        expected = {
            record.id: await harness.object_store.get(record.id)
            for record in records
            if not record.tombstoned
        }
        assert expected

        destination = build_harness(
            object_store=FilesystemObjectStore(tmp_path / "restored-objects")
        )

        async def replay_brings_the_registry_back() -> None:
            async with destination.context.uow_factory() as uow:
                for record in records:
                    await uow.objects.register(record)
                await uow.commit()

        snapshot = FakeDatabaseSnapshot(on_restore=replay_brings_the_registry_back)
        service = BackupService(
            destination.context,
            snapshot=snapshot,
            installation_id=uuid4(),
            store_factory=FilesystemObjectStore,
        )

        async with destination.context.uow_factory() as uow:
            assert await uow.objects.list_all_objects(limit=5) == [], "starts empty"

        report = await service.restore(
            tmp_path / "backup",
            intent=RestoreIntent.CLONE,
            apply=True,
            migration_order=migration_order(),
        )

        assert report.ok, report.problems
        assert report.applied
        assert snapshot.restored == tmp_path / "backup" / "database/dump.pgcustom"
        assert report.objects is not None
        assert report.objects.copied == len(expected)
        for object_id, payload in expected.items():
            assert await destination.context.object_store.get(object_id) == payload

    async def test_a_refused_restore_never_reaches_the_database(
        self, installation, tmp_path
    ) -> None:
        """The order matters: a control plane restored over payloads that did not
        arrive is the broken-citation case, and it is worse than stopping early."""
        harness, _actor = installation
        snapshot = FakeDatabaseSnapshot()
        await _service(harness, snapshot).create(tmp_path / "backup")

        target = FakeDatabaseSnapshot(extensions={})  # no pgvector at the destination
        service = BackupService(
            harness.context,
            snapshot=target,
            installation_id=uuid4(),
            store_factory=FilesystemObjectStore,
        )
        report = await service.restore(
            tmp_path / "backup",
            intent=RestoreIntent.CLONE,
            apply=True,
            migration_order=migration_order(),
        )
        assert not report.ok
        assert not report.applied
        assert target.restored is None

    async def test_a_populated_destination_stops_before_anything_moves(
        self, installation, tmp_path
    ) -> None:
        harness, _actor = installation
        await _service(harness, FakeDatabaseSnapshot()).create(tmp_path / "backup")

        empty = FilesystemObjectStore(tmp_path / "restored-objects")
        occupied = FakeDatabaseSnapshot(relation_count=57)
        service = BackupService(
            replace(harness.context, object_store=empty),
            snapshot=occupied,
            installation_id=uuid4(),
            store_factory=FilesystemObjectStore,
        )
        report = await service.restore(
            tmp_path / "backup", apply=True, migration_order=migration_order()
        )
        assert not report.ok
        assert not report.applied
        assert occupied.restored is None
        assert report.objects is None, "no payload should have been copied"

    async def test_the_next_steps_name_the_verification(self, installation, tmp_path) -> None:
        """A restore that considered itself finished is how an installation ends
        up serving before anybody ran `lab db verify-restore`."""
        harness, _actor = installation
        snapshot = FakeDatabaseSnapshot()
        await _service(harness, snapshot).create(tmp_path / "backup")
        empty = FilesystemObjectStore(tmp_path / "restored-objects")
        service = BackupService(
            replace(harness.context, object_store=empty),
            snapshot=snapshot,
            installation_id=uuid4(),
            store_factory=FilesystemObjectStore,
        )
        report = await service.restore(
            tmp_path / "backup",
            apply=True,
            migration_order=migration_order(),
        )
        assert any("verify-restore" in step for step in report.next_steps)
        assert any("secret manager" in step for step in report.next_steps)
        assert any("may not publish" in step for step in report.next_steps)


class TestTheOperatorCommand:
    """The surface the runbook tells someone to type."""

    async def test_create_verify_and_preflight_through_the_cli(
        self, installation, tmp_path, capsys
    ) -> None:
        from types import SimpleNamespace

        from lab_assistant.cli.main import _backup

        harness, _actor = installation
        snapshot = FakeDatabaseSnapshot()
        container = SimpleNamespace(
            context=harness.context, shutdown=[], database_snapshot=snapshot
        )
        settings = harness.context.settings

        code = await _backup(
            SimpleNamespace(
                backup_command="create",
                directory=tmp_path / "backup",
                dry_run=False,
                skip_objects=False,
                verify="digest",
            ),
            settings,
            container=container,
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["manifest"]["format_name"] == "lab-assistant-recovery"

        code = await _backup(
            SimpleNamespace(
                backup_command="verify", directory=tmp_path / "backup", skip_objects=False
            ),
            settings,
            container=container,
        )
        assert code == 0
        assert json.loads(capsys.readouterr().out)["verified"] is True

        code = await _backup(
            SimpleNamespace(
                backup_command="restore",
                directory=tmp_path / "backup",
                apply=False,
                intent="replace",
                origin_is_fenced=False,
                clean=False,
                verify="digest",
            ),
            settings,
            container=container,
        )
        # A takeover with nothing fenced is refused, and the command says so
        # with a non-zero code a script can gate on.
        assert code == 1
        assert json.loads(capsys.readouterr().out)["verdict"]["permitted"] is False

    async def test_a_failed_verify_exits_non_zero(self, installation, tmp_path, capsys) -> None:
        from types import SimpleNamespace

        from lab_assistant.cli.main import _backup

        harness, _actor = installation
        container = SimpleNamespace(
            context=harness.context, shutdown=[], database_snapshot=FakeDatabaseSnapshot()
        )
        code = await _backup(
            SimpleNamespace(
                backup_command="verify", directory=tmp_path / "nothing", skip_objects=False
            ),
            harness.context.settings,
            container=container,
        )
        assert code == 1
        capsys.readouterr()


class TestVolumeDiscovery:
    """`restore.sh` hardcoded `lab-assistant_pgdata` and `lab-assistant_objects`.

    A deployment under a different Compose project name has different volumes,
    so the script removed the contents of a volume it had guessed at — or, when
    the guess did not exist, created an empty one and restored into nothing.
    """

    def _run(self, tmp_path: Path, function: str, *, volumes: str) -> subprocess.CompletedProcess:
        fake = tmp_path / "docker"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "volume" ]; then\n'
            f"  printf '%s\\n' \"{volumes}\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )
        fake.chmod(0o755)
        return subprocess.run(
            [
                "bash",
                "-c",
                f'source "$1"; {function}',
                "discovery",
                str(ROOT / "infra/backup/lib/volumes.sh"),
            ],
            env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}"),
            capture_output=True,
            text=True,
        )

    def test_it_resolves_the_volume_compose_actually_declares(self, tmp_path: Path) -> None:
        if not shutil.which("bash"):  # pragma: no cover - every supported host has bash
            pytest.skip("bash is required")
        result = self._run(
            tmp_path, "resolve_volume pgdata", volumes="otherproject_pgdata\notherproject_objects"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "otherproject_pgdata"

    def test_it_refuses_to_guess_when_nothing_matches(self, tmp_path: Path) -> None:
        if not shutil.which("bash"):  # pragma: no cover
            pytest.skip("bash is required")
        result = self._run(tmp_path, "resolve_volume pgdata", volumes="unrelated_volume")
        assert result.returncode != 0
        assert "pgdata" in result.stderr

    def test_it_refuses_when_the_name_is_ambiguous(self, tmp_path: Path) -> None:
        """Two candidates means two deployments on one host. Picking one is how
        a restore lands in the wrong installation."""
        if not shutil.which("bash"):  # pragma: no cover
            pytest.skip("bash is required")
        result = self._run(
            tmp_path, "resolve_volume pgdata", volumes="alpha_pgdata\nbeta_pgdata"
        )
        assert result.returncode != 0
        assert "ambiguous" in result.stderr.lower()


@pytest.mark.postgres
class TestAgainstRealPostgres:
    """The adapter the fake stands in for, against a server that can refuse it.

    Offline tests prove the bookkeeping. They cannot prove that `pg_dump` is on
    PATH, that the fingerprint query returns what the compatibility rules expect,
    or that a dump this code took replays into a database this code migrated.
    """

    @pytest.fixture
    async def engine(self, postgres_url):
        """A schema, emptied before each test.

        The PostgreSQL database is shared by the whole session and the id
        factory is deterministic, so without the reset the second test here
        fails on a primary key before it reaches its subject (audit F16).
        """
        from lab_assistant.adapters.db.engine import create_schema, make_engine
        from lab_assistant.testing import reset_database

        engine = make_engine(Settings(database_url=postgres_url))
        await create_schema(engine, dimensions=(64,))
        await reset_database(engine)
        yield engine
        await engine.dispose()

    async def test_the_fingerprint_is_what_the_rules_compare(
        self, engine, postgres_url
    ) -> None:
        from lab_assistant.adapters.db.snapshot import PostgresDatabaseSnapshot

        snapshot = PostgresDatabaseSnapshot(engine, database_url=postgres_url)
        fingerprint = await snapshot.fingerprint()
        assert fingerprint.server_version
        # pgvector is what the vector indexes need, and the rule that blocks an
        # older one is only meaningful if the version is actually readable here.
        assert "vector" in fingerprint.extensions
        assert fingerprint.extensions["vector"][0].isdigit()
        assert "plpgsql" not in fingerprint.extensions

    async def test_a_dump_restores_into_a_fresh_database(
        self, engine, postgres_url, tmp_path
    ) -> None:
        """The portability contract: a dump taken here replays into an empty
        database elsewhere, carrying its rows.

        Into a *fresh* one. The first run of this test replayed into the
        database it had just dumped and failed on `relation already exists`,
        which is how the `destination_not_empty` rule came to exist: that
        failure arrives partway through a restore, after the payloads have
        already been copied in.
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from lab_assistant.adapters.db.snapshot import PostgresDatabaseSnapshot

        if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
            pytest.skip("the PostgreSQL client tools are not on PATH")

        snapshot = PostgresDatabaseSnapshot(engine, database_url=postgres_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE IF NOT EXISTS backup_probe (id int primary key)")
            )
            await connection.execute(text("INSERT INTO backup_probe VALUES (1)"))

        result = await snapshot.dump(tmp_path / "dump.pgcustom")
        assert result.byte_length > 0
        assert (
            result.sha256
            == hashlib.sha256((tmp_path / "dump.pgcustom").read_bytes()).hexdigest()
        )
        assert "pg_dump" in result.tool_version

        # A second database on the same server stands in for the new VM.
        target_name = f"lab_restore_probe_{uuid4().hex[:8]}"
        admin = create_async_engine(postgres_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{target_name}"'))

            target_url = postgres_url.rsplit("/", 1)[0] + "/" + target_name
            target_engine = create_async_engine(target_url)
            try:
                target = PostgresDatabaseSnapshot(target_engine, database_url=target_url)
                before = await target.fingerprint()
                assert before.relation_count == 0, "the stand-in VM starts empty"

                await target.restore(tmp_path / "dump.pgcustom")

                async with target_engine.connect() as connection:
                    rows = (
                        await connection.execute(text("SELECT id FROM backup_probe"))
                    ).all()
                assert [row[0] for row in rows] == [1]
                after = await target.fingerprint()
                assert after.relation_count > 1
            finally:
                await target_engine.dispose()
        finally:
            async with admin.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{target_name}" WITH (FORCE)')
                )
            await admin.dispose()
            async with engine.begin() as connection:
                await connection.execute(text("DROP TABLE IF EXISTS backup_probe"))

    async def test_the_fingerprint_sees_a_populated_destination(
        self, engine, postgres_url
    ) -> None:
        """The count the `destination_not_empty` rule reads is a real count."""
        from lab_assistant.adapters.db.snapshot import PostgresDatabaseSnapshot

        fingerprint = await PostgresDatabaseSnapshot(
            engine, database_url=postgres_url
        ).fingerprint()
        assert fingerprint.relation_count > 1

    async def test_a_restore_into_a_fresh_database(
        self, engine, postgres_url, tmp_path
    ) -> None:
        """The acceptance case, and the one that fails when the steps are
        ordered the wrong way round.

        A fresh database has no schema at all until the dump creates it, so a
        restore that copies objects first does not merely copy nothing — the
        registry query raises `UndefinedTable`. That is what happened the first
        time this ran, against a restore that put the payloads first because a
        backup puts them first. Restoring is not the mirror image of backing up.
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from lab_assistant.adapters.db.repositories import sql_uow_factory
        from lab_assistant.adapters.db.snapshot import PostgresDatabaseSnapshot
        from lab_assistant.application.objects import register_object

        if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
            pytest.skip("the PostgreSQL client tools are not on PATH")

        source = build_harness(
            uow_factory=sql_uow_factory(engine),
            object_store=FilesystemObjectStore(tmp_path / "live"),
        )
        user = await source.accounts.create_user(
            email=f"probe-{uuid4().hex[:8]}@lab.invalid", password="correct horse battery"
        )
        workspace, _ = await source.accounts.create_workspace(
            slug=f"probe-{uuid4().hex[:8]}", display_name="Probe", owner_user_id=user.id
        )
        payloads = {}
        for index in range(3):
            stored = await source.context.object_store.put(
                f"probe payload {index}".encode(),
                workspace_id=workspace.id,
                object_class="asset",
            )
            async with source.context.uow_factory() as uow:
                await register_object(uow, stored)
                await uow.commit()
            payloads[stored.id] = await source.context.object_store.get(stored.id)

        await BackupService(
            source.context,
            snapshot=PostgresDatabaseSnapshot(engine, database_url=postgres_url),
            installation_id=uuid4(),
            store_factory=FilesystemObjectStore,
        ).create(tmp_path / "backup")

        target_name = f"lab_restore_e2e_{uuid4().hex[:8]}"
        admin = create_async_engine(postgres_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{target_name}"'))
            target_url = postgres_url.rsplit("/", 1)[0] + "/" + target_name
            target_engine = create_async_engine(target_url)
            try:
                # pgvector, which the preflight insists on — and which the first
                # real run of this correctly refused to proceed without.
                async with target_engine.begin() as connection:
                    await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

                destination = build_harness(
                    uow_factory=sql_uow_factory(target_engine),
                    object_store=FilesystemObjectStore(tmp_path / "restored"),
                )
                report = await BackupService(
                    destination.context,
                    snapshot=PostgresDatabaseSnapshot(
                        target_engine, database_url=target_url
                    ),
                    installation_id=uuid4(),
                    store_factory=FilesystemObjectStore,
                ).restore(
                    tmp_path / "backup",
                    intent=RestoreIntent.CLONE,
                    apply=True,
                    migration_order=migration_order(),
                )

                assert report.ok, report.problems
                assert report.applied
                assert report.objects is not None and report.objects.copied == len(payloads)

                # The control plane came back with it.
                async with destination.context.uow_factory() as uow:
                    restored = await uow.workspaces.get_by_slug(workspace.slug)
                assert restored is not None and restored.id == workspace.id

                for object_id, payload in payloads.items():
                    assert await destination.context.object_store.get(object_id) == payload
            finally:
                await target_engine.dispose()
        finally:
            async with admin.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{target_name}" WITH (FORCE)')
                )
            await admin.dispose()

    async def test_a_missing_dump_is_a_named_failure(self, engine, postgres_url, tmp_path) -> None:
        from lab_assistant.adapters.db.snapshot import PostgresDatabaseSnapshot

        snapshot = PostgresDatabaseSnapshot(engine, database_url=postgres_url)
        with pytest.raises(LabAssistantError) as excinfo:
            await snapshot.restore(tmp_path / "absent.pgcustom")
        assert excinfo.value.error_class.value == "not_found"


def _target(manifest: BackupManifest):
    """A destination fingerprint identical to the backup's origin.

    Each compatibility test then changes exactly one thing, so a failure names
    the rule that fired rather than a difference nobody meant to introduce.
    """
    return manifest.origin
