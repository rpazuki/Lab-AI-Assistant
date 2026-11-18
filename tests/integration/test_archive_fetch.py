"""A published snapshot is complete, and a reader can take it with GET alone.

Phase 2 proved a snapshot *directory* carried its meaning. It did not prove the
archive did: publication uploaded the manifest and the catalog shards, and the
chunks, the vectors and every payload stayed on the producer. These tests are
about the archive as a reader meets it — through a credential that reads known
keys and nothing else.

Two properties run through all of them. **Nothing unchecked is kept**: every
object is verified against what the manifest declares before it is written, and
the manifest against what the head committed to. And **what may not leave does
not**: a payload, and its text in every other shape, travels only where a rights
decision grants `replicate_content`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab_assistant.adapters.object_store.control import FilesystemControlStore
from lab_assistant.adapters.signing import Ed25519Signer, Ed25519Verifier, generate_keypair
from lab_assistant.application.archive_catalog import (
    INVENTORY_KIND,
    MANIFEST_NAME,
    ArchiveCatalogBuilder,
)
from lab_assistant.application.archive_control import library_prefix
from lab_assistant.application.archive_fetch import SnapshotFetcher
from lab_assistant.application.archive_policy import PolicyCheckpointBuilder, PolicyService
from lab_assistant.application.archive_publish import PublicationService
from lab_assistant.application.archive_validate import validate_snapshot
from lab_assistant.contracts.archive import SnapshotManifest, content_location
from lab_assistant.contracts.archive_catalog import APPLY_ORDER, RecordKind
from lab_assistant.contracts.canonical import canonical_json
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.policy import Effect, PolicyAction, ResourceType, SubjectType
from lab_assistant.contracts.publication import HEAD_LOCATION, HeadDescriptor
from lab_assistant.contracts.release import ReleaseItemRecord
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import publish_archive_fixture, replicating_rights_policy
from lab_assistant.fixtures.archive import RESTRICTED
from lab_assistant.testing import build_harness, make_actor

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)
KEY_ID = "rla-publisher-2026"


class ReadOnly:
    """What a reader's credential is: GET by name. No list, no write."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.reads: list[str] = []

    async def read(self, key):
        self.reads.append(key)
        return await self._inner.read(key)

    async def create(self, key, data):
        raise AssertionError(f"a reader wrote {key}")

    async def compare_and_swap(self, key, data, *, expected_version):
        raise AssertionError(f"a reader swapped {key}")


async def _library(tmp_path, *, rights_policy):
    private, public = generate_keypair()
    harness = build_harness(rights_policy=rights_policy)
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
            policy_basis="fetch_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    fixture = await publish_archive_fixture(harness, actor)

    signer = Ed25519Signer(private, key_identifier=KEY_ID)
    verifier = Ed25519Verifier({KEY_ID: public})
    control = FilesystemControlStore(tmp_path / "archive")
    library_id, installation_id = uuid4(), uuid4()

    async def package(name: str):
        build = await ArchiveCatalogBuilder(
            harness.context,
            library_id=library_id,
            installation_id=installation_id,
            signer=signer,
        ).build(workspace.id, tmp_path / name)
        assert build.ok, build.problems
        return build, tmp_path / name

    async def publish(directory):
        report = await PublicationService(
            harness.context,
            control=control,
            library_id=library_id,
            installation_id=installation_id,
            verifier=verifier,
        ).publish(workspace.id, directory)
        assert report.ok, report.problems
        return report

    async def issue_policy():
        return await PolicyService(
            harness.context,
            control=control,
            builder=PolicyCheckpointBuilder(
                harness.context,
                library_id=library_id,
                installation_id=installation_id,
                signer=signer,
            ),
            library_id=library_id,
            installation_id=installation_id,
        ).issue(workspace.id)

    def fetcher(store=None):
        return SnapshotFetcher(store or ReadOnly(control), library_id=library_id, verifier=verifier)

    return SimpleNamespace(
        harness=harness,
        workspace=workspace,
        actor=actor,
        fixture=fixture,
        control=control,
        archive=tmp_path / "archive",
        library_id=library_id,
        package=package,
        publish=publish,
        issue_policy=issue_policy,
        fetcher=fetcher,
        verifier=verifier,
        public_key=public,
        tmp_path=tmp_path,
    )


