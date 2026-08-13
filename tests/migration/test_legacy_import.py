"""Legacy import tests.

The guarantees under test are the ones that make a migration safe: a legacy row
never becomes truth, PMID/PMCID duplicates resolve through a recorded decision,
contradictions become conflicts, and every run reconciles its counts.
"""

from __future__ import annotations

from typing import Any

import pytest

from lab_assistant.domain.states import IdentityState, RelevanceState, ReviewState
from lab_assistant.domain.workspace import Role
from lab_assistant.migration.legacy_import import (
    AssetClass,
    LegacyImporter,
    classify_asset,
)
from lab_assistant.testing import build_harness, make_actor


@pytest.fixture
async def importer_stack():
    harness = build_harness()
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    actor = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    return harness, actor, LegacyImporter(harness.context)


class TestDocumentImport:
    async def test_pmid_and_pmc_rows_become_one_work(self, importer_stack) -> None:
        """Audit finding H-12: the same PMID stored under two document ids."""
        harness, actor, importer = importer_stack
        report = await importer.import_documents(
            actor,
            documents=[
                {"document_id": "pmid:40000001", "pmid": "40000001", "source": "pubmed"},
                {
                    "document_id": "pmc:PMC9000001",
                    "pmid": "40000001",
                    "pmc_id": "PMC9000001",
                    "source": "pmc",
                },
            ],
        )
        assert report.source_rows == 2
        assert report.works_created == 1
        async with harness.context.uow_factory() as uow:
            works = await uow.works.list_works(actor.workspace_id)
            active = [work for work in works if work.is_active]
            identifiers = await uow.works.list_identifiers(actor.workspace_id, active[0].id)
        assert len(active) == 1
        schemes = {i.scheme.value for i in identifiers}
        assert schemes == {"pmid", "pmcid"}

    async def test_contradictory_identifiers_become_a_conflict(self, importer_stack) -> None:
        harness, actor, importer = importer_stack
        await importer.import_documents(
            actor,
            documents=[
                {"document_id": "a", "doi": "10.1/one", "pmid": "111", "source": "pubmed"},
                {"document_id": "b", "doi": "10.2/two", "pmid": "222", "source": "pubmed"},
            ],
        )
        # A row that claims both works' identifiers is a contradiction, not a merge.
        report = await importer.import_documents(
            actor,
            documents=[{"document_id": "c", "doi": "10.1/one", "pmid": "222", "source": "pubmed"}],
        )
        assert report.identity_conflicts >= 1
        async with harness.context.uow_factory() as uow:
            works = await uow.works.list_works(actor.workspace_id)
            decisions = await uow.works.list_decisions(actor.workspace_id)
        assert any(work.identity_status is IdentityState.CONFLICT for work in works)
        assert any("contradiction" in (d.reason_code or "") for d in decisions)

    async def test_legacy_relevance_is_not_inherited(self, importer_stack) -> None:
        harness, actor, importer = importer_stack
        await importer.import_documents(
            actor,
            documents=[
                {"document_id": "pmid:1", "pmid": "1", "source": "pubmed", "included": True}
            ],
        )
        async with harness.context.uow_factory() as uow:
            works = await uow.works.list_works(actor.workspace_id)
        assert works[0].relevance is RelevanceState.UNREVIEWED

    async def test_rows_without_identifiers_are_reported_not_dropped(
        self, importer_stack
    ) -> None:
        _harness, actor, importer = importer_stack
        report = await importer.import_documents(
            actor, documents=[{"document_id": "", "title": "No identifier at all"}]
        )
        assert report.works_created == 0
        assert report.skipped and "no usable identifier" in report.skipped[0]["reason"]
        assert report.reconciles()

    async def test_every_import_is_mapped_and_audited(self, importer_stack) -> None:
        harness, actor, importer = importer_stack
        report = await importer.import_documents(
            actor, documents=[{"document_id": "doi:10.1/x", "doi": "10.1/x", "source": "crossref"}]
        )
        assert report.mapped == 1
        async with harness.context.uow_factory() as uow:
            events = await uow.audit.list_events(actor.workspace_id, limit=100)
        legacy_events = [e for e in events if e.action.value == "legacy_import_run"]
        assert legacy_events
        assert any(e.metadata.get("legacy_id") == "doi:10.1/x" for e in legacy_events)
        # The run summary is auditable and reconciles.
        summary = next(e for e in legacy_events if e.reason_code == "import_run_complete")
        assert summary.metadata["reconciles"] is True

    async def test_preprint_rows_keep_their_kind(self, importer_stack) -> None:
        harness, actor, importer = importer_stack
        await importer.import_documents(
            actor,
            documents=[
                {"document_id": "doi:10.1101/x", "doi": "10.1101/x", "source": "biorxiv preprint"}
            ],
        )
        async with harness.context.uow_factory() as uow:
            works = await uow.works.list_works(actor.workspace_id)
        assert works[0].work_kind.value == "preprint"


