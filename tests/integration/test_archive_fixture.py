"""The archive fixture contains what it claims, and the restore check sees it.

Two halves. The first asserts each of the six cases is really present, because a
fixture that quietly stopped producing one would let the Phase 2 archive work
pass tests that no longer cover anything. The second is the Phase 0 acceptance
gate: the restore check catches missing original bytes, a missing rendition,
missing claims and config, a missing projection, and corruption past the
thousandth release.

That last one is the reason the gate was written. The check used to read the
newest thousand releases; a corpus with more than that could be corrupt in the
tail and verify clean.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from lab_assistant.application.storage_inventory import (
    FindingKind,
    PayloadCheck,
    StorageInventoryService,
)
from lab_assistant.cli.main import _verify_restore
from lab_assistant.contracts.errors import AbstentionReason
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.release import CorpusRelease
from lab_assistant.domain.states import ReleaseState
from lab_assistant.domain.workspace import Role
from lab_assistant.fixtures import ARCHIVE_FIXTURE_CASES, publish_archive_fixture
from lab_assistant.fixtures.archive import MEASURED, RESTRICTED, REVISED, SILENT
from lab_assistant.testing import build_harness, make_actor

GRANTED = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)


@pytest.fixture
async def archive():
    harness = build_harness()
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
            policy_basis="archive_fixture",
        )
        for action in GRANTED
    ]
    actor = make_actor(
        user_id=user.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    fixture = await publish_archive_fixture(harness, actor)
    container = _container(harness)
    return {
        "harness": harness,
        "actor": actor,
        "fixture": fixture,
        "container": container,
    }


def _container(harness):
    from types import SimpleNamespace

    return SimpleNamespace(context=harness.context, shutdown=())


class TestTheFixtureContainsItsCases:
    def test_every_case_is_named(self) -> None:
        """If a case is dropped, the name goes with it and this fails."""
        assert set(ARCHIVE_FIXTURE_CASES) == {
            "two_releases",
            "revised_paper",
            "restricted_evidence",
            "review_history",
            "empty_extraction",
            "compatible_vectors",
        }

    async def test_two_releases_and_the_earlier_one_is_still_servable(self, archive) -> None:
        """An old citation resolves into the release it was minted against."""
        fixture, harness = archive["fixture"], archive["harness"]
        assert fixture.first_release_id != fixture.second_release_id
        assert fixture.served_release_id == fixture.second_release_id
        async with harness.context.uow_factory() as uow:
            first = await uow.releases.get(fixture.workspace_id, fixture.first_release_id)
            second = await uow.releases.get(fixture.workspace_id, fixture.second_release_id)
        assert first is not None and first.status.is_servable
        assert second is not None and second.status is ReleaseState.PUBLISHED
        assert second.parent_release_id == fixture.first_release_id

    async def test_the_revised_paper_changes_digest_and_only_it_does(self, archive) -> None:
        fixture = archive["fixture"]
        before, after = fixture.rendition_digests[REVISED]
        assert before != after, "the correction should produce a new rendition"
        for title in (MEASURED, SILENT, RESTRICTED):
            unchanged = fixture.rendition_digests[title]
            assert unchanged[0] == unchanged[1], f"{title} should not have been revised"

    async def test_the_earlier_rendition_survives_the_revision(self, archive) -> None:
        """A correction is a successor. It does not retarget an old citation."""
        fixture, harness = archive["fixture"], archive["harness"]
        original = fixture.digest_for(REVISED, release_index=0)
        payload = await harness.object_store.get_by_digest(
            original, workspace_id=fixture.workspace_id
        )
        assert payload is not None, "the superseded rendition's bytes must still resolve"

    async def test_restricted_evidence_names_a_group(self, archive) -> None:
        fixture, harness = archive["fixture"], archive["harness"]
        restricted_work = fixture.work_ids[RESTRICTED]
        async with harness.context.uow_factory() as uow:
            assets = await uow.assets.list_assets(fixture.workspace_id, work_id=restricted_work)
            decisions = [
                await uow.policies.get_rights_decision(
                    fixture.workspace_id, asset.rights_decision_id
                )
                for asset in assets
                if asset.rights_decision_id
            ]
        assert decisions
        assert any(
            fixture.restricted_group_id in (decision.restricted_to_group_ids or ())
            for decision in decisions
            if decision is not None
        )

    async def test_the_review_history_has_an_acceptance_and_a_correction(
        self, archive
    ) -> None:
        fixture, harness = archive["fixture"], archive["harness"]
        assert fixture.accepted_revision_ids
        assert fixture.corrected_revision_ids
        async with harness.context.uow_factory() as uow:
            corrected = await uow.claims.get_revision(
                fixture.workspace_id, fixture.corrected_revision_ids[0]
            )
        assert corrected is not None
        # The revision the reviewer replaced is still part of the record.
        assert corrected.revision_number >= 2

    async def test_the_silent_paper_searched_and_reported_nothing(self, archive) -> None:
        """Not an absence of rows: a positive statement, for every target."""
        fixture, harness = archive["fixture"], archive["harness"]
        assert fixture.empty_extraction_work_id == fixture.work_ids[SILENT]
        async with harness.context.uow_factory() as uow:
            claims = await uow.claims.list_claims(
                fixture.workspace_id, work_id=fixture.empty_extraction_work_id
            )
            assert claims
            for claim in claims:
                revisions = await uow.claims.list_revisions(fixture.workspace_id, claim.id)
                current = next(
                    (r for r in revisions if r.id == claim.current_revision_id), revisions[-1]
                )
                assert (
                    current.abstention_reason
                    is AbstentionReason.NOT_REPORTED_AFTER_COMPLETE_SEARCH
                )

    async def test_both_releases_carry_projections_under_a_named_profile(
        self, archive
    ) -> None:
        """An importer has to be testable for reusing these, not re-embedding."""
        fixture, harness = archive["fixture"], archive["harness"]
        for release_id in fixture.release_ids:
            chunks = await harness.search.count_chunks(fixture.workspace_id, release_id)
            assert chunks, f"release {release_id} has no projection to reuse"
        async with harness.context.uow_factory() as uow:
            second = await uow.releases.get(fixture.workspace_id, fixture.second_release_id)
        assert second is not None
        assert fixture.embedding_profile_id in second.projection_ready_profiles

    async def test_the_fixture_verifies_clean(self, archive, capsys) -> None:
        """The baseline: every failure test below starts from a clean sweep."""
        code = await _verify_restore(archive["container"], archive["fixture"].workspace_id)
        capsys.readouterr()
        assert code == 0


class TestTheRestoreCheckCatchesEachLoss:
    """The Phase 0 acceptance gate, one loss at a time."""

    async def test_missing_original_bytes(self, archive, capsys) -> None:
        harness, fixture = archive["harness"], archive["fixture"]
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(fixture.workspace_id, limit=500)
        asset = next(record for record in records if record.object_class == "asset")
        harness.object_store._objects.pop(asset.id)
        harness.object_store._records.pop(asset.id)

        code = await _verify_restore(archive["container"], fixture.workspace_id)
        report = json.loads(capsys.readouterr().out)
        assert code == 1
        assert any(
            finding["kind"] == FindingKind.MISSING_PAYLOAD.value
            for finding in report["storage"]["findings"]
        )

    async def test_missing_rendition(self, archive, capsys) -> None:
        harness, fixture = archive["harness"], archive["fixture"]
        async with harness.context.uow_factory() as uow:
            records = await uow.objects.list_objects(fixture.workspace_id, limit=500)
        rendition = next(record for record in records if record.object_class == "rendition")
        harness.object_store._objects.pop(rendition.id)
        harness.object_store._records.pop(rendition.id)

        code = await _verify_restore(archive["container"], fixture.workspace_id)
        capsys.readouterr()
        assert code == 1

    async def test_missing_claims_and_config(self, archive, capsys) -> None:
        """A release whose claim revisions are gone must not verify."""
        harness, fixture = archive["harness"], archive["fixture"]
        harness.db.revisions.clear()
        harness.db.claims.clear()
        harness.db.config_versions.clear()

        code = await _verify_restore(archive["container"], fixture.workspace_id)
        report = json.loads(capsys.readouterr().out)
        assert code == 1, "a release citing claim revisions that are gone is not servable"
        assert report["problems"]

    async def test_missing_projection(self, archive, capsys) -> None:
        """Every byte can be present and the release still unable to answer."""
        harness, fixture = archive["harness"], archive["fixture"]
        for release_id in fixture.release_ids:
            await harness.search.delete_release_projections(fixture.workspace_id, release_id)

        code = await _verify_restore(archive["container"], fixture.workspace_id)
        report = json.loads(capsys.readouterr().out)
        assert code == 1
        assert any("has no chunks" in problem for problem in report["problems"])
        # The objects are all still there: this is not a storage finding.
        assert report["storage"]["blocking"] == 0

    async def test_corruption_past_the_thousandth_release(self, archive, capsys) -> None:
        """The reason the gate exists: a limit is a page size, not a horizon."""
        harness, fixture = archive["harness"], archive["fixture"]
        broken_id = UUID(int=2_000)
        async with harness.context.uow_factory() as uow:
            for index in range(1, 1_050):
                await uow.releases.add(
                    CorpusRelease(
                        id=UUID(int=index),
                        workspace_id=fixture.workspace_id,
                        name="filler",
                        semantic_version=f"0.0.{index}",
                        status=ReleaseState.DRAFT,
                    )
                )
            # Sorts after every filler row, so a sweep that stops early misses it.
            await uow.releases.add(
                CorpusRelease(
                    id=broken_id,
                    workspace_id=fixture.workspace_id,
                    name="tail",
                    semantic_version="9.9.9",
                    status=ReleaseState.PUBLISHED,
                    release_manifest_object_id=None,
                )
            )
            await uow.commit()

        code = await _verify_restore(archive["container"], fixture.workspace_id)
        report = json.loads(capsys.readouterr().out)
        assert code == 1
        assert any(
            str(broken_id) in problem and "no stored manifest object" in problem
            for problem in report["problems"]
        ), "the corrupt release past the thousandth row was not reached"
        assert report["releases_checked"] >= 3


class TestTheSweepSeesTheWholeFixture:
    async def test_every_object_in_both_releases_is_accounted_for(self, archive) -> None:
        fixture, harness = archive["fixture"], archive["harness"]
        report = await StorageInventoryService(harness.context).sweep(
            fixture.workspace_id, payload_check=PayloadCheck.DIGEST
        )
        assert report.ok
        # Four papers, each with an asset and a rendition, plus the revision's
        # pair, plus two manifests and two validation reports.
        assert report.objects_registered >= 14
        assert report.payloads_checked == report.objects_registered
