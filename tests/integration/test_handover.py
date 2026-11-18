"""Handing the right to publish from one installation to another (phase 4).

Two installations share nothing here but the archive and — for a successor
restored from the old publisher's backup — a copy of its database, which these
tests model by giving the successor the same repositories under its own
installation id and the next epoch.

The acceptance criteria the offline core can prove:

- the old publisher cannot publish, or issue policy, after the handover;
- a promotion that crashed between the two heads resumes, and the old writer is
  fenced from both in the meantime;
- an old publisher that published after draining blocks the promotion;
- an unreachable old publisher blocks it unless an operator asserts it fenced;
- a successor without the published state is refused unless the loss is accepted;
- every drain and promotion leaves an attributable audit row.

What they cannot prove is the half that is not software: that the old machine's
archive credentials were revoked. That needs a real bucket and real IAM.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab_assistant.adapters.object_store.control import FilesystemControlStore
from lab_assistant.application.archive_catalog import ArchiveCatalogBuilder
from lab_assistant.application.archive_control import read_catalog_head, read_policy_head
from lab_assistant.application.archive_policy import PolicyCheckpointBuilder, PolicyService
from lab_assistant.application.archive_publish import PublicationService
from lab_assistant.application.handover import HandoverService
from lab_assistant.application.readiness import ReadinessService
from lab_assistant.contracts.archive_policy import POLICY_HEAD_LOCATION
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError
from lab_assistant.contracts.events import AuditAction
from lab_assistant.contracts.policy import Effect, PolicyAction, ResourceType, SubjectType
from lab_assistant.contracts.publication import PublicationRecord, PublicationState
from lab_assistant.domain.handover import HandoverRefusal, HeadPlan
from lab_assistant.domain.installation import InstallationRole, InstanceCapabilities
from lab_assistant.domain.publication import CommitRefusal
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import publish_archive_fixture, replicating_rights_policy
from lab_assistant.testing import build_harness, make_actor

GRANTED = (PolicyAction.QUERY, PolicyAction.VIEW_CONTENT, PolicyAction.INDEX)


@pytest.fixture
async def world(tmp_path):
    """A library published at epoch 0 by installation A, and a successor B."""
    a = build_harness(rights_policy=replicating_rights_policy())
    user = await a.accounts.create_user(email="s@lab.invalid", password="correct horse battery")
    workspace, _ = await a.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
    )
    admin = make_actor(user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN)
    grants = [
        await a.accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=action,
            effect=Effect.ALLOW,
            policy_basis="handover_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    await publish_archive_fixture(a, actor)

    control = FilesystemControlStore(tmp_path / "archive")
    library_id = uuid4()
    a_id, b_id = uuid4(), uuid4()

    def restarted(harness, role):
        """The same database under another role, as after a restart."""
        return build_harness(
            rights_policy=replicating_rights_policy(),
            uow_factory=harness.context.uow_factory,
            object_store=harness.object_store,
            search_index=harness.search,
            installation_role=role,
        )

    def publisher(context, installation_id, epoch):
        return PublicationService(
            context,
            control=control,
            library_id=library_id,
            installation_id=installation_id,
            writer_epoch=epoch,
        )

    def policy(context, installation_id, epoch):
        return PolicyService(
            context,
            control=control,
            builder=PolicyCheckpointBuilder(
                context,
                library_id=library_id,
                installation_id=installation_id,
                writer_epoch=epoch,
            ),
            library_id=library_id,
            installation_id=installation_id,
            writer_epoch=epoch,
        )

    def handover(context, installation_id, epoch, store=None):
        return HandoverService(
            context,
            control=store or control,
            library_id=library_id,
            installation_id=installation_id,
            writer_epoch=epoch,
        )

    async def publish(context, installation_id, epoch, name):
        build = await ArchiveCatalogBuilder(
            context, library_id=library_id, installation_id=installation_id, writer_epoch=epoch
        ).build(workspace.id, tmp_path / name)
        assert build.ok, build.problems
        return await publisher(context, installation_id, epoch).publish(
            workspace.id, tmp_path / name
        )

    first = await publish(a.context, a_id, 0, "first")
    assert first.ok, first.problems
    assert (await policy(a.context, a_id, 0).issue(workspace.id)).ok

    maintenance = restarted(a, InstallationRole.MAINTENANCE)
    successor = restarted(a, InstallationRole.PUBLISHER)
    return SimpleNamespace(
        a=a,
        maintenance=maintenance,
        successor=successor,
        workspace=workspace,
        control=control,
        library_id=library_id,
        a_id=a_id,
        b_id=b_id,
        publisher=publisher,
        policy=policy,
        handover=handover,
        publish=publish,
        restarted=restarted,
    )


async def _drain(world):
    report = await world.handover(world.maintenance.context, world.a_id, 0).drain(
        world.workspace.id
    )
    assert report.ok, report.blockers
    return report.statement


class TestDraining:
    async def test_a_publisher_still_serving_may_not_drain(self, world) -> None:
        report = await world.handover(world.a.context, world.a_id, 0).drain(world.workspace.id)
        assert not report.ok
        assert [b.code for b in report.blockers] == ["not_in_maintenance"]

    async def test_a_publication_in_flight_blocks_the_drain(self, world) -> None:
        async with world.a.context.uow_factory() as uow:
            await uow.publications.add(
                PublicationRecord(
                    id=uuid4(),
                    workspace_id=world.workspace.id,
                    library_id=world.library_id,
                    snapshot_id=uuid4(),
                    snapshot_manifest_digest="0" * 64,
                    state=PublicationState.PREPARED,
                    writer_epoch=0,
                    commit_sequence=2,
                    created_at=world.a.context.now(),
                )
            )
            await uow.commit()
        report = await world.handover(world.maintenance.context, world.a_id, 0).drain(
            world.workspace.id
        )
        assert "publication_in_flight" in [b.code for b in report.blockers]

    async def test_an_installation_that_does_not_publish_has_nothing_to_drain(self, world) -> None:
        report = await world.handover(world.maintenance.context, uuid4(), 0).drain(
            world.workspace.id
        )
        assert "not_the_publisher" in [b.code for b in report.blockers]

    async def test_maintenance_serves_nobody_and_claims_nothing(self, world) -> None:
        capabilities = InstanceCapabilities(role=InstallationRole.MAINTENANCE)
        assert capabilities.capabilities == frozenset()
        assert capabilities.refuse_worker_startup() is not None
        readiness = await ReadinessService(world.maintenance.context).check()
        assert not readiness.ready
        assert readiness.http_status == 503
        assert any(check.name == "installation_role" and not check.ok for check in readiness.checks)


class TestPromotion:
    async def test_the_successor_takes_both_heads_and_the_old_writer_is_fenced(self, world) -> None:
        statement = await _drain(world)
        service = world.handover(world.successor.context, world.b_id, 1)

        dry = await service.promote(world.workspace.id, statement=statement)
        assert dry.ok and not dry.applied
        assert (dry.verdict.catalog, dry.verdict.policy) == (HeadPlan.TRANSFER, HeadPlan.TRANSFER)
        catalog, _ = await read_catalog_head(world.control, world.library_id)
        assert catalog.owner_installation_id == world.a_id

        applied = await service.promote(world.workspace.id, statement=statement, dry_run=False)
        assert applied.applied
        catalog, _ = await read_catalog_head(world.control, world.library_id)
        policy, _ = await read_policy_head(world.control, world.library_id)
        for head in (catalog, policy):
            assert head.owner_installation_id == world.b_id
            assert head.writer_epoch == 1
            assert head.fenced_predecessor_id == world.a_id

        # The old publisher, restarted as a publisher by mistake, is refused.
        stale = await world.publish(world.a.context, world.a_id, 0, "stale")
        assert stale.verdict.refusal is CommitRefusal.FENCED_BY_EPOCH
        stale_policy = await world.policy(world.a.context, world.a_id, 0).issue(world.workspace.id)
        assert stale_policy.verdict.refusal is CommitRefusal.FENCED_BY_EPOCH
        assert (await world.handover(world.a.context, world.a_id, 0).status())["fenced"]

        # The successor publishes, both kinds.
        assert (await world.publish(world.successor.context, world.b_id, 1, "next")).ok
        assert (
            await world.policy(world.successor.context, world.b_id, 1).issue(world.workspace.id)
        ).ok

        async with world.a.context.uow_factory() as uow:
            actions = {event.action for event in await uow.audit.list_events(world.workspace.id)}
        assert {AuditAction.INSTALLATION_DRAINED, AuditAction.INSTALLATION_PROMOTED} <= actions

    async def test_a_publisher_that_kept_publishing_after_draining_blocks_it(self, world) -> None:
        statement = await _drain(world)
        # Restarted as a publisher and issued policy again: the drain is stale.
        assert (await world.policy(world.a.context, world.a_id, 0).issue(world.workspace.id)).ok
        report = await world.handover(world.successor.context, world.b_id, 1).promote(
            world.workspace.id, statement=statement, dry_run=False
        )
        assert report.verdict.refusal is HandoverRefusal.HEAD_MOVED_SINCE_DRAIN
        catalog, _ = await read_catalog_head(world.control, world.library_id)
        assert catalog.owner_installation_id == world.a_id

    async def test_the_epoch_moves_by_exactly_one(self, world) -> None:
        statement = await _drain(world)
        report = await world.handover(world.successor.context, world.b_id, 2).promote(
            world.workspace.id, statement=statement
        )
        assert report.verdict.refusal is HandoverRefusal.EPOCH_NOT_NEXT

    async def test_an_unreachable_publisher_must_be_asserted_fenced(self, world) -> None:
        """S53: no statement, no assertion, no promotion."""
        service = world.handover(world.successor.context, world.b_id, 1)
        blocked = await service.promote(world.workspace.id, statement=None, dry_run=False)
        assert blocked.verdict.refusal is HandoverRefusal.NOT_FENCED

        asserted = await service.promote(
            world.workspace.id, statement=None, origin_fenced=True, dry_run=False
        )
        assert asserted.applied
        catalog, _ = await read_catalog_head(world.control, world.library_id)
        assert (catalog.owner_installation_id, catalog.writer_epoch) == (world.b_id, 1)

    async def test_a_successor_without_the_published_state_is_refused(self, world) -> None:
        """Promoting an empty installation would curate over what was published."""
        statement = await _drain(world)
        empty = build_harness()
        user = await empty.accounts.create_user(
            email="b@lab.invalid", password="correct horse battery"
        )
        workspace, _ = await empty.accounts.create_workspace(
            slug="rla-lab", display_name="RLA Lab", owner_user_id=user.id
        )
        service = world.handover(empty.context, world.b_id, 1)
        refused = await service.promote(workspace.id, statement=statement)
        assert refused.verdict.refusal is HandoverRefusal.STATE_NOT_RECOVERED
        accepted = await service.promote(workspace.id, statement=statement, accept_loss=True)
        assert accepted.ok

    async def test_a_promotion_interrupted_between_the_heads_resumes(self, world) -> None:
        """Catalog moved, policy not. The old writer is fenced from both anyway,
        and the same command finishes the job."""
        statement = await _drain(world)

        class CrashOnPolicy:
            def __init__(self, inner) -> None:
                self._inner = inner

            async def read(self, key):
                return await self._inner.read(key)

            async def create(self, key, data):
                return await self._inner.create(key, data)

            async def compare_and_swap(self, key, data, *, expected_version):
                if key.endswith(POLICY_HEAD_LOCATION):
                    raise LabAssistantError("connection reset").with_class(ErrorClass.TIMEOUT)
                return await self._inner.compare_and_swap(
                    key, data, expected_version=expected_version
                )

        crashing = world.handover(
            world.successor.context, world.b_id, 1, store=CrashOnPolicy(world.control)
        )
        with pytest.raises(LabAssistantError):
            await crashing.promote(world.workspace.id, statement=statement, dry_run=False)

        policy, _ = await read_policy_head(world.control, world.library_id)
        assert policy.owner_installation_id == world.a_id
        # Half done, and the old writer may still not issue policy: the catalog
        # head already names a later epoch.
        stale = await world.policy(world.a.context, world.a_id, 0).issue(world.workspace.id)
        assert stale.verdict.refusal is CommitRefusal.FENCED_BY_EPOCH
        assert "catalog head" in stale.verdict.detail

        resumed = await world.handover(world.successor.context, world.b_id, 1).promote(
            world.workspace.id, statement=statement, dry_run=False
        )
        assert (resumed.verdict.catalog, resumed.verdict.policy) == (
            HeadPlan.ALREADY_TRANSFERRED,
            HeadPlan.TRANSFER,
        )
        policy, _ = await read_policy_head(world.control, world.library_id)
        assert (policy.owner_installation_id, policy.writer_epoch) == (world.b_id, 1)

        again = await world.handover(world.successor.context, world.b_id, 1).promote(
            world.workspace.id, statement=statement
        )
        assert again.verdict.complete
