"""The shared-archive plan's success definition, offline.

    A fresh authorised reader, starting with no corpus DB and GET-only archive
    credentials, imports an approved snapshot and can search, ask and open
    citations without downloading from scholarly sources, reparsing papers,
    re-extracting claims or regenerating stored embeddings.

Two installations here share nothing but an archive directory: the reader has
its own database, its own object store and its own search index, and reaches
the archive through a store that refuses every write. The reader's document
embedder and extractor are replaced with ones that fail if called, so "without
re-extracting or re-embedding" is asserted rather than hoped.

What this cannot show is the cloud half — a real bucket and an IAM policy that
truly grants GET and nothing else. That is the deployment acceptance.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab_assistant.adapters.object_store.control import FilesystemControlStore
from lab_assistant.adapters.signing import Ed25519Signer, Ed25519Verifier, generate_keypair
from lab_assistant.application.archive_catalog import MANIFEST_NAME, ArchiveCatalogBuilder
from lab_assistant.application.archive_control import read_policy_head
from lab_assistant.application.archive_fetch import SnapshotFetcher
from lab_assistant.application.archive_import import SnapshotImporter
from lab_assistant.application.archive_policy import PolicyCheckpointBuilder, PolicyService
from lab_assistant.application.archive_publish import PublicationService
from lab_assistant.application.chat import ChatService
from lab_assistant.application.refresh import RefreshService
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.contracts.archive import SnapshotManifest
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.policy import Effect, PolicyAction, ResourceType, SubjectType
from lab_assistant.domain.installation import InstallationRole
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import publish_archive_fixture, replicating_rights_policy
from lab_assistant.fixtures.archive import MEASURED, RESTRICTED
from lab_assistant.testing import build_harness, make_actor

READER_GRANTS = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
    PolicyAction.INDEX,
)


class ReadOnly:
    def __init__(self, inner) -> None:
        self._inner = inner

    async def read(self, key):
        return await self._inner.read(key)

    async def create(self, key, data):
        raise AssertionError(f"a reader wrote {key}")

    async def compare_and_swap(self, key, data, *, expected_version):
        raise AssertionError(f"a reader swapped {key}")


async def _member(harness, *, email, slug="rla-reader"):
    user = await harness.accounts.create_user(email=email, password="correct horse battery")
    workspace, _ = await harness.accounts.create_workspace(
        slug=slug, display_name="RLA Lab", owner_user_id=user.id
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
            policy_basis="reader_import_test",
        )
        for action in READER_GRANTS
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    return user, workspace, actor


def _forbid(*_args, **_kwargs):
    raise AssertionError("the reader recomputed something the snapshot carried")


@pytest.fixture(params=["memory", "sqlite"])
async def world(request, tmp_path):
    """The reader runs on the in-memory adapters and on SQLite with foreign keys
    enforced: the first would pass an import that wrote a row before the row it
    references, and the second would not."""
    private, public = generate_keypair()
    signer = Ed25519Signer(private, key_identifier="rla-2026")
    verifier = Ed25519Verifier({"rla-2026": public})

    producer = build_harness(rights_policy=replicating_rights_policy())
    _user, producer_ws, producer_actor = await _member(
        producer, email="steward@lab.invalid", slug="rla-lab"
    )
    fixture = await publish_archive_fixture(producer, producer_actor)

    control = FilesystemControlStore(tmp_path / "archive")
    library_id, installation_id = uuid4(), uuid4()
    counter = iter(range(100))

    def policy_service():
        return PolicyService(
            producer.context,
            control=control,
            builder=PolicyCheckpointBuilder(
                producer.context,
                library_id=library_id,
                installation_id=installation_id,
                signer=signer,
            ),
            library_id=library_id,
            installation_id=installation_id,
        )

    async def publish():
        assert (await policy_service().issue(producer_ws.id)).ok
        head, _ = await read_policy_head(control, library_id)
        directory = tmp_path / f"package-{next(counter)}"
        build = await ArchiveCatalogBuilder(
            producer.context,
            library_id=library_id,
            installation_id=installation_id,
            signer=signer,
        ).build(
            producer_ws.id,
            directory,
            policy_sequence=head.policy_sequence,
            policy_artifact_digest=head.checkpoint_digest,
        )
        assert build.ok, build.problems
        report = await PublicationService(
            producer.context,
            control=control,
            library_id=library_id,
            installation_id=installation_id,
            verifier=verifier,
        ).publish(producer_ws.id, directory)
        assert report.ok, report.problems
        return build

    # The reader: its own everything, and a document embedder and extractor
    # that fail if anything asks them to work.
    if request.param == "sqlite":
        from lab_assistant.adapters.db.engine import create_schema, make_engine
        from lab_assistant.adapters.db.repositories import sql_uow_factory
        from lab_assistant.adapters.db.search import PostgresSearchIndex
        from lab_assistant.adapters.object_store.filesystem import FilesystemObjectStore
        from lab_assistant.config import LabEnv, Settings

        settings = Settings(
            env=LabEnv.LOCAL,
            database_url="sqlite+aiosqlite:///:memory:",
            object_store_root=tmp_path / "reader-objects",
        )
        engine = make_engine(settings)
        await create_schema(engine, dimensions=(64,))
        reader = build_harness(
            installation_role=InstallationRole.READER,
            uow_factory=sql_uow_factory(engine),
            object_store=FilesystemObjectStore(settings.object_store_root),
            search_index=PostgresSearchIndex(engine),
        )
    else:
        reader = build_harness(installation_role=InstallationRole.READER)
    reader.embedder.embed_documents = _forbid
    reader.context.extractor.extract = _forbid
    _ruser, reader_ws, reader_actor = await _member(reader, email="researcher@lab.invalid")

    async def fetch():
        destination = tmp_path / f"reader-{next(counter)}"
        report = await SnapshotFetcher(
            ReadOnly(control), library_id=library_id, verifier=verifier
        ).fetch(destination, now=reader.context.now())
        assert report.ok, (report.problems, report.validation.blocking)
        return destination

    def importer(group_map=None):
        return SnapshotImporter(
            reader.context,
            library_id=library_id,
            verifier=verifier,
            group_map=group_map,
        )

    return SimpleNamespace(
        producer=producer,
        producer_ws=producer_ws,
        producer_actor=producer_actor,
        fixture=fixture,
        control=control,
        library_id=library_id,
        publish=publish,
        policy_service=policy_service,
        reader=reader,
        reader_ws=reader_ws,
        reader_actor=reader_actor,
        fetch=fetch,
        importer=importer,
    )


class TestTheSuccessDefinition:
    async def test_an_empty_reader_imports_and_searches_asks_and_cites(self, world) -> None:
        await world.publish()
        directory = await world.fetch()

        report = await world.importer().import_snapshot(world.reader_ws.id, directory)
        assert report.ok, report.problems
        assert report.imported and not report.lexical_only
        assert report.counts["projection_vector"] > 0

        retrieval = RetrievalService(world.reader.context)
        found = await retrieval.retrieve(world.reader_actor, query="lipid yield engineered strain")
        assert found.evidence, "the reader found nothing in an imported corpus"
        # Titles come from the snapshot: the reader has no source assertions.
        assert MEASURED in {block.work_title for block in found.evidence}

        chat = ChatService(world.reader.context)
        session = await chat.create_session(world.reader_actor)
        answer = await chat.ask(
            world.reader_actor,
            session_id=session.id,
            question="What lipid yield was reported for the engineered strain?",
        )
        assert answer.message.citations
        assert answer.message.citation_validation_passed
        resolved = await chat.resolve_citation(
            world.reader_actor,
            message_id=answer.message.id,
            handle=answer.message.citations[0].handle,
        )
        assert resolved["resolved"], resolved

    async def test_importing_the_same_snapshot_again_changes_nothing(self, world) -> None:
        await world.publish()
        directory = await world.fetch()
        assert (await world.importer().import_snapshot(world.reader_ws.id, directory)).imported
        again = await world.importer().import_snapshot(world.reader_ws.id, directory)
        assert again.ok and again.unchanged

    async def test_a_restriction_to_an_unmapped_group_admits_nobody(self, world) -> None:
        """S22: the producer's group id means nothing here until it is mapped."""
        await world.publish()
        directory = await world.fetch()
        assert (await world.importer().import_snapshot(world.reader_ws.id, directory)).ok

        found = await RetrievalService(world.reader.context).retrieve(
            world.reader_actor, query="collaborator measurements embargo"
        )
        assert RESTRICTED not in {block.work_title for block in found.evidence}
        async with world.reader.context.uow_factory() as uow:
            decisions = await uow.policies.list_rights_decisions(world.reader_ws.id)
        restricted = [d for d in decisions if d.restricted_to_group_ids]
        assert restricted
        assert restricted[0].restricted_to_group_ids == (world.fixture.restricted_group_id,)

    async def test_a_mapped_group_carries_the_restriction_to_a_local_group(self, world) -> None:
        await world.publish()
        directory = await world.fetch()
        local = await world.reader.accounts.create_group(
            world.reader_actor,
            slug="collaborators",
            display_name="Collaborators",
            member_user_ids=frozenset({world.reader_actor.user_id}),
        )
        if local.id == world.fixture.restricted_group_id:
            # Both harnesses count ids from one, so the two installations can
            # mint the same id; a mapping between equal ids would prove nothing.
            local = await world.reader.accounts.create_group(
                world.reader_actor,
                slug="collaborators-2",
                display_name="Collaborators",
                member_user_ids=frozenset({world.reader_actor.user_id}),
            )
        assert local.id != world.fixture.restricted_group_id
        report = await world.importer(
            {world.fixture.restricted_group_id: local.id}
        ).import_snapshot(world.reader_ws.id, directory)
        assert report.ok
        async with world.reader.context.uow_factory() as uow:
            decisions = await uow.policies.list_rights_decisions(world.reader_ws.id)
        assert any(d.restricted_to_group_ids == (local.id,) for d in decisions)


