"""Bulk PDF import: identifiers from names and first pages, strict matching.

The service is the one the web drop and `lab acquisition import-folder` share, so
these tests cover both. Waiting acquisition plans are seeded directly: how a plan
comes to wait for a person is the acquisition tests' business, not this one's.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from lab_assistant.application.bulk_import import (
    BulkFile,
    BulkImportService,
    FileOutcome,
    identifiers_in_name,
    identifiers_in_text,
)
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import ValidationFailure
from lab_assistant.domain.assets import AcquisitionPlan, Manifestation, ManifestationKind
from lab_assistant.domain.identity import Work, WorkIdentifier
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor

PASSWORD = "a-long-enough-passphrase"


def _pdf(*, first_page: str = "", metadata: dict[str, str] | None = None) -> bytes:
    """A real one-page PDF whose text layer says `first_page`."""
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    text = first_page.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 10 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    if metadata:
        writer.add_metadata(metadata)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestIdentifiersInNames:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("10.1234_yeast.2025.01.pdf", ["doi:10.1234/yeast.2025.01"]),
            ("doi_10.1234_abc.PDF", ["doi:10.1234/abc"]),
            ("10.1234%2Fabc.pdf", ["doi:10.1234/abc"]),
            ("PMID12345678.pdf", ["pmid:12345678"]),
            ("12345678.pdf", ["pmid:12345678"]),
            ("PMC9876543.pdf", ["pmcid:PMC9876543"]),
            ("lipid paper final v2.pdf", []),
        ],
    )
    def test_reads_the_usual_spellings(self, name: str, expected: list[str]) -> None:
        assert identifiers_in_name(name) == expected

    def test_text_identifiers_drop_trailing_punctuation(self) -> None:
        text = "Published as https://doi.org/10.1234/Yeast.42. PMID: 31415926 (PMC1234567)."
        assert identifiers_in_text(text) == [
            "doi:10.1234/yeast.42",
            "pmid:31415926",
            "pmcid:PMC1234567",
        ]


@pytest.fixture
async def seeded():
    harness = build_harness()
    accounts = harness.accounts
    owner = await accounts.create_user(email="steward@lab.invalid", password=PASSWORD)
    workspace, _ = await accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    actor = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    plans: dict[str, UUID] = {}

    async def waiting(doi: str, *, status: str = "assisted") -> UUID:
        now = datetime.now(tz=UTC)
        work = Work(id=uuid4(), workspace_id=workspace.id, created_at=now)
        manifestation = Manifestation(
            id=uuid4(),
            workspace_id=workspace.id,
            work_id=work.id,
            manifestation_kind=ManifestationKind.PUBLISHER_PDF,
            created_at=now,
        )
        plan = AcquisitionPlan(
            id=uuid4(),
            workspace_id=workspace.id,
            work_id=work.id,
            manifestation_ids=(manifestation.id,),
            status=status,
            created_at=now,
        )
        async with harness.context.uow_factory() as uow:
            await uow.works.add(work)
            await uow.works.add_identifier(
                WorkIdentifier(
                    id=uuid4(),
                    workspace_id=workspace.id,
                    work_id=work.id,
                    scheme=IdentifierScheme.DOI,
                    value_normalized=doi.lower(),
                    value_display=doi,
                )
            )
            await uow.assets.add_manifestation(manifestation)
            await uow.assets.add_plan(plan)
            await uow.commit()
        plans[doi] = plan.id
        return plan.id

    await waiting("10.1234/waiting.one")
    await waiting("10.1234/waiting.two", status="failed")
    await waiting("10.1234/already.acquired", status="acquired")
    return harness, actor, plans


async def _dry(harness, actor, *files: BulkFile):
    return await BulkImportService(harness.context, harness.orchestrator).run(
        actor,
        files=list(files),
        access_basis="institutional_subscription_copy",
        attestation="",
        dry_run=True,
    )


class TestMatching:
    async def test_a_file_name_matches_a_waiting_plan(self, seeded) -> None:
        harness, actor, plans = seeded
        [result] = await _dry(harness, actor, BulkFile("10.1234_waiting.one.pdf", b"%PDF-1.7"))
        assert result.outcome is FileOutcome.MATCHED
        assert result.plan_id == plans["10.1234/waiting.one"]

    async def test_a_failed_plan_also_waits_for_a_person(self, seeded) -> None:
        harness, actor, plans = seeded
        [result] = await _dry(harness, actor, BulkFile("10.1234_waiting.two.pdf", b"%PDF-1.7"))
        assert result.plan_id == plans["10.1234/waiting.two"]

    async def test_an_acquired_work_is_not_matched_again(self, seeded) -> None:
        harness, actor, _ = seeded
        [result] = await _dry(harness, actor, BulkFile("10.1234_already.acquired.pdf", b"%PDF-1.7"))
        assert result.outcome is FileOutcome.UNMATCHED
        assert result.identifiers == ["doi:10.1234/already.acquired"]

    async def test_the_first_page_is_read_when_the_name_says_nothing(self, seeded) -> None:
        harness, actor, plans = seeded
        content = _pdf(first_page="Yarrowia lipids. doi:10.1234/waiting.one")
        [result] = await _dry(harness, actor, BulkFile("scan 0042.pdf", content))
        assert result.outcome is FileOutcome.MATCHED
        assert result.plan_id == plans["10.1234/waiting.one"]

    async def test_metadata_counts_as_the_first_page(self, seeded) -> None:
        harness, actor, plans = seeded
        content = _pdf(metadata={"/Subject": "Journal, doi:10.1234/waiting.two"})
        [result] = await _dry(harness, actor, BulkFile("download.pdf", content))
        assert result.plan_id == plans["10.1234/waiting.two"]

    async def test_a_page_naming_two_waiting_works_is_left_to_a_person(self, seeded) -> None:
        harness, actor, _ = seeded
        content = _pdf(first_page="doi:10.1234/waiting.one and doi:10.1234/waiting.two")
        [result] = await _dry(harness, actor, BulkFile("both.pdf", content))
        assert result.outcome is FileOutcome.AMBIGUOUS
        assert result.plan_id is None

    async def test_two_files_for_one_plan_are_not_both_imported(self, seeded) -> None:
        harness, actor, _ = seeded
        first, second = await _dry(
            harness,
            actor,
            BulkFile("10.1234_waiting.one.pdf", b"%PDF-1.7"),
            BulkFile("copy of 10.1234_waiting.one.pdf", _pdf(first_page="doi:10.1234/waiting.one")),
        )
        assert first.outcome is FileOutcome.MATCHED
        assert second.outcome is FileOutcome.AMBIGUOUS
        assert "already matched" in (second.detail or "")

    async def test_a_file_with_no_identifier_says_so(self, seeded) -> None:
        harness, actor, _ = seeded
        [result] = await _dry(harness, actor, BulkFile("notes.pdf", b"not really a pdf"))
        assert result.outcome is FileOutcome.UNMATCHED
        assert "no DOI, PMID or PMCID" in (result.detail or "")

    async def test_importing_needs_an_attestation(self, seeded) -> None:
        harness, actor, _ = seeded
        with pytest.raises(ValidationFailure, match="attestation"):
            await BulkImportService(harness.context, harness.orchestrator).run(
                actor,
                files=[BulkFile("10.1234_waiting.one.pdf", b"%PDF-1.7")],
                access_basis="institutional_subscription_copy",
                attestation=" ",
            )


class TestImport:
    async def test_a_match_goes_through_the_assisted_upload_path(self, seeded) -> None:
        harness, actor, plans = seeded
        content = _pdf(first_page="Lipid accumulation. doi:10.1234/waiting.one")
        [result] = await BulkImportService(harness.context, harness.orchestrator).run(
            actor,
            files=[BulkFile("10.1234_waiting.one.pdf", content, "application/pdf")],
            access_basis="institutional_subscription_copy",
            attestation="Obtained through the library subscription.",
        )
        assert result.outcome in (FileOutcome.IMPORTED, FileOutcome.BLOCKED), result.detail
        assert result.asset_id is not None
        async with harness.context.uow_factory() as uow:
            asset = await uow.assets.get_asset(actor.workspace_id, result.asset_id)
        assert asset is not None
        assert asset.work_id == (await _work_of(harness, actor, plans["10.1234/waiting.one"]))
        if result.outcome is FileOutcome.IMPORTED:
            assert result.parse_task_id is not None


async def _work_of(harness, actor, plan_id: UUID) -> UUID:
    async with harness.context.uow_factory() as uow:
        plan = await uow.assets.get_plan(actor.workspace_id, plan_id)
    assert plan is not None
    return plan.work_id