@pytest.fixture
async def library(tmp_path):
    return await _library(tmp_path, rights_policy=replicating_rights_policy())


@pytest.fixture
async def published(library):
    await library.issue_policy()
    build, directory = await library.package("snapshot")
    report = await library.publish(directory)
    return library, build, directory, report


def _now(library):
    return library.harness.context.now()


def _files(directory) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _content(library, sha256):
    return library.archive / library_prefix(library.library_id) / content_location(sha256)


class TestTheArchiveIsComplete:
    async def test_publication_carries_payloads_and_projections(self, published) -> None:
        library, build, directory, report = published
        assert report.payloads_uploaded == build.counts["payload_copied"] > 0
        manifest = build.manifest
        snapshot_root = (
            library.archive
            / library_prefix(library.library_id)
            / "snapshots"
            / str(build.snapshot_id)
        )
        for ref in manifest.projection_refs:
            for shard in ref.shards:
                assert (snapshot_root / shard.location).is_file(), shard.location
        for path in (directory / "content").rglob("*"):
            if path.is_file():
                assert _content(library, path.name).read_bytes() == path.read_bytes()

    async def test_a_payload_already_in_the_archive_is_not_uploaded_again(self, published) -> None:
        """Shared across snapshots: a publication that changed nothing moves no bytes."""
        library, build, _directory, _report = published
        again, directory = await library.package("again")
        report = await PublicationService(
            library.harness.context,
            control=library.control,
            library_id=library.library_id,
            installation_id=build.manifest.origin_installation_id,
            verifier=library.verifier,
        ).publish(library.workspace.id, directory)
        assert report.ok, report.problems
        assert report.payloads_uploaded == 0
        assert report.payloads_present == again.counts["payload_copied"]

    async def test_the_catalog_carries_what_a_reader_database_requires(self, published) -> None:
        """Parse runs (a rendition needs one), experiments (claims name them),
        and entry digests that rebuild exactly."""
        _library, _build, directory, _report = published
        rows = {
            kind: [
                json.loads(line)
                for line in (directory / f"catalog/{kind.value}.jsonl").read_bytes().splitlines()
            ]
            for kind in (
                RecordKind.PARSE_RUN,
                RecordKind.RENDITION,
                RecordKind.EXPERIMENT,
                RecordKind.CLAIM,
                RecordKind.RELEASE_ITEM,
            )
        }
        runs = {row["record_id"] for row in rows[RecordKind.PARSE_RUN]}
        assert {row["parse_run_id"] for row in rows[RecordKind.RENDITION]} <= runs
        experiments = {row["record_id"] for row in rows[RecordKind.EXPERIMENT]}
        assert experiments
        assert {row["experiment_id"] for row in rows[RecordKind.CLAIM]} <= experiments
        for entry in rows[RecordKind.RELEASE_ITEM]:
            rebuilt = ReleaseItemRecord(
                **{
                    key: entry[key]
                    for key in (
                        "work_id",
                        "manifestation_id",
                        "asset_id",
                        "rendition_id",
                        "rendition_digest",
                        "claim_revision_ids",
                        "access_policy_snapshot_id",
                        "inclusion_reason",
                    )
                }
            )
            assert rebuilt.content_digest() == entry["content_digest"]
        # The order an importer applies: a decision before the asset naming it.
        assert APPLY_ORDER.index(RecordKind.RIGHTS_DECISION) < APPLY_ORDER.index(RecordKind.ASSET)
        assert validate_snapshot(directory, payloads=True).ok