class TestNeverBackwards:
    async def test_an_older_head_is_refused_once_a_newer_one_is_served(self, world) -> None:
        """S65, on the reader: its own record of where it is."""
        await world.publish()
        older = await world.fetch()
        await world.publish()
        newer = await world.fetch()

        assert (await world.importer().import_snapshot(world.reader_ws.id, newer)).imported
        replay = await world.importer().import_snapshot(world.reader_ws.id, older)
        assert not replay.ok
        assert "replayed or rolled-back head" in replay.problems[0]

    async def test_a_revocation_reaches_the_reader_without_a_new_snapshot(self, world) -> None:
        """The policy stream, end to end: revoke, re-issue, refetch, and the
        reader's rights rows are the narrowed decision."""
        await world.publish()
        assert (await world.importer().import_snapshot(world.reader_ws.id, await world.fetch())).ok

        async with world.producer.context.uow_factory() as uow:
            asset = (
                await uow.assets.list_assets(
                    world.producer_ws.id, work_id=world.fixture.work_ids[MEASURED]
                )
            )[0]
        await RefreshService(world.producer.context).revoke_rights(
            world.producer_actor, rights_decision_id=asset.rights_decision_id
        )
        assert (await world.policy_service().issue(world.producer_ws.id)).ok

        refreshed = await world.importer().import_snapshot(world.reader_ws.id, await world.fetch())
        assert refreshed.ok and refreshed.imported, refreshed.problems
        async with world.reader.context.uow_factory() as uow:
            decision = await uow.policies.get_rights_decision(
                world.reader_ws.id, asset.rights_decision_id
            )
        assert decision.status.value == "prohibited"
        assert not decision.allowed_actions

        found = await RetrievalService(world.reader.context).retrieve(
            world.reader_actor, query="lipid yield engineered strain"
        )
        assert MEASURED not in {block.work_title for block in found.evidence}


