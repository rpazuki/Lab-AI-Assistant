"""Signed policy checkpoints, and what they do to a snapshot already published.

A release's science is immutable and permission to see it is not. These tests
are about the gap between the two: a licence revoked after readers imported a
snapshot that granted it, a release withdrawn after it was served, a payload
tombstoned after it was copied.

The property most of them defend is that **a checkpoint narrows and never
widens**. A reader applies the intersection of what the snapshot froze and what
the checkpoint says now, so a stale or replayed checkpoint can be out of date
about a revocation and can never grant more than the publisher signed. Where a
test proves the overlay *withholds* something, a sibling usually proves it does
not withhold too much — identical bytes under two decisions survive revoking
one of them, and a rendition is withheld because its asset is, not because of a
blanket rule that would also withhold its neighbours.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from lab_assistant.adapters.object_store.control import FilesystemControlStore
from lab_assistant.adapters.signing import Ed25519Signer, Ed25519Verifier, generate_keypair
from lab_assistant.application.archive_catalog import (
    INVENTORY_KIND,
    MANIFEST_NAME,
    ArchiveCatalogBuilder,
)
from lab_assistant.application.archive_control import library_prefix, read_policy_head
from lab_assistant.application.archive_policy import PolicyCheckpointBuilder, PolicyService
from lab_assistant.application.archive_publish import PublicationService
from lab_assistant.application.archive_validate import (
    validate_policy_checkpoint,
    validate_snapshot,
)
from lab_assistant.application.refresh import RefreshService
from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
from lab_assistant.contracts.archive import (
    ArtifactClass,
    ArtifactRef,
    SnapshotManifest,
    UnavailableReason,
)
from lab_assistant.contracts.archive_catalog import AssetRecord, RenditionRecord
from lab_assistant.contracts.archive_catalog import RightsDecisionRecord as FrozenDecision
from lab_assistant.contracts.archive_policy import (
    POLICY_HEAD_LOCATION,
    PolicyCheckpoint,
    RightsStanding,
)
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, InstallationDenied, LabAssistantError
from lab_assistant.contracts.policy import Effect, PolicyAction, ResourceType, SubjectType
from lab_assistant.contracts.signing import policy_statement_for, statement_for
from lab_assistant.domain.archive_policy import (
    CheckpointStatus,
    evaluate_checkpoint,
    narrow,
    overlay,
)
from lab_assistant.domain.installation import InstallationRole
from lab_assistant.domain.publication import CommitRefusal
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import publish_archive_fixture
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
            policy_basis="policy_checkpoint_test",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    return workspace, actor


@pytest.fixture
async def library(tmp_path):
    """The fixture corpus, an empty archive, a signing key, and a publisher."""
    private, public = generate_keypair()
    key_path = tmp_path / "signing.key"
    key_path.write_text(private, encoding="utf-8")
    harness = build_harness(
        settings_overrides={
            "object_store_root": tmp_path / "archive",
            "archive_signing_key_path": key_path,
            "archive_signing_key_id": KEY_ID,
            "archive_trusted_keys": {KEY_ID: public},
        }
    )
    workspace, actor = await _provision(harness)
    fixture = await publish_archive_fixture(harness, actor)

    signer = Ed25519Signer(private, key_identifier=KEY_ID)
    verifier = Ed25519Verifier({KEY_ID: public})
    control = FilesystemControlStore(tmp_path / "archive")
    library_id = uuid4()
    installation_id = uuid4()

    def policy(*, epoch: int = 0, installation=None, store=None, public_validity=None):
        builder = PolicyCheckpointBuilder(
            harness.context,
            library_id=library_id,
            installation_id=installation or installation_id,
            writer_epoch=epoch,
            validity=timedelta(hours=24),
            public_validity=public_validity,
            signer=signer,
        )
        return PolicyService(
            harness.context,
            control=store or control,
            builder=builder,
            library_id=library_id,
            installation_id=installation or installation_id,
            writer_epoch=epoch,
        )

    def publisher(*, epoch: int = 0):
        return PublicationService(
            harness.context,
            control=control,
            library_id=library_id,
            installation_id=installation_id,
            writer_epoch=epoch,
            verifier=verifier,
        )

    async def package(name: str, *, release_ids=None, epoch: int = 0):
        """A signed snapshot naming whatever checkpoint the policy head holds now."""
        head, _ = await read_policy_head(control, library_id)
        build = await ArchiveCatalogBuilder(
            harness.context,
            library_id=library_id,
            installation_id=installation_id,
            writer_epoch=epoch,
            signer=signer,
        ).build(
            workspace.id,
            tmp_path / name,
            release_ids=release_ids,
            policy_sequence=head.policy_sequence if head else 0,
            policy_artifact_digest=head.checkpoint_digest if head else None,
        )
        assert build.ok, build.problems
        return tmp_path / name

    return SimpleNamespace(
        harness=harness,
        workspace=workspace,
        actor=actor,
        fixture=fixture,
        control=control,
        archive=tmp_path / "archive",
        library_id=library_id,
        installation_id=installation_id,
        signer=signer,
        verifier=verifier,
        policy=policy,
        publisher=publisher,
        package=package,
        tmp_path=tmp_path,
    )


async def _restricted_asset(library):
    """The asset of the work whose rights decision names a group."""
    async with library.harness.context.uow_factory() as uow:
        assets = await uow.assets.list_assets(
            library.workspace.id, work_id=library.fixture.work_ids[RESTRICTED]
        )
    assert assets and assets[0].rights_decision_id is not None
    return assets[0]


async def _revoke(library, decision_id: UUID) -> None:
    await RefreshService(library.harness.context).revoke_rights(
        library.actor, rights_decision_id=decision_id, reason_code="licence_revoked"
    )


def _checkpoint_file(library, head) -> Path:
    return library.archive / library_prefix(library.library_id) / head.location


class TestIssuing:
    async def test_the_first_checkpoint_covers_the_published_closure(self, library) -> None:
        report = await library.policy().issue(library.workspace.id)
        assert report.ok, report.problems
        assert report.published
        checkpoint = report.checkpoint
        assert checkpoint.policy_sequence == 1
        assert checkpoint.parent_checkpoint_digest is None
        assert checkpoint.is_signed

        # Both releases were published; the first is superseded and still a
        # citation target, so both are standings.
        statuses = {standing.release_id: standing.status for standing in checkpoint.releases}
        assert statuses == {
            library.fixture.first_release_id: "superseded",
            library.fixture.second_release_id: "published",
        }
        # The restricted decision travels with its group.
        restricted = await _restricted_asset(library)
        standing = next(
            item for item in checkpoint.rights if item.decision_id == restricted.rights_decision_id
        )
        assert standing.restricted_to_group_ids == (library.fixture.restricted_group_id,)

        head, _ = await read_policy_head(library.control, library.library_id)
        assert head.policy_sequence == 1
        assert head.checkpoint_digest == checkpoint.signing_digest()

    async def test_a_dry_run_moves_nothing(self, library) -> None:
        report = await library.policy().issue(library.workspace.id, dry_run=True)
        assert report.ok
        assert report.checkpoint is not None
        assert not report.published
        head, _ = await read_policy_head(library.control, library.library_id)
        assert head is None

    async def test_reissuing_unchanged_policy_renews_it_and_says_so(self, library) -> None:
        """The heartbeat: same content, a later window, the next sequence."""
        first = await library.policy().issue(library.workspace.id)
        library.harness.clock.advance(3600)
        second = await library.policy().issue(library.workspace.id)

        assert second.checkpoint.policy_sequence == 2
        assert second.checkpoint.parent_checkpoint_digest == first.checkpoint.signing_digest()
        assert not second.changed
        assert second.checkpoint.content_digest() == first.checkpoint.content_digest()
        assert second.checkpoint.valid_until > first.checkpoint.valid_until

    async def test_a_revocation_is_a_change(self, library) -> None:
        await library.policy().issue(library.workspace.id)
        restricted = await _restricted_asset(library)
        await _revoke(library, restricted.rights_decision_id)

        report = await library.policy().issue(library.workspace.id)
        assert report.changed
        standing = next(
            item
            for item in report.checkpoint.rights
            if item.decision_id == restricted.rights_decision_id
        )
        assert standing.status == "prohibited"
        assert standing.allowed_actions == ()
        assert standing.reason_code == "licence_revoked"

    async def test_a_draft_is_not_disclosed(self, library) -> None:
        """A reader cannot hold a draft, and listing one tells it what the lab is
        working on."""
        draft, _manifest, _diff = await ReleaseService(library.harness.context).draft(
            library.actor,
            ReleaseDraftRequest(
                name="archive-fixture",
                semantic_version="2.0.0",
                parent_release_id=library.fixture.second_release_id,
            ),
        )
        report = await library.policy().issue(library.workspace.id)
        assert draft.id not in {standing.release_id for standing in report.checkpoint.releases}

    async def test_a_withdrawn_release_is_still_a_standing(self, library) -> None:
        """The one a reader most needs to hear about: it may be serving it."""
        await ReleaseService(library.harness.context).withdraw(
            library.actor, release_id=library.fixture.first_release_id
        )
        report = await library.policy().issue(library.workspace.id)
        statuses = {item.release_id: item.status for item in report.checkpoint.releases}
        assert statuses[library.fixture.first_release_id] == "withdrawn"

    async def test_a_reader_installation_issues_no_policy(self, library) -> None:
        reader = build_harness(installation_role=InstallationRole.READER)
        service = PolicyService(
            reader.context,
            control=library.control,
            builder=PolicyCheckpointBuilder(
                reader.context, library_id=library.library_id, installation_id=uuid4()
            ),
            library_id=library.library_id,
            installation_id=uuid4(),
        )
        with pytest.raises(InstallationDenied):
            await service.issue(library.workspace.id)
        head, _ = await read_policy_head(library.control, library.library_id)
        assert head is None

    async def test_a_reader_installation_publishes_no_snapshot(self, library) -> None:
        """The same rule for the catalog head, which was missing it: the owner
        check would have refused a reader eventually, but only at the head, and
        only once the library had one."""
        directory = await library.package("snapshot")
        reader = build_harness(installation_role=InstallationRole.READER)
        service = PublicationService(
            reader.context,
            control=library.control,
            library_id=library.library_id,
            installation_id=uuid4(),
        )
        with pytest.raises(InstallationDenied):
            await service.publish(library.workspace.id, directory)


class TestFencing:
    async def test_a_writer_behind_the_policy_head_is_fenced(self, library) -> None:
        await library.policy(epoch=2).issue(library.workspace.id)
        stale = await library.policy(epoch=1).issue(library.workspace.id)
        assert not stale.ok
        assert stale.verdict.refusal is CommitRefusal.FENCED_BY_EPOCH
        assert any("do NOT re-run" in step for step in stale.next_steps)

    async def test_the_catalog_head_fences_policy_too(self, library) -> None:
        """A handover that moved the catalog head and crashed before the policy
        head has still fenced the old writer from both."""
        directory = await library.package("snapshot", epoch=2)
        published = await library.publisher(epoch=2).publish(library.workspace.id, directory)
        assert published.ok, published.problems

        stale = await library.policy(epoch=1).issue(library.workspace.id)
        assert stale.verdict.refusal is CommitRefusal.FENCED_BY_EPOCH
        assert "catalog head" in stale.verdict.detail
        head, _ = await read_policy_head(library.control, library.library_id)
        assert head is None

    async def test_the_policy_head_fences_snapshot_publication_too(self, library) -> None:
        """And the other way round."""
        await library.policy(epoch=2).issue(library.workspace.id)
        directory = await library.package("snapshot", epoch=1)
        stale = await library.publisher(epoch=1).publish(library.workspace.id, directory)
        assert not stale.ok
        assert stale.verdict.refusal is CommitRefusal.FENCED_BY_EPOCH
        assert "policy head" in stale.verdict.detail

    async def test_another_installation_at_the_same_epoch_is_refused(self, library) -> None:
        await library.policy().issue(library.workspace.id)
        other = await library.policy(installation=uuid4()).issue(library.workspace.id)
        assert other.verdict.refusal is CommitRefusal.FOREIGN_OWNER

    async def test_a_swap_that_loses_the_race_is_not_retried(self, library) -> None:
        """Somebody moved the head between this writer's read and its swap. The
        writer raises and leaves the winner in place, rather than re-reading
        and trying again on top of it."""
        await library.policy().issue(library.workspace.id)
        winner = library.policy()

        class Interleaving:
            def __init__(self, inner) -> None:
                self._inner = inner
                self.won = None

            async def read(self, key):
                return await self._inner.read(key)

            async def create(self, key, data):
                return await self._inner.create(key, data)

            async def compare_and_swap(self, key, data, *, expected_version):
                if self.won is None and key.endswith(POLICY_HEAD_LOCATION):
                    self.won = await winner.issue(library.workspace.id)
                return await self._inner.compare_and_swap(
                    key, data, expected_version=expected_version
                )

        store = Interleaving(library.control)
        loser = library.policy(store=store)
        with pytest.raises(LabAssistantError) as raised:
            await loser.issue(library.workspace.id)
        assert raised.value.error_class is ErrorClass.CONFLICT

        head, _ = await read_policy_head(library.control, library.library_id)
        assert head.checkpoint_digest == store.won.checkpoint.signing_digest()
        assert head.policy_sequence == 2


class TestTrust:
    async def test_an_issued_checkpoint_verifies(self, library) -> None:
        report = await library.policy().issue(library.workspace.id)
        checked = validate_policy_checkpoint(
            _checkpoint_file(library, report.head), verifier=library.verifier
        )
        assert checked.ok, [finding.detail for finding in checked.blocking]
        assert checked.verdict.status is CheckpointStatus.CURRENT

    async def test_restoring_a_revoked_permission_breaks_the_checkpoint(self, library) -> None:
        """The edit an attacker with bucket write would make: put the grant back."""
        restricted = await _restricted_asset(library)
        await _revoke(library, restricted.rights_decision_id)
        report = await library.policy().issue(library.workspace.id)
        path = _checkpoint_file(library, report.head)

        checkpoint = PolicyCheckpoint.model_validate(json.loads(path.read_bytes()))
        restored = tuple(
            item.model_copy(update={"status": "permitted", "allowed_actions": ("view_content",)})
            if item.decision_id == restricted.rights_decision_id
            else item
            for item in checkpoint.rights
        )
        path.write_bytes(checkpoint.model_copy(update={"rights": restored}).serialise())

        checked = validate_policy_checkpoint(path, verifier=library.verifier)
        codes = {finding.code for finding in checked.blocking}
        assert "policy_signature_invalid" in codes
        # Caught twice over: the name is the digest, so the file no longer is
        # the object its name says.
        assert "policy_edited" in codes

    async def test_a_policy_statement_cannot_be_mistaken_for_a_snapshot_statement(
        self, library
    ) -> None:
        """Domain separation: one key signs both kinds of statement.

        What makes that safe is structural, so that is what this asserts: the
        two statements have different key sets, one of them names what it is
        about, and so no choice of values makes their canonical bytes
        coincide. A lifted signature failing is the consequence, checked last
        — on its own it would pass for any two different payloads, and prove
        nothing about separation.
        """
        directory = await library.package("snapshot")
        manifest = SnapshotManifest.model_validate_json((directory / MANIFEST_NAME).read_bytes())
        report = await library.policy().issue(library.workspace.id)

        policy_fields = json.loads(policy_statement_for(report.checkpoint).to_bytes())
        snapshot_fields = json.loads(statement_for(manifest).to_bytes())
        assert policy_fields["statement_type"] == "policy_checkpoint"
        assert "statement_type" not in snapshot_fields
        assert set(policy_fields) != set(snapshot_fields)

        lifted = report.checkpoint.model_copy(update={"signature": manifest.signature})
        assert not library.verifier.verify(
            policy_statement_for(lifted).to_bytes(), manifest.signature, key_identifier=KEY_ID
        )

    async def test_an_unsigned_checkpoint_is_refused_where_signatures_are_required(
        self, library, tmp_path
    ) -> None:
        unsigned = await PolicyCheckpointBuilder(
            library.harness.context,
            library_id=library.library_id,
            installation_id=library.installation_id,
        ).build(library.workspace.id, policy_sequence=1)
        path = tmp_path / "unsigned.json"
        path.write_bytes(unsigned.serialise())

        relaxed = validate_policy_checkpoint(path, verifier=library.verifier)
        assert relaxed.ok
        assert any(f.code == "policy_signature_unsigned" for f in relaxed.findings)
        strict = validate_policy_checkpoint(path, verifier=library.verifier, require_signature=True)
        assert not strict.ok

    async def test_a_field_this_version_cannot_read_is_refused(self, library, tmp_path) -> None:
        """In a stream that withholds, an ignored field is most likely a denial."""
        report = await library.policy().issue(library.workspace.id)
        payload = json.loads(report.checkpoint.serialise())
        payload["withheld_artifacts"] = ["something a newer producer withholds"]
        path = tmp_path / "newer.json"
        path.write_bytes(canonical_json(payload))
        checked = validate_policy_checkpoint(path, verifier=library.verifier)
        assert not checked.ok
        assert checked.blocking[0].code == "policy_unparseable"


def _checkpoint(**changes) -> PolicyCheckpoint:
    issued = datetime(2026, 11, 18, 12, tzinfo=UTC)
    values = {
        "library_id": UUID(int=1),
        "source_workspace_id": UUID(int=2),
        "checkpoint_id": uuid4(),
        "policy_sequence": 3,
        "parent_checkpoint_digest": "a" * 64,
        "origin_installation_id": UUID(int=3),
        "writer_epoch": 0,
        "issued_at": issued,
        "valid_until": issued + timedelta(hours=24),
        "public_valid_until": issued + timedelta(hours=24),
    }
    values.update(changes)
    return PolicyCheckpoint(**values)


class TestRelying:
    def test_a_lower_sequence_than_one_applied_is_a_replay(self) -> None:
        """S65: a replayed head must not roll a reader's policy back."""
        verdict = evaluate_checkpoint(
            _checkpoint(policy_sequence=3),
            library_id=UUID(int=1),
            now=datetime(2026, 11, 18, 13, tzinfo=UTC),
            last_seen_sequence=4,
        )
        assert verdict.status is CheckpointStatus.REPLAYED
        assert verdict.refused

    def test_two_checkpoints_at_one_sequence_are_a_fork(self) -> None:
        checkpoint = _checkpoint(policy_sequence=4)
        verdict = evaluate_checkpoint(
            checkpoint,
            library_id=UUID(int=1),
            now=datetime(2026, 11, 18, 13, tzinfo=UTC),
            last_seen_sequence=4,
            last_seen_digest="b" * 64,
        )
        assert verdict.status is CheckpointStatus.FORKED
        same = evaluate_checkpoint(
            checkpoint,
            library_id=UUID(int=1),
            now=datetime(2026, 11, 18, 13, tzinfo=UTC),
            last_seen_sequence=4,
            last_seen_digest=checkpoint.signing_digest(),
        )
        assert same.status is CheckpointStatus.CURRENT

    def test_older_than_the_snapshot_is_refused(self) -> None:
        verdict = evaluate_checkpoint(
            _checkpoint(policy_sequence=3),
            library_id=UUID(int=1),
            now=datetime(2026, 11, 18, 13, tzinfo=UTC),
            minimum_sequence=5,
        )
        assert verdict.status is CheckpointStatus.BEHIND_SNAPSHOT

    def test_another_librarys_checkpoint_is_refused(self) -> None:
        verdict = evaluate_checkpoint(
            _checkpoint(),
            library_id=UUID(int=99),
            now=datetime(2026, 11, 18, 13, tzinfo=UTC),
        )
        assert verdict.status is CheckpointStatus.FOREIGN_LIBRARY

    def test_restricted_material_lapses_before_an_approved_public_window(self) -> None:
        """S20: restricted serving stops at the bound; public material only
        continues where the publisher approved a longer window."""
        issued = datetime(2026, 11, 18, 12, tzinfo=UTC)
        checkpoint = _checkpoint(
            valid_until=issued + timedelta(hours=24),
            public_valid_until=issued + timedelta(days=7),
        )
        later = evaluate_checkpoint(
            checkpoint, library_id=UUID(int=1), now=issued + timedelta(hours=30)
        )
        assert later.status is CheckpointStatus.RESTRICTED_EXPIRED
        assert later.serves(public=True)
        assert not later.serves(public=False)

        much_later = evaluate_checkpoint(
            checkpoint, library_id=UUID(int=1), now=issued + timedelta(days=8)
        )
        assert much_later.status is CheckpointStatus.EXPIRED
        assert not much_later.serves(public=True)

    def test_without_an_approved_window_public_material_lapses_too(self) -> None:
        issued = datetime(2026, 11, 18, 12, tzinfo=UTC)
        verdict = evaluate_checkpoint(
            _checkpoint(), library_id=UUID(int=1), now=issued + timedelta(hours=25)
        )
        assert verdict.status is CheckpointStatus.EXPIRED
        assert not verdict.serves(public=True)

    @pytest.mark.parametrize(
        "changes",
        [
            {"valid_until": datetime(2026, 11, 18, 11, tzinfo=UTC)},
            {"public_valid_until": datetime(2026, 11, 18, 13, tzinfo=UTC)},
            {"policy_sequence": 3, "parent_checkpoint_digest": None},
            {"policy_sequence": 1, "parent_checkpoint_digest": "a" * 64},
        ],
        ids=["window_inverted", "public_shorter", "orphaned_successor", "first_with_parent"],
    )
    def test_contradictory_content_is_refused(self, changes) -> None:
        verdict = evaluate_checkpoint(
            _checkpoint(**changes),
            library_id=UUID(int=1),
            now=datetime(2026, 11, 18, 12, 30, tzinfo=UTC),
        )
        assert verdict.status is CheckpointStatus.MALFORMED