class TestAReaderFetches:
    async def test_everything_arrives_by_name_and_nothing_is_written(self, published) -> None:
        library, _build, directory, _report = published
        store = ReadOnly(library.control)
        destination = library.tmp_path / "reader"

        report = await library.fetcher(store).fetch(destination, now=_now(library))
        assert report.ok, (report.problems, report.validation.blocking)
        assert report.policy_sequence == 1
        assert report.payloads > 0 and report.withheld == 0
        # Byte for byte the snapshot that was published, plus the policy it
        # is served under. ReadOnly would have raised on any write.
        fetched = _files(destination)
        for relative, data in _files(directory).items():
            assert fetched[relative] == data, relative
        assert any(name.startswith("policy/") for name in fetched)

    async def test_nothing_is_fetched_before_anything_is_published(self, library) -> None:
        report = await library.fetcher().fetch(library.tmp_path / "reader", now=_now(library))
        assert not report.ok
        assert "published nothing" in report.problems[0]

    async def test_a_manifest_replaced_under_the_head_is_refused(self, published) -> None:
        """A bucket writer who cannot move the head can still overwrite what it
        points at; the head's digest is how that is caught."""
        library, build, _directory, _report = published
        key_path = (
            library.archive
            / library_prefix(library.library_id)
            / "snapshots"
            / str(build.snapshot_id)
            / MANIFEST_NAME
        )
        # Material, and plausible: the same snapshot serving one release fewer.
        # (An identical re-packaging would not be a forgery — under the test
        # clock it digests the same, which is the determinism working.)
        forged = build.manifest.model_copy(
            update={"scientific_release_ids": build.manifest.scientific_release_ids[:1]}
        )
        assert forged.signing_digest() != build.manifest.signing_digest()
        key_path.write_bytes(canonical_json(forged.model_dump(mode="python")))

        report = await library.fetcher().fetch(library.tmp_path / "reader", now=_now(library))
        assert not report.ok
        assert "not the one the head committed to" in report.problems[0]

    async def test_a_corrupt_payload_is_not_kept(self, published) -> None:
        library, _build, directory, _report = published
        victim = next(path for path in (directory / "content").rglob("*") if path.is_file())
        _content(library, victim.name).write_bytes(b"not the paper")

        destination = library.tmp_path / "reader"
        report = await library.fetcher().fetch(destination, now=_now(library))
        assert not report.ok
        assert any("digesting to" in problem for problem in report.problems)
        assert not (destination / content_location(victim.name)).exists()

    async def test_a_missing_payload_is_reported_missing(self, published) -> None:
        library, _build, directory, _report = published
        victim = next(path for path in (directory / "content").rglob("*") if path.is_file())
        _content(library, victim.name).unlink()

        report = await library.fetcher().fetch(library.tmp_path / "reader", now=_now(library))
        assert not report.ok
        assert any("absent from the archive" in problem for problem in report.problems)
        assert any(f.code == "payload_missing" for f in report.validation.blocking)

    async def test_a_denial_is_not_an_absence(self, published) -> None:
        """S27: forbidden raises. Read as missing, it would send somebody to
        acquire the paper again."""
        library, _build, _directory, _report = published

        class Denying(ReadOnly):
            async def read(self, key):
                if "/content/" in key:
                    raise LabAssistantError("forbidden").with_class(ErrorClass.STORAGE_DENIED)
                return await super().read(key)

        with pytest.raises(LabAssistantError) as raised:
            await library.fetcher(Denying(library.control)).fetch(
                library.tmp_path / "reader", now=_now(library)
            )
        assert raised.value.error_class is ErrorClass.STORAGE_DENIED

    async def test_an_interrupted_fetch_resumes_without_refetching(self, published) -> None:
        library, build, _directory, _report = published

        class Dropping(ReadOnly):
            """The connection drops after a few payloads."""

            def __init__(self, inner, *, payloads: int) -> None:
                super().__init__(inner)
                self._left = payloads

            async def read(self, key):
                if "/content/" in key:
                    if self._left == 0:
                        raise LabAssistantError("timed out").with_class(ErrorClass.TIMEOUT)
                    self._left -= 1
                return await super().read(key)

        destination = library.tmp_path / "reader"
        with pytest.raises(LabAssistantError):
            await library.fetcher(Dropping(library.control, payloads=3)).fetch(
                destination, now=_now(library)
            )

        report = await library.fetcher().fetch(destination, now=_now(library))
        assert report.ok, report.problems
        # Only the payloads the first attempt did not reach are fetched again.
        assert report.fetched == build.counts["payload_copied"] - 3
        assert report.payloads == build.counts["payload_copied"]

    async def test_a_directory_holding_another_snapshot_is_refused(self, published) -> None:
        library, build, _directory, _report = published
        destination = library.tmp_path / "reader"
        assert (await library.fetcher().fetch(destination, now=_now(library))).ok

        _next, directory = await library.package("next")
        await PublicationService(
            library.harness.context,
            control=library.control,
            library_id=library.library_id,
            installation_id=build.manifest.origin_installation_id,
            verifier=library.verifier,
        ).publish(library.workspace.id, directory)

        report = await library.fetcher().fetch(destination, now=_now(library))
        assert not report.ok
        assert "already holds a different snapshot" in report.problems[0]

    async def test_a_location_that_escapes_the_directory_is_never_followed(self, published) -> None:
        """S34, as a compromised publisher would attempt it: a manifest the head
        genuinely commits to, naming a shard outside the reader's directory."""
        library, build, _directory, _report = published
        root = library.archive / library_prefix(library.library_id)
        manifest_path = root / "snapshots" / str(build.snapshot_id) / MANIFEST_NAME
        manifest = SnapshotManifest.model_validate_json(manifest_path.read_bytes())
        shards = list(manifest.catalog_shards)
        shards[0] = shards[0].model_copy(update={"location": "../../escaped.jsonl"})
        evil = manifest.model_copy(
            update={"catalog_shards": tuple(shards), "signature": None, "key_identifier": None}
        )
        manifest_path.write_bytes(canonical_json(evil.model_dump(mode="python")))
        head = HeadDescriptor.model_validate_json((root / HEAD_LOCATION).read_bytes())
        (root / HEAD_LOCATION).write_bytes(
            head.model_copy(update={"snapshot_manifest_digest": evil.signing_digest()}).serialise()
        )

        destination = library.tmp_path / "deep" / "reader"
        report = await library.fetcher().fetch(destination, now=_now(library))
        assert not report.ok
        assert any("not a plain relative path" in problem for problem in report.problems)
        assert not (library.tmp_path / "escaped.jsonl").exists()
        assert any(f.code == "shard_location_unsafe" for f in report.validation.blocking)