class TestCheckedAgain:
    async def test_a_span_that_does_not_verify_refuses_the_import(self, world) -> None:
        """A span row means "this text was there", and the reader checks."""
        await world.publish()
        directory = await world.fetch()
        spans_path = directory / "catalog" / "span.jsonl"
        rows = [json.loads(line) for line in spans_path.read_bytes().splitlines() if line.strip()]
        rows[0]["start_offset"] += 1
        serialised = [canonical_json(row) + b"\n" for row in rows]
        spans_path.write_bytes(b"".join(serialised))
        manifest_path = directory / MANIFEST_NAME
        manifest = SnapshotManifest.model_validate_json(manifest_path.read_bytes())
        shards = tuple(
            shard.model_copy(
                update={
                    "sha256": sha256_bytes(b"".join(serialised)),
                    "byte_length": sum(len(row) for row in serialised),
                }
            )
            if shard.kind == "span"
            else shard
            for shard in manifest.catalog_shards
        )
        resealed = manifest.model_copy(
            update={"catalog_shards": shards, "signature": None, "key_identifier": None}
        )
        payload = canonical_json(resealed.model_dump(mode="python"))
        manifest_path.write_bytes(payload)
        (directory / "MANIFEST.sha256").write_text(f"{sha256_bytes(payload)}  {MANIFEST_NAME}\n")

        report = await SnapshotImporter(
            world.reader.context, library_id=world.library_id
        ).import_snapshot(world.reader_ws.id, directory)
        assert not report.ok
        assert any("does not verify" in problem for problem in report.problems)
        async with world.reader.context.uow_factory() as uow:
            assert await uow.releases.get_alias(world.reader_ws.id, "current") is None
