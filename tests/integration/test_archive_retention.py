"""Reachability-based purge, planned and reviewed before anything is deleted.

The property that matters most is the negative one: a purge never removes
anything a retained snapshot, a held object or a cited release still needs.
The positive half — litter and unreferenced payloads actually go — is checked
too, and the strongest check of all is the last step of the apply test: the
snapshot the head names still fetches and validates, byte for byte, after the
purge.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab_assistant.adapters.object_store.control import FilesystemControlStore
from lab_assistant.application.archive_catalog import ArchiveCatalogBuilder
from lab_assistant.application.archive_control import library_prefix
from lab_assistant.application.archive_fetch import SnapshotFetcher
from lab_assistant.application.archive_policy import PolicyCheckpointBuilder, PolicyService
from lab_assistant.application.archive_publish import PublicationService
from lab_assistant.application.archive_retention import RetentionPlanner
from lab_assistant.contracts.archive import content_location
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.policy import Effect, PolicyAction, ResourceType, SubjectType
from lab_assistant.contracts.publication import PublicationState
from lab_assistant.domain.chat import ChatMessage, ChatSession, MessageRole
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import publish_archive_fixture, replicating_rights_policy
from lab_assistant.fixtures.archive import RESTRICTED
from lab_assistant.testing import build_harness, make_actor


@pytest.fixture
async def archive(tmp_path):
    harness = build_harness(rights_policy=replicating_rights_policy())
    user = await harness.accounts.create_user(
        email="s@lab.invalid", password="correct horse battery"
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
            policy_basis="retention_test",
        )
        for action in (PolicyAction.QUERY, PolicyAction.VIEW_CONTENT, PolicyAction.INDEX)
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    fixture = await publish_archive_fixture(harness, actor)
    control = FilesystemControlStore(tmp_path / "archive")
    library_id, installation_id = uuid4(), uuid4()
    counter = iter(range(100))

    async def publish(*, release_ids=None):
        directory = tmp_path / f"snapshot-{next(counter)}"
        build = await ArchiveCatalogBuilder(
            harness.context, library_id=library_id, installation_id=installation_id
        ).build(workspace.id, directory, release_ids=release_ids)
        assert build.ok, build.problems
        report = await PublicationService(
            harness.context,
            control=control,
            library_id=library_id,
            installation_id=installation_id,
        ).publish(workspace.id, directory)
        assert report.ok, report.problems
        return build

    async def narrow_replication(title):
        """A steward withdraws replication from one paper, so later snapshots
        withhold its bytes and only older ones name them."""
        async with harness.context.uow_factory() as uow:
            asset = (await uow.assets.list_assets(workspace.id, work_id=fixture.work_ids[title]))[0]
            decision = await uow.policies.get_rights_decision(
                workspace.id, asset.rights_decision_id
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
        return asset

    def planner():
        return RetentionPlanner(harness.context, control=control, library_id=library_id)

    return SimpleNamespace(
        harness=harness,
        workspace=workspace,
        user=user,
        fixture=fixture,
        control=control,
        archive=tmp_path / "archive",
        library_id=library_id,
        installation_id=installation_id,
        publish=publish,
        narrow_replication=narrow_replication,
        planner=planner,
        tmp_path=tmp_path,
    )


def _keys(plan) -> set[str]:
    return {key for key, _ in plan.deletions}


def _content_key(archive, sha256) -> str:
    return f"{library_prefix(archive.library_id)}{content_location(sha256)}"


class TestPlanning:
    async def test_shared_payloads_survive_dropping_an_older_snapshot(self, archive) -> None:
        """S40: the older snapshot goes; the bytes it shares with the head stay."""
        older = await archive.publish()
        await archive.publish()
        plan = await archive.planner().plan(archive.workspace.id, keep_last=1)

        prefix = f"{library_prefix(archive.library_id)}snapshots/{older.snapshot_id}/"
        assert f"{prefix}manifest.json" in _keys(plan)
        assert not any("/content/" in key for key in _keys(plan))
        # Manifests go last, so an interrupted purge leaves each surviving
        # manifest able to name what it lost.
        assert plan.deletions[-1][0].endswith("manifest.json")

    async def test_bytes_only_an_old_snapshot_named_are_deleted(self, archive) -> None:
        await archive.publish()
        asset = await archive.narrow_replication(RESTRICTED)
        await archive.publish()
        plan = await archive.planner().plan(archive.workspace.id, keep_last=1)
        assert _content_key(archive, asset.sha256) in _keys(plan)

    async def test_a_held_snapshot_keeps_its_bytes(self, archive) -> None:
        older = await archive.publish()
        asset = await archive.narrow_replication(RESTRICTED)
        await archive.publish()
        plan = await archive.planner().plan(
            archive.workspace.id, keep_last=1, held_snapshots=(older.snapshot_id,)
        )
        assert "held by an operator" in plan.retained[str(older.snapshot_id)]
        assert _content_key(archive, asset.sha256) not in _keys(plan)

    async def test_a_cited_release_keeps_the_snapshot_that_holds_it(self, archive) -> None:
        """S18: an old conversation's citation must still open."""
        first = archive.fixture.first_release_id
        only_first = await archive.publish(release_ids=(first,))
        await archive.publish(release_ids=(archive.fixture.second_release_id,))

        plan = await archive.planner().plan(archive.workspace.id, keep_last=1)
        assert str(only_first.snapshot_id) not in plan.retained

        async with archive.harness.context.uow_factory() as uow:
            session = ChatSession(
                id=uuid4(), workspace_id=archive.workspace.id, user_id=archive.user.id
            )
            await uow.chat.add_session(session)
            await uow.chat.add_message(
                ChatMessage(
                    id=uuid4(),
                    workspace_id=archive.workspace.id,
                    session_id=session.id,
                    role=MessageRole.ASSISTANT,
                    content="an answer from last quarter",
                    release_id=first,
                )
            )
            await uow.commit()

        plan = await archive.planner().plan(archive.workspace.id, keep_last=1)
        assert any("cited" in reason for reason in plan.retained[str(only_first.snapshot_id)])

    async def test_a_tombstoned_payload_still_named_says_what_keeps_it(self, archive) -> None:
        """S41: kept, and the plan says why and what would release it."""
        await archive.publish()
        async with archive.harness.context.uow_factory() as uow:
            asset = (
                await uow.assets.list_assets(
                    archive.workspace.id, work_id=archive.fixture.work_ids[RESTRICTED]
                )
            )[0]
            await uow.objects.tombstone(asset.object_id, reason="takedown_request")
            await uow.commit()
        assert (
            await PolicyService(
                archive.harness.context,
                control=archive.control,
                builder=PolicyCheckpointBuilder(
                    archive.harness.context,
                    library_id=archive.library_id,
                    installation_id=archive.installation_id,
                ),
                library_id=archive.library_id,
                installation_id=archive.installation_id,
            ).issue(archive.workspace.id)
        ).ok

        plan = await archive.planner().plan(archive.workspace.id, keep_last=3)
        assert _content_key(archive, asset.sha256) not in _keys(plan)
        assert any("tombstoned and retained" in reason for reason in plan.blocked)

    async def test_an_abandoned_upload_is_litter(self, archive) -> None:
        await archive.publish()
        directory = archive.tmp_path / "abandoned"
        build = await ArchiveCatalogBuilder(
            archive.harness.context,
            library_id=archive.library_id,
            installation_id=archive.installation_id,
        ).build(archive.workspace.id, directory)
        service = PublicationService(
            archive.harness.context,
            control=archive.control,
            library_id=archive.library_id,
            installation_id=archive.installation_id,
        )
        head, version = await service.read_head()
        from lab_assistant.domain.publication import evaluate_commit

        verdict = evaluate_commit(
            head,
            library_id=archive.library_id,
            owner_installation_id=archive.installation_id,
            writer_epoch=0,
            snapshot_id=build.snapshot_id,
        )
        record = await service._prepare(archive.workspace.id, build.manifest, verdict, version)
        record, _, _ = await service._upload(record, directory, build.manifest)
        await service._save(record.model_copy(update={"state": PublicationState.FAILED}))

        plan = await archive.planner().plan(archive.workspace.id, keep_last=3)
        abandoned = [
            reason for key, reason in plan.deletions if f"snapshots/{build.snapshot_id}/" in key
        ]
        assert abandoned and set(abandoned) == {"an abandoned upload no head ever named"}


