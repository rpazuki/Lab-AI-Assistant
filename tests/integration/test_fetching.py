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
            facts = [(Predicate.LICENCE, licence), (Predicate.FULL_TEXT_LOCATION, location)]
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
            await uow.commit()
        return work.id

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
        assert done == {"requeued": 1, "kept_for_a_person": 1, "already_tried": 0}
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
