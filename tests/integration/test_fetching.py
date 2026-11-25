"""Fetching included papers: what the fetch is told, when it retries, and what is planned.

Each case is one the internal deployment met: every fetched file judged "licence
unknown", Unpaywall never given the link discovery found, a PMCID known only from
Europe PMC's location, a timeout handed straight to a person, and papers included
after a run that were never planned at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from test_scope_deletion import DeletionLab

from lab_assistant.adapters.connectors.fake import FakeAcquisitionConnector
from lab_assistant.adapters.db.engine import create_schema, make_engine
from lab_assistant.adapters.db.repositories import sql_uow_factory
from lab_assistant.application.acquisition import IMPLEMENTATION_VERSION as FETCHER_VERSION
from lab_assistant.application.acquisition import AcquisitionHandler, AcquisitionPlanningHandler
from lab_assistant.application.fetching import FetchQueue
from lab_assistant.application.reconciliation import refresh_summaries
from lab_assistant.config import LabEnv, Settings
from lab_assistant.contracts.connector import (
    AcquisitionRequest,
    IdentifierScheme,
    RawAcquisition,
)
from lab_assistant.contracts.errors import ErrorClass
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.contracts.stage import StageCommand, StageName, StageStatus
from lab_assistant.domain.assets import (
    AcquisitionAttempt,
    AcquisitionOutcome,
    AcquisitionPlan,
    AcquisitionRoute,
    Manifestation,
)
from lab_assistant.domain.identity import Work, WorkIdentifier, WorkKind
from lab_assistant.domain.observation import (
    Predicate,
    SourceAssertion,
    SourceObservation,
    SourceRequest,
)
from lab_assistant.domain.states import RelevanceState, TaskState
from lab_assistant.domain.tasks import Task
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor, reset_database


def compressed_pdf(text: str) -> bytes:
    """A one-page PDF whose text is Flate-compressed, as a journal's is.

    Its bytes never show ``text``: only reading the page as text finds it.
    """
    import zlib

    stream = zlib.compress(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


ACTIONS = frozenset(
    {PolicyAction.STORE_CONTENT, PolicyAction.PROCESS_LOCAL, PolicyAction.INDEX, PolicyAction.QUERY}
)


#: A title long enough to identify a paper by.
TITLE = "Lipid factories in engineered Yarrowia lipolytica strains"

#: One PDF that names both twin papers, fetched for each of them.
TWIN = compressed_pdf("doi:10.9/twin-a and doi:10.9/twin-b")


@dataclass
class Recording(FakeAcquisitionConnector):
    """The fake acquirer, remembering every request it was given."""

    requests: list[AcquisitionRequest] = field(default_factory=list)

    async def acquire(self, request: AcquisitionRequest) -> RawAcquisition:
        self.requests.append(request)
        return await super().acquire(request)


BACKENDS = ["memory", "sqlite"] + (["postgres"] if os.environ.get("LAB_TEST_DATABASE_URL") else [])


@pytest.fixture(params=BACKENDS)
async def lab(request, tmp_path):
    """Over every adapter: SQL enforces the foreign keys an in-memory store does not."""
    acquirer = Recording(
        assets={
            # A source reports no licence: the one discovery recorded must decide.
            "doi:10.9/licensed": (compressed_pdf("doi:10.9/licensed"), "application/pdf", None),
            "doi:10.9/unlicensed": (compressed_pdf("doi:10.9/unlicensed"), "application/pdf", None),
            "pmcid:PMC77": (compressed_pdf("PMCID: PMC77"), "application/pdf", None),
            # No identifier in the file, only the title.
            "doi:10.9/titled": (compressed_pdf(TITLE), "application/pdf", None),
            # A preprint of the published paper: the title, and bioRxiv's own DOI.
            "doi:10.9/published": (
                compressed_pdf(f"{TITLE} doi:10.1101/2024.12.09.627483"),
                "application/pdf",
                None,
            ),
            # A title too short to tell one paper from another.
            "doi:10.9/short": (compressed_pdf("Yarrowia lipids"), "application/pdf", None),
            # One file served for two papers: the same bytes under two assets.
            "doi:10.9/twin-a": (TWIN, "application/pdf", None),
            "doi:10.9/twin-b": (TWIN, "application/pdf", None),
        },
        failures={"doi:10.9/down": ErrorClass.SOURCE_UNAVAILABLE},
    )
    engine = None
    if request.param == "memory":
        harness = build_harness(connectors={"fake-acquire": acquirer})
    else:
        settings = Settings(
            env=LabEnv.LOCAL,
            database_url=(
                os.environ["LAB_TEST_DATABASE_URL"]
                if request.param == "postgres"
                else "sqlite+aiosqlite:///:memory:"
            ),
            object_store_root=tmp_path / "objects",
        )
        engine = make_engine(settings)
        await create_schema(engine, dimensions=(64,))
        await reset_database(engine)
        harness = build_harness(
            uow_factory=sql_uow_factory(engine), connectors={"fake-acquire": acquirer}
        )
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    actor = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    lab = _Lab(harness, actor, acquirer)
    # Source requests belong to a discovery run, and a run to a scope version.
    scopes = DeletionLab(harness, actor)
    lab.run_id = (await scopes.run(await scopes.scope())).id
    try:
        yield lab
    finally:
        if engine is not None:
            await engine.dispose()


@dataclass
class _Lab:
    harness: Any
    actor: Any
    acquirer: Recording
    run_id: UUID | None = None

    @property
    def ws(self) -> UUID:
        return self.actor.workspace_id

    async def paper(
        self,
        doi: str,
        *,
        licence: str | None = None,
        location: str | None = None,
        relevance: RelevanceState = RelevanceState.INCLUDED,
        title: str | None = None,
    ) -> UUID:
        """An included work with a DOI, and what discovery said about its full text."""
        context = self.harness.context
        now = context.now()
        work = Work(
            id=context.new_id(),
            workspace_id=self.ws,
            work_kind=WorkKind.ARTICLE,
            relevance=relevance,
            created_at=now,
        )
        request_id, observation_id = context.new_id(), context.new_id()
        async with context.uow_factory() as uow:
            await uow.works.add(work)
            await uow.works.add_identifier(
                WorkIdentifier(
                    id=context.new_id(),
                    workspace_id=self.ws,
                    work_id=work.id,
                    scheme=IdentifierScheme.DOI,
                    value_raw=doi,
                    value_normalized=doi,
                    value_display=doi,
                    is_active=True,
                    asserted_by=("fixture",),
                )
            )
            await uow.observations.add_request(
                SourceRequest(
                    id=request_id,
                    workspace_id=self.ws,
                    discovery_run_id=self.run_id,
                    connector_id="fixture",
                    connector_version="1.0.0",
                    request_fingerprint=str(request_id),
                    method="GET",
                    canonical_url_redacted="https://example.invalid/q",
                    requested_at=now,
                )
            )
            await uow.observations.add_observation(
                SourceObservation(
                    id=observation_id,
                    workspace_id=self.ws,
                    source_request_id=request_id,
                    source_record_locator=f"doi:{doi}",
                    parser_id="p",
                    parser_version="1",
                    schema_version="1",
                    observed_at=now,
                )
            )
            facts = [
                (Predicate.LICENCE, licence),
                (Predicate.FULL_TEXT_LOCATION, location),
                (Predicate.TITLE, title),
            ]
            await uow.observations.add_assertions(
                [
                    SourceAssertion(
                        id=context.new_id(),
                        workspace_id=self.ws,
                        source_observation_id=observation_id,
                        subject_hint=f"doi:{doi}",
                        predicate=predicate,
                        value_json=value,
                        work_id=work.id,
                    )
                    for predicate, value in facts
                    if value is not None
                ]
            )
            await refresh_summaries(uow, self.ws, [work.id], now=now)
            await uow.commit()
        return work.id

    async def learn(self, work_id: UUID, doi: str, **facts: Any) -> None:
        """A later discovery run's record of a paper already found."""
        context = self.harness.context
        now = context.now()
        request_id, observation_id = context.new_id(), context.new_id()
        async with context.uow_factory() as uow:
            await uow.observations.add_request(
                SourceRequest(
                    id=request_id,
                    workspace_id=self.ws,
                    discovery_run_id=self.run_id,
                    connector_id="fixture",
                    connector_version="1.1.0",
                    request_fingerprint=str(request_id),
                    method="GET",
                    canonical_url_redacted="https://example.invalid/q2",
                    requested_at=now,
                )
            )
            await uow.observations.add_observation(
                SourceObservation(
                    id=observation_id,
                    workspace_id=self.ws,
                    source_request_id=request_id,
                    source_record_locator=f"doi:{doi}",
                    parser_id="p",
                    parser_version="1",
                    schema_version="1",
                    observed_at=now,
                )
            )
            await uow.observations.add_assertions(
                [
                    SourceAssertion(
                        id=context.new_id(),
                        workspace_id=self.ws,
                        source_observation_id=observation_id,
                        subject_hint=f"doi:{doi}",
                        predicate=Predicate(name),
                        value_json=value,
                        work_id=work_id,
                    )
                    for name, value in facts.items()
                ]
            )
            await uow.commit()

    async def plan(self, work_id: UUID, routes: tuple[AcquisitionRoute, ...]) -> AcquisitionPlan:
        context = self.harness.context
        manifestation = Manifestation(
            id=context.new_id(),
            workspace_id=self.ws,
            work_id=work_id,
            manifestation_kind="publisher_pdf",
            created_at=context.now(),
        )
        plan = AcquisitionPlan(
            id=context.new_id(),
            workspace_id=self.ws,
            work_id=work_id,
            manifestation_ids=(manifestation.id,),
            requested_actions=ACTIONS,
            route_order=routes,
            status="planned",
            created_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.assets.add_manifestation(manifestation)
            await uow.assets.add_plan(plan)
            await uow.commit()
        return plan

    async def fetch(self, plan: AcquisitionPlan, *, attempt: int = 1, of: int = 1):
        return await AcquisitionHandler(self.harness.context).execute(
            StageCommand(
                stage=StageName.ACQUISITION,
                workspace_id=self.ws,
                actor_id=self.actor.user_id,
                inputs={"plan_id": str(plan.id)},
                attempt=attempt,
                max_attempts=of,
            )
        )

    async def status(self, plan: AcquisitionPlan) -> str:
        async with self.harness.context.uow_factory() as uow:
            stored = await uow.assets.get_plan(self.ws, plan.id)
        assert stored is not None
        return stored.status


OPEN = (AcquisitionRoute.OPEN_REPOSITORY, AcquisitionRoute.USER_ASSISTED)


class TestWhatTheFetchIsTold:
    def test_a_compressed_pdf_hides_its_doi_from_a_byte_search(self) -> None:
        assert b"10.9/licensed" not in compressed_pdf("doi:10.9/licensed")

    async def test_the_licence_discovery_recorded_decides_a_fetched_file(self, lab) -> None:
        licensed = await lab.plan(await lab.paper("10.9/licensed", licence="cc-by-4.0"), OPEN)
        unknown = await lab.plan(await lab.paper("10.9/unlicensed"), OPEN)

        assert (await lab.fetch(licensed)).status is StageStatus.SUCCEEDED
        assert await lab.status(licensed) == "acquired"
        # Without one there is still no licence: a person decides, as before.
        assert (await lab.fetch(unknown)).status is StageStatus.AWAITING_USER
        assert await lab.status(unknown) == "assisted"

    async def test_the_open_access_link_is_passed_on(self, lab) -> None:
        link = "https://journal.example/paper.pdf"
        plan = await lab.plan(
            await lab.paper("10.9/licensed", licence="cc-by-4.0", location=link), OPEN
        )
        await lab.fetch(plan)
        assert lab.acquirer.requests[-1].target_url == link

    async def test_a_pmcid_known_only_from_europe_pmcs_location_is_used(self, lab) -> None:
        work = await lab.paper("10.9/elsewhere", licence="cc-by-4.0", location="pmc:PMC77")
        plan = await lab.plan(work, OPEN)
        assert (await lab.fetch(plan)).status is StageStatus.SUCCEEDED
        sent = {(i.scheme, i.value) for i in lab.acquirer.requests[-1].identifiers}
        assert (IdentifierScheme.PMCID, "PMC77") in sent


class TestTheIdentityCheck:
    async def test_a_file_naming_the_title_is_the_paper(self, lab) -> None:
        plan = await lab.plan(
            await lab.paper("10.9/titled", licence="cc-by-4.0", title=TITLE), OPEN
        )
        assert (await lab.fetch(plan)).status is StageStatus.SUCCEEDED
        async with lab.harness.context.uow_factory() as uow:
            [asset] = await uow.assets.list_assets(lab.ws, work_id=plan.work_id)
        assert asset.identity_verified
        assert asset.metadata["identity_basis"] == "title"
        assert asset.metadata["version"] is None

    async def test_a_preprint_of_the_paper_is_accepted_and_marked(self, lab) -> None:
        plan = await lab.plan(
            await lab.paper("10.9/published", licence="cc-by-4.0", title=TITLE), OPEN
        )
        assert (await lab.fetch(plan)).status is StageStatus.SUCCEEDED
        async with lab.harness.context.uow_factory() as uow:
            [asset] = await uow.assets.list_assets(lab.ws, work_id=plan.work_id)
        assert asset.metadata["version"] == "preprint"

    async def test_a_short_title_is_no_evidence(self, lab) -> None:
        plan = await lab.plan(
            await lab.paper("10.9/short", licence="cc-by-4.0", title="Yarrowia lipids"), OPEN
        )
        result = await lab.fetch(plan)
        assert result.status is StageStatus.FAILED
        assert result.error_class is ErrorClass.IDENTITY_MISMATCH

    async def test_a_file_an_older_rule_rejected_is_checked_again_once(self, lab) -> None:
        """The stored file is read again, not fetched again, and parsing follows."""
        context = lab.harness.context
        plan = await lab.plan(
            await lab.paper("10.9/titled", licence="cc-by-4.0", title=TITLE), OPEN
        )
        await lab.fetch(plan)
        async with context.uow_factory() as uow:
            [asset] = await uow.assets.list_assets(lab.ws, work_id=plan.work_id)
            # As the identifier-only rule left it: unverified, no rule recorded.
            asset.identity_verified = False
            asset.metadata = {
                key: value
                for key, value in asset.metadata.items()
                if key not in ("identity_basis", "version", "identity_rule")
            }
            await uow.assets.update_asset(asset)
            stored = await uow.assets.get_plan(lab.ws, plan.id)
            stored.status = "failed"
            await uow.assets.update_plan(stored)
            task = Task(
                id=context.new_id(),
                workspace_id=lab.ws,
                stage=StageName.ACQUISITION,
                state=TaskState.FAILED,
                input_fingerprint=str(context.new_id()),
                inputs={"plan_id": str(plan.id)},
                attempt=1,
                error_class=ErrorClass.IDENTITY_MISMATCH,
                created_at=context.now(),
            )
            await uow.tasks.add(task)
            await uow.commit()

        queue = FetchQueue(context, lab.harness.orchestrator)
        assert (await queue.retry_waiting(lab.actor))["requeued"] == 1
        fetched = len(lab.acquirer.requests)
        result = await lab.fetch(plan)
        assert result.status is StageStatus.SUCCEEDED_NOOP
        assert [t.stage for t in result.next_tasks] == [StageName.PARSING]
        assert len(lab.acquirer.requests) == fetched  # nothing was fetched again
        async with context.uow_factory() as uow:
            [asset] = await uow.assets.list_assets(lab.ws, work_id=plan.work_id)
            assert asset.identity_verified and asset.metadata["identity_basis"] == "title"
            assert (await uow.assets.get_plan(lab.ws, plan.id)).status == "acquired"
            again = await uow.tasks.get(lab.ws, task.id)
            again.state = TaskState.FAILED
            await uow.tasks.update(again)
            await uow.commit()
        # Checked by this rule now: a later press does not read it again.
        assert (await queue.retry_waiting(lab.actor))["requeued"] == 0


class TestParsingWhatWasFetched:
    async def test_the_same_bytes_for_two_papers_parse_twice_without_a_clash(self, lab) -> None:
        """Both assets once shared one parse fingerprint, and the second insert died."""
        from lab_assistant.application.parsing import ParsingHandler

        assets = []
        for doi in ("10.9/twin-a", "10.9/twin-b"):
            plan = await lab.plan(await lab.paper(doi, licence="cc-by-4.0"), OPEN)
            assert (await lab.fetch(plan)).status is StageStatus.SUCCEEDED
            async with lab.harness.context.uow_factory() as uow:
                [asset] = await uow.assets.list_assets(lab.ws, work_id=plan.work_id)
            assets.append(asset)
        assert assets[0].sha256 == assets[1].sha256

        parse = ParsingHandler(lab.harness.context)
        for asset in assets:
            result = await parse.execute(
                StageCommand(
                    stage=StageName.PARSING,
                    workspace_id=lab.ws,
                    actor_id=lab.actor.user_id,
                    inputs={"asset_id": str(asset.id)},
                )
            )
            assert result.status in (StageStatus.SUCCEEDED, StageStatus.DEGRADED), result
        async with lab.harness.context.uow_factory() as uow:
            for asset in assets:
                assert await uow.renditions.list_renditions(lab.ws, asset_id=asset.id)


class TestParseQualityLimits:
    async def _degraded(self, lab, doi: str, **metrics: Any) -> UUID:
        """A fetched PDF whose parse missed a limit, with what the parse measured."""
        from lab_assistant.contracts.parsing import ParseQualityMetrics
        from lab_assistant.domain.renditions import ParseRun, Rendition
        from lab_assistant.domain.states import ParseState

        context = lab.harness.context
        plan = await lab.plan(await lab.paper(doi, licence="cc-by-4.0"), OPEN)
        await lab.fetch(plan)
        async with context.uow_factory() as uow:
            [asset] = await uow.assets.list_assets(lab.ws, work_id=plan.work_id)
            run = ParseRun(
                id=context.new_id(),
                workspace_id=lab.ws,
                asset_id=asset.id,
                parser_id="pypdf-text",
                parser_version="1.0.0",
                input_sha256=asset.sha256,
                run_fingerprint=str(context.new_id()),
                status="succeeded",
                quality_metrics=ParseQualityMetrics(
                    page_count=10,
                    pages_represented=10,
                    characters=40_000,
                    reference_separation=True,
                    **metrics,
                ),
            )
            await uow.renditions.add_parse_run(run)
            rendition = Rendition(
                id=context.new_id(),
                workspace_id=lab.ws,
                parse_run_id=run.id,
                asset_id=asset.id,
                work_id=plan.work_id,
                quality_status=ParseState.DEGRADED,
                created_at=context.now(),
            )
            await uow.renditions.add_rendition(rendition)
            await uow.commit()
        return rendition.id

    async def test_loosened_limits_promote_degraded_documents_on_the_next_check(self, lab) -> None:
        from lab_assistant.application.parse_quality import ParseQualityLimits, ParseQualityService

        repeated = await self._degraded(lab, "10.9/licensed", duplicate_paragraph_ratio=0.15)
        much = await self._degraded(lab, "10.9/twin-a", duplicate_paragraph_ratio=0.6)
        service = ParseQualityService(lab.harness.context, lab.harness.orchestrator)

        view = await service.view(lab.actor)
        assert view["degraded"] == 2
        assert view["degraded_by_reason"] == {"too many repeated paragraphs": 2}
        assert view["defaults"]["application/pdf"]["max_duplicate_paragraph_ratio"] == 0.10

        # Saving changes nothing already parsed: the check runs on the next press.
        await service.set_limits(lab.actor, ParseQualityLimits(max_duplicate_paragraph_ratio=0.2))
        assert (await service.view(lab.actor))["degraded"] == 2
        assert await service.recheck_degraded(lab.actor) == {"promoted": 1, "still_degraded": 1}
        async with lab.harness.context.uow_factory() as uow:
            assert (
                await uow.renditions.get_rendition(lab.ws, repeated)
            ).quality_status.value == "ready"
            assert (
                await uow.renditions.get_rendition(lab.ws, much)
            ).quality_status.value == "degraded"
            queued = await uow.tasks.page_tasks(
                lab.ws, states=["ready"], stage=StageName.EXTRACTION.value
            )
        assert [t.inputs["rendition_id"] for t in queued] == [str(repeated)]

    async def test_new_parses_use_the_limits_in_force(self, lab) -> None:
        from lab_assistant.application.parse_quality import (
            ParseQualityLimits,
            ParseQualityService,
            apply_limits,
            current_limits,
        )

        service = ParseQualityService(lab.harness.context)
        await service.set_limits(lab.actor, ParseQualityLimits(require_reference_separation=False))
        parsers = lab.harness.context.require_parsers()
        async with lab.harness.context.uow_factory() as uow:
            limits, version = await current_limits(uow, lab.ws)
        xml = apply_limits(parsers.profile_for("application/xml"), limits)
        assert xml.require_reference_separation is False
        assert version.semantic_version == "1.0.0"


class TestAScannerThatCannotRun:
    async def test_leaves_the_file_unscanned_not_infected(self, lab) -> None:
        """A missing scanner reported "not clean", which quarantined every file."""
        lab.harness.context.malware_scanner.available = False
        plan = await lab.plan(await lab.paper("10.9/licensed", licence="cc-by-4.0"), OPEN)
        assert (await lab.fetch(plan)).status is StageStatus.SUCCEEDED
        async with lab.harness.context.uow_factory() as uow:
            [asset] = await uow.assets.list_assets(lab.ws, work_id=plan.work_id)
        assert asset.malware_scan_status.value == "scanner_unavailable"
        assert asset.validation_status.value != "quarantined"


class TestASourceThatIsDown:
    async def test_is_tried_again_while_deliveries_remain_then_handed_to_a_person(
        self, lab
    ) -> None:
        plan = await lab.plan(await lab.paper("10.9/down", licence="cc-by-4.0"), OPEN)

        first = await lab.fetch(plan, attempt=1, of=3)
        assert first.status is StageStatus.FAILED
        assert first.error_class is ErrorClass.SOURCE_UNAVAILABLE  # retryable
        assert await lab.status(plan) == "planned"

        last = await lab.fetch(plan, attempt=3, of=3)
        assert last.status is StageStatus.AWAITING_USER
        assert await lab.status(plan) == "assisted"


class TestPlanning:
    async def _plan_all(self, lab) -> dict[str, Any]:
        result = await AcquisitionPlanningHandler(lab.harness.context).execute(
            StageCommand(
                stage=StageName.ACQUISITION_PLANNING,
                workspace_id=lab.ws,
                actor_id=lab.actor.user_id,
                inputs={"included_by": "test"},
            )
        )
        return result.metrics

    async def test_a_paper_included_later_is_planned_and_none_is_planned_twice(self, lab) -> None:
        await lab.paper("10.9/first", licence="cc-by-4.0")
        assert (await self._plan_all(lab))["plans_created"] == 1

        await lab.paper("10.9/later", licence="cc-by-4.0")
        await lab.paper("10.9/excluded", relevance=RelevanceState.EXCLUDED)
        assert (await self._plan_all(lab))["plans_created"] == 1  # only the new one
        assert (await self._plan_all(lab))["plans_created"] == 0
        async with lab.harness.context.uow_factory() as uow:
            plans = await uow.assets.list_plans(lab.ws)
        assert len({plan.work_id for plan in plans}) == len(plans) == 2

    async def test_asking_while_planning_is_still_queued_returns_that_task(self, lab) -> None:
        queue = FetchQueue(lab.harness.context, lab.harness.orchestrator)
        first = await queue.plan_included(lab.actor, reason="relevance_set")
        again = await queue.plan_included(lab.actor, reason="bulk_relevance")
        assert again.id == first.id and first.stage is StageName.ACQUISITION_PLANNING


class TestTryingAgain:
    async def test_only_fetches_with_an_automatic_route_go_back_in_the_queue(self, lab) -> None:
        context = lab.harness.context
        automatic = await lab.plan(await lab.paper("10.9/a", licence="cc-by-4.0"), OPEN)
        upload_only = await lab.plan(await lab.paper("10.9/b"), (AcquisitionRoute.USER_ASSISTED,))
        tasks = {}
        async with context.uow_factory() as uow:
            for plan in (automatic, upload_only):
                task = Task(
                    id=context.new_id(),
                    workspace_id=lab.ws,
                    stage=StageName.ACQUISITION,
                    state=TaskState.AWAITING_USER,
                    input_fingerprint=str(context.new_id()),
                    inputs={"plan_id": str(plan.id)},
                    attempt=1,
                    human_action_required="assisted acquisition required",
                    created_at=context.now(),
                )
                await uow.tasks.add(task)
                tasks[plan.id] = task.id
            await uow.commit()

        queue = FetchQueue(context, lab.harness.orchestrator)
        done = await queue.retry_waiting(lab.actor)
        assert done == {"requeued": 1, "replanned": 0, "kept_for_a_person": 1, "already_tried": 0}
        async with context.uow_factory() as uow:
            again = await uow.tasks.get(lab.ws, tasks[automatic.id])
            kept = await uow.tasks.get(lab.ws, tasks[upload_only.id])
        assert again.state is TaskState.READY and again.attempt == 0
        assert kept.state is TaskState.AWAITING_USER

        # The current fetcher tries it, every route fails, and it waits for a person.
        async with context.uow_factory() as uow:
            await uow.assets.add_attempt(
                AcquisitionAttempt(
                    id=context.new_id(),
                    workspace_id=lab.ws,
                    acquisition_plan_id=automatic.id,
                    manifestation_id=automatic.manifestation_ids[0],
                    connector_id="unpaywall",
                    route=AcquisitionRoute.OPEN_REPOSITORY,
                    outcome=AcquisitionOutcome.NOT_FOUND,
                    worker_version=FETCHER_VERSION,
                    requested_at=context.now(),
                )
            )
            again.state = TaskState.AWAITING_USER
            again.version += 1
            await uow.tasks.update(again)
            await uow.commit()
        # A second press sends nothing to the sources: the answer would be the same.
        assert await queue.retry_waiting(lab.actor) == {
            "requeued": 0,
            "replanned": 0,
            "kept_for_a_person": 1,
            "already_tried": 1,
        }

        # But a source that was rate-limiting gave no answer: that paper is tried again.
        async with context.uow_factory() as uow:
            await uow.assets.add_attempt(
                AcquisitionAttempt(
                    id=context.new_id(),
                    workspace_id=lab.ws,
                    acquisition_plan_id=automatic.id,
                    manifestation_id=automatic.manifestation_ids[0],
                    connector_id="unpaywall",
                    route=AcquisitionRoute.OPEN_REPOSITORY,
                    attempt_number=2,
                    outcome=AcquisitionOutcome.RATE_LIMITED_RETRYABLE,
                    worker_version=FETCHER_VERSION,
                    requested_at=context.now(),
                )
            )
            await uow.commit()
        assert (await queue.retry_waiting(lab.actor))["requeued"] == 1


class TestCollectionProgress:
    async def test_counts_every_stage_and_why_papers_are_not_fetched(self, lab) -> None:
        from lab_assistant.application.collection_progress import CollectionProgressService

        fetched = await lab.plan(await lab.paper("10.9/licensed", licence="cc-by-4.0"), OPEN)
        refused = await lab.plan(await lab.paper("10.9/unlicensed"), OPEN)
        upload = await lab.plan(await lab.paper("10.9/b"), (AcquisitionRoute.USER_ASSISTED,))
        await lab.fetch(fetched)
        await lab.fetch(refused)
        async with lab.harness.context.uow_factory() as uow:
            stored = await uow.assets.get_plan(lab.ws, upload.id)
            stored.status = "assisted"
            await uow.assets.update_plan(stored)
            await uow.commit()
            outcomes = await uow.assets.latest_outcomes(lab.ws, status="assisted")
        # The refused one's last answer, and one no source was ever asked about.
        assert outcomes == {"licence_not_permitted": 1, "none": 1}

        view = await CollectionProgressService(lab.harness.context).view(lab.actor)
        funnel = {row["stage"]: row["count"] for row in view["funnel"]}
        assert funnel["found"] == 3 and funnel["included"] == 3
        assert funnel["planned"] == 3 and funnel["fetched"] >= 1
        assert view["acquisition"]["plans"]["acquired"] == 1
        assert view["acquisition"]["waiting_reasons"] == outcomes
        assert view["acquisition"]["identity"]["identifier"] >= 1
        assert len(view["daily"]) == 30 and view["daily"][-1]["found"] == 3


class TestReplanningUploadOnly:
    """A paper planned for upload only gets the open routes once discovery finds an open licence."""

    async def waiting(self, lab, plan) -> UUID:
        context = lab.harness.context
        task = Task(
            id=context.new_id(),
            workspace_id=lab.ws,
            stage=StageName.ACQUISITION,
            state=TaskState.AWAITING_USER,
            input_fingerprint=str(context.new_id()),
            inputs={"plan_id": str(plan.id)},
            attempt=1,
            human_action_required="assisted acquisition required",
            created_at=context.now(),
        )
        async with context.uow_factory() as uow:
            await uow.tasks.add(task)
            stored = await uow.assets.get_plan(lab.ws, plan.id)
            stored.status = "assisted"
            stored.version += 1
            await uow.assets.update_plan(stored)
            await uow.commit()
        return task.id

    async def test_an_open_licence_found_later_queues_the_paper_again(self, lab) -> None:
        context = lab.harness.context
        work = await lab.paper("10.9/licensed")
        plan = await lab.plan(work, (AcquisitionRoute.USER_ASSISTED,))
        task_id = await self.waiting(lab, plan)
        queue = FetchQueue(context, lab.harness.orchestrator)

        # No licence yet: it stays with a person, and no rights decision is written.
        async with context.uow_factory() as uow:
            before = len(await uow.policies.list_rights_decisions(lab.ws))
        assert (await queue.retry_waiting(lab.actor))["kept_for_a_person"] == 1
        async with context.uow_factory() as uow:
            assert len(await uow.policies.list_rights_decisions(lab.ws)) == before

        # A rerun's source records an open licence for it.
        await lab.learn(work, "10.9/licensed", licence="cc-by-4.0")
        done = await queue.retry_waiting(lab.actor)
        assert done["replanned"] == 1 and done["requeued"] == 1 and done["kept_for_a_person"] == 0
        async with context.uow_factory() as uow:
            stored = await uow.assets.get_plan(lab.ws, plan.id)
            task = await uow.tasks.get(lab.ws, task_id)
        assert stored.status == "planned"
        assert AcquisitionRoute.OPEN_REPOSITORY in stored.route_order
        assert stored.route_order[-1] is AcquisitionRoute.USER_ASSISTED
        assert task.state is TaskState.READY and task.attempt == 0

        # And the open route now fetches it.
        await lab.fetch(stored)
        assert await lab.status(plan) == "acquired"

    async def test_a_licence_that_forbids_keeping_a_copy_changes_nothing(self, lab) -> None:
        context = lab.harness.context
        work = await lab.paper("10.9/unlicensed")
        plan = await lab.plan(work, (AcquisitionRoute.USER_ASSISTED,))
        task_id = await self.waiting(lab, plan)
        await lab.learn(work, "10.9/unlicensed", licence="all-rights-reserved")
        done = await FetchQueue(context, lab.harness.orchestrator).retry_waiting(lab.actor)
        assert done["replanned"] == 0 and done["kept_for_a_person"] == 1
        async with context.uow_factory() as uow:
            stored = await uow.assets.get_plan(lab.ws, plan.id)
            task = await uow.tasks.get(lab.ws, task_id)
        assert stored.route_order == (AcquisitionRoute.USER_ASSISTED,)
        assert task.state is TaskState.AWAITING_USER