class TestWhatMayNotLeaveDoesNot:
    async def test_without_replication_the_records_travel_and_the_text_does_not(
        self, tmp_path
    ) -> None:
        """The deployed default: nobody has decided replication is permitted."""
        from lab_assistant.policy.rights import default_open_access_policy

        library = await _library(tmp_path, rights_policy=default_open_access_policy())
        build, directory = await library.package("snapshot")
        assert build.counts["payload_copied"] == 0
        assert build.counts["payload_withheld"] == build.counts["artifact"] > 0
        assert build.counts["projection_chunk"] == 0
        assert not (directory / "content").exists()
        # The records still travel: a reader knows the papers and their claims.
        assert build.counts["work"] == 4 and build.counts["claim_revision"] > 0
        inventory = [
            json.loads(line)
            for line in (directory / f"catalog/{INVENTORY_KIND}.jsonl").read_bytes().splitlines()
        ]
        assert {row["unavailable"] for row in inventory} == {"withheld_by_rights"}
        assert {str(i) for i in build.manifest.missing_optional_artifacts} == {
            row["artifact_id"] for row in inventory
        }
        assert validate_snapshot(directory, payloads=True).ok

        await library.publish(directory)
        assert not (library.archive / library_prefix(library.library_id) / "content").exists()
        report = await library.fetcher().fetch(tmp_path / "reader", now=_now(library))
        assert report.ok, report.problems
        assert report.payloads == 0 and report.withheld == build.counts["artifact"]

    async def test_one_narrowed_decision_withholds_one_paper(self, library) -> None:
        """A steward withdraws replication from one paper. Its bytes, its text
        and its passages stay home; every other paper's travel."""
        async with library.harness.context.uow_factory() as uow:
            assets = await uow.assets.list_assets(
                library.workspace.id, work_id=library.fixture.work_ids[RESTRICTED]
            )
            decision = await uow.policies.get_rights_decision(
                library.workspace.id, assets[0].rights_decision_id
            )
            await uow.policies.add_rights_decision(
                decision.model_copy(
                    update={
                        "allowed_actions": decision.allowed_actions
                        - {PolicyAction.REPLICATE_CONTENT}
                    }
                )
            )
            await uow.commit()

        build, directory = await library.package("snapshot")
        withheld_digests = {
            ref["sha256"]
            for ref in (
                json.loads(line)
                for line in (directory / f"catalog/{INVENTORY_KIND}.jsonl")
                .read_bytes()
                .splitlines()
            )
            if ref["unavailable"]
        }
        assert assets[0].sha256 in withheld_digests
        assert len(withheld_digests) == 2  # the asset and its rendition
        assert build.counts["payload_copied"] == build.counts["artifact"] - 2

        restricted_work = str(library.fixture.work_ids[RESTRICTED])
        for ref in build.manifest.projection_refs:
            for shard in ref.shards:
                if shard.kind == "projection_chunk":
                    rows = [
                        json.loads(line)
                        for line in (directory / shard.location).read_bytes().splitlines()
                    ]
                    assert rows and restricted_work not in {row["work_id"] for row in rows}
        assert validate_snapshot(directory, payloads=True).ok

    async def test_withheld_bytes_found_in_a_snapshot_are_blocking(self, tmp_path) -> None:
        """Content that left without the permission the inventory says it lacked."""
        from lab_assistant.policy.rights import default_open_access_policy

        library = await _library(tmp_path, rights_policy=default_open_access_policy())
        _build, directory = await library.package("snapshot")
        async with library.harness.context.uow_factory() as uow:
            asset = (await uow.assets.list_assets(library.workspace.id))[0]
        leaked = directory / content_location(asset.sha256)
        leaked.parent.mkdir(parents=True, exist_ok=True)
        leaked.write_bytes(await library.harness.object_store.get(asset.object_id))

        report = validate_snapshot(directory, payloads=True)
        assert any(f.code == "payload_withheld_present" for f in report.blocking)

    async def test_replication_is_never_granted_to_attested_uploads(self) -> None:
        """Attested access for one person is not a licence to copy to every reader."""
        from lab_assistant.domain.assets import AcquisitionRoute
        from lab_assistant.policy.rights import RightsRequest, default_open_access_policy

        verdict = default_open_access_policy(replicate_open_content=True).evaluate(
            RightsRequest(
                workspace_id=uuid4(),
                subject_type="asset",
                subject_id=uuid4(),
                licence_expression="publisher-specific",
                route=AcquisitionRoute.USER_ASSISTED,
                user_attestation="I have access through my institution",
                requested_actions=frozenset(
                    {PolicyAction.PROCESS_LOCAL, PolicyAction.REPLICATE_CONTENT}
                ),
            )
        )
        assert PolicyAction.PROCESS_LOCAL in verdict.allowed_actions
        assert PolicyAction.REPLICATE_CONTENT not in verdict.allowed_actions


class TestCommandLine:
    async def test_lab_archive_fetch_needs_no_database(self, published, capsys) -> None:
        """A reader's first command, run before it has a corpus. The container
        here has a clock and nothing else, so any database access would fail."""
        from lab_assistant.cli.main import _archive

        library, _build, _directory, _report = published
        settings = library.harness.settings.model_copy(
            update={
                "object_store_root": library.archive,
                "archive_trusted_keys": {KEY_ID: library.public_key},
            }
        )
        clock_only = SimpleNamespace(context=SimpleNamespace(now=library.harness.context.now))
        destination = library.tmp_path / "cli-reader"
        code = await _archive(
            SimpleNamespace(
                archive_command="fetch",
                directory=destination,
                library_id=str(library.library_id),
            ),
            settings,
            container=clock_only,
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 0, out
        assert out["ok"] and out["payloads"] > 0
        assert out["validation"]["policy"]["verdict"]["status"] == "current"

        code = await _archive(
            SimpleNamespace(
                archive_command="verify", directory=destination, policy=None, payloads=True
            ),
            settings,
            container=clock_only,
        )
        assert code == 0