class TestDatasheetRowImport:
    async def _seed_work(self, importer, actor) -> None:
        await importer.import_documents(
            actor,
            documents=[{"document_id": "doi:10.1/x", "doi": "10.1/x", "source": "crossref"}],
        )

    async def test_rows_import_as_unreviewed_invalid_proposals(self, importer_stack) -> None:
        """Roadmap rule: never promote a legacy row directly to an approved claim."""
        harness, actor, importer = importer_stack
        await self._seed_work(importer, actor)
        report = await importer.import_datasheet_rows(
            actor,
            rows=[
                {
                    "row_id": "row-1",
                    "doi": "10.1/x",
                    "values": {"lipid_yield": "0.42", "temperature": "30 C"},
                    "quote": "yield of 0.42 g/g",
                }
            ],
            template_id="yeast-lipid-datasheet",
            template_version="1.0.0",
        )
        assert report.proposals_created == 2
        async with harness.context.uow_factory() as uow:
            claims = await uow.claims.list_claims(actor.workspace_id)
            revisions = [
                revision
                for claim in claims
                for revision in await uow.claims.list_revisions(actor.workspace_id, claim.id)
            ]
        assert claims
        assert all(claim.lifecycle_status == "proposed" for claim in claims)
        assert all(revision.review_status is ReviewState.UNREVIEWED for revision in revisions)
        assert all(revision.validation_status == "invalid" for revision in revisions)
        assert all(revision.evidence_span_ids == () for revision in revisions)
        assert all(
            revision.proposal_source.value == "legacy_import" for revision in revisions
        )

    async def test_an_imported_proposal_cannot_reach_a_release(self, importer_stack) -> None:
        harness, actor, importer = importer_stack
        await self._seed_work(importer, actor)
        await importer.import_datasheet_rows(
            actor,
            rows=[{"row_id": "row-1", "doi": "10.1/x", "values": {"lipid_yield": "0.42"}}],
            template_id="t",
            template_version="1.0.0",
        )
        from lab_assistant.domain.claims import claim_release_blockers

        async with harness.context.uow_factory() as uow:
            claims = await uow.claims.list_claims(actor.workspace_id)
            revision = await uow.claims.get_revision(
                actor.workspace_id, claims[0].current_revision_id
            )
        blockers = claim_release_blockers(
            revision, verified_span_ids=frozenset(), require_review=True
        )
        assert blockers
        assert any("validation status" in blocker for blocker in blockers)

    async def test_legacy_not_reported_is_not_treated_as_an_absence(
        self, importer_stack
    ) -> None:
        harness, actor, importer = importer_stack
        await self._seed_work(importer, actor)
        await importer.import_datasheet_rows(
            actor,
            rows=[{"row_id": "row-1", "doi": "10.1/x", "values": {"oxygen": "Not reported"}}],
            template_id="t",
            template_version="1.0.0",
        )
        async with harness.context.uow_factory() as uow:
            claims = await uow.claims.list_claims(actor.workspace_id)
            revision = await uow.claims.get_revision(
                actor.workspace_id, claims[0].current_revision_id
            )
        # `extractor_abstained` cannot support a negative statement about the source.
        assert revision.abstention_reason.value == "extractor_abstained"
        assert not revision.abstention_reason.supports_source_statement

    async def test_a_row_without_a_matching_work_is_an_exception(self, importer_stack) -> None:
        _harness, actor, importer = importer_stack
        report = await importer.import_datasheet_rows(
            actor,
            rows=[{"row_id": "orphan", "doi": "10.9/missing", "values": {"a": "1"}}],
            template_id="t",
            template_version="1.0.0",
        )
        assert report.proposals_created == 0
        assert report.exceptions
        assert report.reconciles()