class TestApplying:
    async def test_apply_deletes_the_plan_and_the_head_still_fetches(self, archive) -> None:
        older = await archive.publish()
        asset = await archive.narrow_replication(RESTRICTED)
        await archive.publish()
        plan = await archive.planner().plan(archive.workspace.id, keep_last=1)

        result = await archive.planner().apply(
            archive.workspace.id, plan.as_dict(), deletion=archive.control
        )
        assert result["deleted"] == len(plan.deletions) > 0
        root = archive.archive / library_prefix(archive.library_id)
        assert not (root / "snapshots" / str(older.snapshot_id) / "manifest.json").exists()
        assert not (root / content_location(asset.sha256)).exists()

        fetched = await SnapshotFetcher(archive.control, library_id=archive.library_id).fetch(
            archive.tmp_path / "reader", now=archive.harness.context.now()
        )
        assert fetched.ok, (fetched.problems, fetched.validation.blocking)

    async def test_a_plan_the_archive_has_moved_past_is_refused(self, archive) -> None:
        await archive.publish()
        await archive.publish()
        plan = await archive.planner().plan(archive.workspace.id, keep_last=1)
        await archive.publish()

        with pytest.raises(LabAssistantError) as raised:
            await archive.planner().apply(
                archive.workspace.id, plan.as_dict(), deletion=archive.control
            )
        assert raised.value.error_class is ErrorClass.CONFLICT
        for key, _ in plan.deletions:
            assert (archive.archive / key).exists(), key