def _frozen(**changes) -> FrozenDecision:
    values = {
        "library_id": UUID(int=1),
        "record_id": UUID(int=10),
        "subject_type": "asset",
        "subject_id": UUID(int=11),
        "access_basis": "open_access_repository",
        "allowed_actions": ("display_excerpt", "query", "view_content"),
        "restrictions": (),
        "restricted_to_group_ids": (),
        "status": "permitted",
        "review_due_at": datetime(2027, 6, 1, tzinfo=UTC),
    }
    values.update(changes)
    return FrozenDecision(**values)


def _standing(**changes) -> RightsStanding:
    values = {
        "decision_id": UUID(int=10),
        "status": "permitted",
        "allowed_actions": ("display_excerpt", "query", "view_content"),
        "review_due_at": datetime(2027, 6, 1, tzinfo=UTC),
    }
    values.update(changes)
    return RightsStanding(**values)


class TestNarrowing:
    NOW = datetime(2026, 11, 18, tzinfo=UTC)

    def test_a_revocation_withdraws_what_the_snapshot_granted(self) -> None:
        effective = narrow(_frozen(), _standing(status="prohibited", allowed_actions=()))
        assert effective.withheld(now=self.NOW) is UnavailableReason.WITHHELD_BY_RIGHTS
        assert not effective.permits("view_content", now=self.NOW)
        assert "revoked" in effective.narrowed_by

    def test_a_checkpoint_cannot_widen_a_snapshot(self) -> None:
        """An embargo lifting is a publication, not a checkpoint. Every field a
        checkpoint could widen stays where the snapshot left it."""
        group = UUID(int=50)
        frozen = _frozen(
            status="restricted",
            allowed_actions=("query",),
            restrictions=("no external model transfer",),
            restricted_to_group_ids=(group,),
            review_due_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        generous = _standing(
            status="permitted",
            allowed_actions=("query", "view_content", "export_content"),
            restrictions=(),
            restricted_to_group_ids=(),
            review_due_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        effective = narrow(frozen, generous)
        assert effective.status == "restricted"
        assert effective.allowed_actions == frozenset({"query"})
        assert effective.restrictions == ("no external model transfer",)
        assert effective.restricted_to_group_ids == (group,)
        assert effective.review_due_at == datetime(2027, 1, 1, tzinfo=UTC)
        assert effective.narrowed_by == ()

    def test_absent_from_the_checkpoint_is_withheld(self) -> None:
        effective = narrow(_frozen(), None)
        assert effective.withheld(now=self.NOW) is UnavailableReason.WITHHELD_BY_RIGHTS
        assert effective.narrowed_by == ("absent_from_policy",)

    def test_groups_narrow_to_the_ones_both_admit(self) -> None:
        a, b, c = UUID(int=51), UUID(int=52), UUID(int=53)
        both = narrow(
            _frozen(restricted_to_group_ids=(a, b)), _standing(restricted_to_group_ids=(b, c))
        )
        assert both.restricted_to_group_ids == (b,)
        assert "groups_narrowed" in both.narrowed_by

        newly = narrow(_frozen(), _standing(restricted_to_group_ids=(a,)))
        assert newly.restricted_to_group_ids == (a,)
        assert not newly.is_public

    def test_disjoint_groups_admit_nobody(self) -> None:
        effective = narrow(
            _frozen(restricted_to_group_ids=(UUID(int=51),)),
            _standing(restricted_to_group_ids=(UUID(int=52),)),
        )
        assert effective.status == "prohibited"
        assert "groups_disjoint" in effective.narrowed_by

    def test_an_earlier_expiry_is_brought_forward(self) -> None:
        effective = narrow(_frozen(), _standing(review_due_at=datetime(2026, 11, 1, tzinfo=UTC)))
        assert effective.withheld(now=self.NOW) is UnavailableReason.EXPIRED
        assert "expiry_brought_forward" in effective.narrowed_by

    def test_an_unrecognised_status_is_the_strictest(self) -> None:
        """A newer producer's state must not read as a permission to this reader."""
        effective = narrow(_frozen(), _standing(status="embargoed_pending_review"))
        assert effective.status == "prohibited"
        assert any(reason.startswith("unrecognised_status") for reason in effective.narrowed_by)


class TestRetentionEdges:
    async def test_every_payload_says_what_it_depends_on_and_what_governs_it(self, library) -> None:
        directory = await library.package("snapshot")
        inventory = _rows(directory, INVENTORY_KIND)
        assets = {row["record_id"]: row for row in _rows(directory, "asset")}
        renditions = _rows(directory, "rendition")
        assert renditions

        by_artifact = {row["artifact_id"]: row for row in inventory}
        for rendition in renditions:
            source = assets[rendition["asset_id"]]
            ref = by_artifact[rendition["artifact_id"]]
            assert source["artifact_id"] in ref["depends_on"]
            assert source["rights_decision_id"] in ref["governed_by"]
        for asset in assets.values():
            assert asset["rights_decision_id"] in by_artifact[asset["artifact_id"]]["governed_by"]

        assert validate_snapshot(directory, verifier=library.verifier).ok

    async def test_an_inventory_that_drops_an_edge_is_blocking(self, library) -> None:
        """Understating what governs a payload is how a revocation misses bytes."""
        directory = await library.package("snapshot")
        stripped = [
            {**row, "governed_by": []} if row["artifact_class"] == "asset" else row
            for row in _rows(directory, INVENTORY_KIND)
        ]
        _rewrite_shard(directory, INVENTORY_KIND, stripped)
        report = validate_snapshot(directory)
        assert any(f.code == "retention_edge_missing" for f in report.blocking)

    async def test_an_edge_to_nothing_is_blocking(self, library) -> None:
        directory = await library.package("snapshot")
        rows = _rows(directory, INVENTORY_KIND)
        rows[0] = {**rows[0], "depends_on": [str(uuid4())]}
        _rewrite_shard(directory, INVENTORY_KIND, rows)
        report = validate_snapshot(directory)
        assert any(f.code == "retention_dependency_unresolved" for f in report.blocking)


class TestSnapshotUnderPolicy:
    async def test_a_revocation_after_publication_withholds_without_a_new_snapshot(
        self, library
    ) -> None:
        """The case the stream exists for. The snapshot granted it; today's
        checkpoint withholds the asset, its rendition and the passages
        searched out of it — and nothing else."""
        await library.policy().issue(library.workspace.id)
        directory = await library.package("snapshot")
        restricted = await _restricted_asset(library)
        await _revoke(library, restricted.rights_decision_id)
        report = await library.policy().issue(library.workspace.id)

        checked = validate_snapshot(
            directory,
            verifier=library.verifier,
            policy=report.checkpoint,
            now=report.checkpoint.issued_at,
        )
        assert checked.ok, [finding.detail for finding in checked.blocking]
        applied = checked.policy
        assert applied is not None
        assert applied.withheld_assets == {restricted.id: UnavailableReason.WITHHELD_BY_RIGHTS}
        assert set(applied.withheld_renditions.values()) == {UnavailableReason.WITHHELD_BY_RIGHTS}
        assert checked.counts["policy_withheld_chunk"] > 0
        assert any(f.code == "policy_withholds" for f in checked.findings)
        # Narrowed, not erased: the other works are untouched.
        assert len(applied.withheld_assets) == 1

    async def test_rolling_back_to_an_older_release_keeps_todays_revocations(self, library) -> None:
        """S19: a snapshot of release 1 alone, served after the revocation,
        does not bring the revoked permission back."""
        older = await library.package("release-1", release_ids=(library.fixture.first_release_id,))
        restricted = await _restricted_asset(library)
        await _revoke(library, restricted.rights_decision_id)
        report = await library.policy().issue(library.workspace.id)

        checked = validate_snapshot(older, policy=report.checkpoint)
        assert restricted.id in checked.policy.withheld_assets

    async def test_a_checkpoint_older_than_the_snapshot_is_refused(self, library) -> None:
        first = await library.policy().issue(library.workspace.id)
        await library.policy().issue(library.workspace.id)
        directory = await library.package("snapshot")

        checked = validate_snapshot(directory, policy=first.checkpoint)
        assert not checked.ok
        assert any(f.code == "policy_behind_snapshot" for f in checked.blocking)
        assert checked.policy is None

    async def test_a_withdrawn_release_is_reported(self, library) -> None:
        directory = await library.package("snapshot")
        await ReleaseService(library.harness.context).withdraw(
            library.actor, release_id=library.fixture.first_release_id
        )
        report = await library.policy().issue(library.workspace.id)

        checked = validate_snapshot(directory, policy=report.checkpoint)
        assert checked.policy.withdrawn_releases == {library.fixture.first_release_id}
        assert checked.counts["policy_withheld_chunk"] > 0

    async def test_a_tombstone_withholds_the_bytes_and_their_text(self, library) -> None:
        directory = await library.package("snapshot")
        restricted = await _restricted_asset(library)
        async with library.harness.context.uow_factory() as uow:
            await uow.objects.tombstone(restricted.object_id, reason="takedown_request")
            await uow.commit()
        report = await library.policy().issue(library.workspace.id)
        assert [item.sha256 for item in report.checkpoint.tombstones] == [restricted.sha256]

        applied = validate_snapshot(directory, policy=report.checkpoint).policy
        assert applied.withheld_assets[restricted.id] is UnavailableReason.TOMBSTONED
        # Exactly two payloads: the asset's bytes, and its rendition. The
        # rendition's own bytes are not tombstoned; it is withheld because it
        # depends on bytes that are — the retention edge doing its job — and
        # no other work's payload is touched.
        assert set(applied.withheld_artifacts.values()) == {UnavailableReason.TOMBSTONED}
        assert len(applied.withheld_artifacts) == 2

    async def test_a_stale_checkpoint_withholds_restricted_material_only(self, library) -> None:
        directory = await library.package("snapshot")
        report = await library.policy(public_validity=timedelta(days=7)).issue(library.workspace.id)
        later = report.checkpoint.valid_until + timedelta(hours=1)

        checked = validate_snapshot(directory, policy=report.checkpoint, now=later)
        assert checked.ok
        assert any(f.code == "policy_restricted_expired" for f in checked.findings)
        restricted = await _restricted_asset(library)
        assert checked.policy.withheld_assets == {restricted.id: UnavailableReason.EXPIRED}

    async def test_a_snapshot_naming_an_unissued_checkpoint_is_not_published(self, library) -> None:
        """A reader would go looking for policy that is not there."""
        directory = await library.package("snapshot")
        path = directory / MANIFEST_NAME
        manifest = SnapshotManifest.model_validate_json(path.read_bytes())
        bound = manifest.model_copy(
            update={"policy_sequence": 7, "policy_artifact_digest": "c" * 64}
        ).model_copy(update={"signature": None, "key_identifier": None})
        payload = canonical_json(bound.model_dump(mode="python"))
        path.write_bytes(payload)
        (directory / "MANIFEST.sha256").write_text(f"{sha256_bytes(payload)}  {MANIFEST_NAME}\n")

        refused = await library.publisher().publish(library.workspace.id, directory)
        assert refused.verdict.refusal is CommitRefusal.POLICY_UNRESOLVED
        assert any("lab archive policy publish" in step for step in refused.next_steps)


class TestSharedBytes:
    """S14 and S40, where per-record and per-artifact standing differ."""

    NOW = datetime(2026, 11, 18, tzinfo=UTC)

    def _records(self):
        library_id = UUID(int=1)
        d1, d2 = UUID(int=21), UUID(int=22)
        artifact = UUID(int=30)
        rendition_artifact = UUID(int=31)
        sha = "d" * 64
        rights = {
            d: _frozen(record_id=d, subject_id=asset)
            for d, asset in ((d1, UUID(int=41)), (d2, UUID(int=42)))
        }
        assets = {
            UUID(int=41): AssetRecord(
                library_id=library_id,
                record_id=UUID(int=41),
                sha256=sha,
                byte_length=10,
                rights_decision_id=d1,
                artifact_id=artifact,
            ),
            UUID(int=42): AssetRecord(
                library_id=library_id,
                record_id=UUID(int=42),
                sha256=sha,
                byte_length=10,
                rights_decision_id=d2,
                artifact_id=artifact,
            ),
        }
        renditions = {
            UUID(int=43): RenditionRecord(
                library_id=library_id,
                record_id=UUID(int=43),
                asset_id=UUID(int=41),
                sha256="e" * 64,
                format="jats",
                schema_version="1.0",
                artifact_id=rendition_artifact,
            )
        }
        inventory = {
            artifact: ArtifactRef(
                artifact_id=artifact,
                artifact_class=ArtifactClass.ASSET,
                sha256=sha,
                byte_length=10,
                media_type="application/pdf",
                library_id=library_id,
                governed_by=(d1, d2),
            ),
            rendition_artifact: ArtifactRef(
                artifact_id=rendition_artifact,
                artifact_class=ArtifactClass.RENDITION,
                sha256="e" * 64,
                byte_length=0,
                media_type="application/json",
                library_id=library_id,
                depends_on=(artifact,),
                governed_by=(d1,),
            ),
        }
        return rights, assets, renditions, inventory, (d1, d2)

    def _apply(self, standings):
        rights, assets, renditions, inventory, _ = self._records()
        checkpoint = _checkpoint(rights=standings)
        verdict = evaluate_checkpoint(checkpoint, library_id=UUID(int=1), now=self.NOW)
        return overlay(
            checkpoint,
            verdict=verdict,
            now=self.NOW,
            rights=rights,
            assets=assets,
            renditions=renditions,
            inventory=inventory,
        )

    def test_revoking_one_acquisition_leaves_the_other_its_bytes(self) -> None:
        _, _, _, _, (d1, d2) = self._records()
        applied = self._apply(
            (
                _standing(decision_id=d1, status="prohibited", allowed_actions=()),
                _standing(decision_id=d2),
            )
        )
        # The revoked acquisition is withheld, as is the text extracted from it…
        assert set(applied.withheld_assets) == {UUID(int=41)}
        assert set(applied.withheld_renditions) == {UUID(int=43)}
        # …and the shared bytes are not, because another acquisition still
        # lawfully holds them. A purge working from this would keep them.
        assert UUID(int=30) not in applied.withheld_artifacts

    def test_revoking_both_withholds_the_bytes(self) -> None:
        _, _, _, _, (d1, d2) = self._records()
        applied = self._apply(
            (
                _standing(decision_id=d1, status="prohibited", allowed_actions=()),
                _standing(decision_id=d2, status="prohibited", allowed_actions=()),
            )
        )
        assert applied.withheld_artifacts[UUID(int=30)] is UnavailableReason.WITHHELD_BY_RIGHTS
        assert UUID(int=31) in applied.withheld_artifacts

    def test_a_refused_checkpoint_withholds_nothing_either(self) -> None:
        """A checkpoint that must not be relied on is not relied on to withhold:
        the reader keeps what it applied before."""
        rights, assets, renditions, inventory, (d1, _) = self._records()
        checkpoint = _checkpoint(
            rights=(_standing(decision_id=d1, status="prohibited", allowed_actions=()),)
        )
        verdict = evaluate_checkpoint(
            checkpoint, library_id=UUID(int=1), now=self.NOW, last_seen_sequence=9
        )
        applied = overlay(
            checkpoint,
            verdict=verdict,
            now=self.NOW,
            rights=rights,
            assets=assets,
            renditions=renditions,
            inventory=inventory,
        )
        assert verdict.refused
        assert not applied.withheld_assets


class TestCommandLine:
    async def test_package_signs_and_names_the_policy_and_publish_checks_both(
        self, library, capsys
    ) -> None:
        """The configured key is used, the policy head is recorded, and the
        snapshot is verified against the trusted keys on the way out.

        Before this, `LAB_ARCHIVE_SIGNING_KEY_PATH` and
        `LAB_ARCHIVE_TRUSTED_KEYS` were settings nothing read.
        """
        from lab_assistant.cli.main import _archive

        container = SimpleNamespace(context=library.harness.context, shutdown=[])
        settings = library.harness.settings
        workspace = library.workspace

        code = await _archive(
            SimpleNamespace(
                archive_command="policy",
                policy_command="publish",
                workspace=workspace.slug,
                library_id=str(library.library_id),
                apply=True,
            ),
            settings,
            container=container,
        )
        assert code == 0, capsys.readouterr().err
        issued = json.loads(capsys.readouterr().out)
        assert issued["checkpoint"]["signed"]

        snapshot = library.tmp_path / "cli-snapshot"
        code = await _archive(
            SimpleNamespace(
                archive_command="package",
                workspace=workspace.slug,
                directory=snapshot,
                release_id=None,
                library_id=str(library.library_id),
            ),
            settings,
            container=container,
        )
        assert code == 0, capsys.readouterr().err
        capsys.readouterr()
        manifest = SnapshotManifest.model_validate_json((snapshot / MANIFEST_NAME).read_bytes())
        assert manifest.is_signed
        assert manifest.policy_sequence == 1
        assert manifest.policy_artifact_digest == issued["checkpoint"]["digest"]

        code = await _archive(
            SimpleNamespace(
                archive_command="verify",
                directory=snapshot,
                policy=library.archive
                / library_prefix(library.library_id)
                / "policy"
                / f"{1:012d}-{issued['checkpoint']['digest']}.json",
            ),
            settings,
            container=container,
        )
        verified = json.loads(capsys.readouterr().out)
        assert code == 0
        assert not [f for f in verified["findings"] if f["code"].startswith("signature_")]
        assert verified["policy"]["verdict"]["status"] == "current"

        code = await _archive(
            SimpleNamespace(
                archive_command="publish",
                workspace=workspace.slug,
                directory=snapshot,
                library_id=str(library.library_id),
                apply=True,
            ),
            settings,
            container=container,
        )
        assert code == 0, capsys.readouterr().err

    async def test_status_reports_freshness_and_fails_when_stale(self, library, capsys) -> None:
        from lab_assistant.cli.main import _archive

        container = SimpleNamespace(context=library.harness.context, shutdown=[])
        args = SimpleNamespace(
            archive_command="policy",
            policy_command="status",
            workspace=library.workspace.slug,
            library_id=str(library.library_id),
        )
        assert await _archive(args, library.harness.settings, container=container) == 1
        capsys.readouterr()

        await library.policy().issue(library.workspace.id)
        assert await _archive(args, library.harness.settings, container=container) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["verdict"]["status"] == "current"
        assert status["policy_head"]["policy_sequence"] == 1


def _rows(directory, kind: str) -> list[dict]:
    path = directory / "catalog" / f"{kind}.jsonl"
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def _rewrite_shard(directory, kind: str, rows: list[dict]) -> None:
    """Rewrite one shard and reseal the manifest around it, unsigned.

    Resealed so the test fails on the check it is about, not on a digest that
    a tampering attacker would also have recomputed.
    """
    serialised = [canonical_json(row) + b"\n" for row in rows]
    location = f"catalog/{kind}.jsonl"
    (directory / location).write_bytes(b"".join(serialised))
    path = directory / MANIFEST_NAME
    manifest = SnapshotManifest.model_validate_json(path.read_bytes())
    shards = tuple(
        shard.model_copy(
            update={
                "sha256": sha256_bytes(b"".join(serialised)),
                "byte_length": sum(len(row) for row in serialised),
                "row_count": len(serialised),
            }
        )
        if shard.location == location
        else shard
        for shard in manifest.catalog_shards
    )
    resealed = manifest.model_copy(
        update={"catalog_shards": shards, "signature": None, "key_identifier": None}
    )
    payload = canonical_json(resealed.model_dump(mode="python"))
    path.write_bytes(payload)
    (directory / "MANIFEST.sha256").write_text(
        f"{sha256_bytes(payload)}  {MANIFEST_NAME}\n", encoding="utf-8"
    )