class TestAssetClassification:
    @pytest.mark.parametrize(
        ("record", "expected"),
        [
            (
                {
                    "path": "a.pdf",
                    "bytes_available": True,
                    "sha256": "a" * 64,
                    "source_url": "https://repo.invalid/a.pdf",
                    "licence": "cc-by-4.0",
                },
                AssetClass.A_TRUSTED_ORIGINAL,
            ),
            (
                {"path": "b.pdf", "bytes_available": True, "sha256": "b" * 64},
                AssetClass.B_USABLE_WITH_REVIEW,
            ),
            (
                {"path": "c.pdf", "bytes_available": True},
                AssetClass.B_USABLE_WITH_REVIEW,
            ),
            ({"path": "chunks.jsonl", "kind": "chunk"}, AssetClass.C_DERIVED_REPRODUCIBLE),
            (
                {"path": "rows.csv", "kind": "datasheet_row"},
                AssetClass.D_DERIVED_UNVERIFIABLE,
            ),
            ({"path": "backend/adminPass"}, AssetClass.E_UNSAFE_OR_OBSOLETE),
            ({"path": "gone.pdf", "bytes_available": False}, AssetClass.E_UNSAFE_OR_OBSOLETE),
        ],
    )
    def test_classification(self, record: dict[str, Any], expected: AssetClass) -> None:
        asset_class, reason = classify_asset(record)
        assert asset_class is expected
        assert reason

    def test_credentials_are_never_imported(self) -> None:
        asset_class, reason = classify_asset({"path": "backend/adminPass"})
        assert not asset_class.imports_at_all
        assert "credential" in reason

    async def test_classification_run_reports_every_record(self, importer_stack) -> None:
        _harness, actor, importer = importer_stack
        report = await importer.classify_assets(
            actor,
            records=[
                {
                    "path": "a.pdf",
                    "bytes_available": True,
                    "sha256": "a" * 64,
                    "source_url": "https://repo.invalid/a.pdf",
                    "licence": "cc-by-4.0",
                },
                {"path": "b.pdf", "bytes_available": True},
                {"path": "backend/adminPass"},
            ],
        )
        assert report.assets_by_class == {"A": 1, "B": 1, "E": 1}
        assert report.reconciles()
        assert "class E" in report.skipped[0]["reason"]
        assert report.to_csv().startswith("kind,legacy_id,reason")

    def test_re_acquisition_list_names_hosts_and_unobtainables(self, importer_stack) -> None:
        _harness, _actor, importer = importer_stack
        summary = importer.re_acquisition_list(
            [
                {
                    "path": "a.pdf",
                    "bytes_available": True,
                    "sha256": "a" * 64,
                    "source_url": "https://repo.invalid/a.pdf",
                    "licence": "cc-by-4.0",
                },
                {"path": "b.pdf", "bytes_available": True, "host": "https://publisher.invalid"},
                {"path": "c.pdf", "bytes_available": True},
            ]
        )
        assert summary["counts_by_class"]["A"] == 1
        assert summary["re_acquisition_by_host"]["publisher.invalid"] == 1
        assert "c.pdf" in summary["unobtainable"]
        assert "one request per second" in summary["note"]
