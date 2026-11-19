"""Deterministic in-process stack.

Builds the whole application over in-memory adapters, a fixed clock, sequential
ids, fixture-keyed fake connectors, and a hashing embedder. Used by the test
suite and by ``lab demo``, so the thing tests exercise is the thing a developer
can run.

Nothing here reaches the network, the filesystem, or a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from lab_assistant.adapters.connectors.base import ConnectorRegistryImpl
from lab_assistant.adapters.connectors.fake import (
    FakeAcquisitionConnector,
    FakeSourceConnector,
    FixturePage,
)
from lab_assistant.adapters.memory.repositories import MemoryDatabase, memory_uow_factory
from lab_assistant.adapters.memory.search import MemorySearchIndex
from lab_assistant.adapters.models.fake import (
    FakeExtractor,
    FakeLLMProvider,
    HashingEmbeddingProvider,
)
from lab_assistant.adapters.object_store.filesystem import InMemoryObjectStore
from lab_assistant.adapters.parsers.registry import default_parser_registry
from lab_assistant.adapters.security import StubScanner
from lab_assistant.application import build_handler_registry
from lab_assistant.application.accounts import AccountService
from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.application.tasks import HandlerRegistry, TaskOrchestrator
from lab_assistant.config import LabEnv, Settings
from lab_assistant.contracts.runtime import FixedClock, SequentialIds
from lab_assistant.domain.installation import (
    InstallationCapability,
    InstallationRole,
    InstanceCapabilities,
)
from lab_assistant.domain.workspace import Capability, Membership, MembershipStatus, Role
from lab_assistant.policy.engine import PolicyEngine
from lab_assistant.policy.model_transfer import ModelTransferPolicy
from lab_assistant.policy.rights import RightsPolicy, default_open_access_policy

__all__ = [
    "DEFAULT_DEMO_WORKS",
    "DemoCorpus",
    "Harness",
    "build_harness",
    "make_actor",
    "publish_demo_corpus",
    "reset_database",
    "sample_jats",
    "source_page",
]

FIXED_START = datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC)


@dataclass
class Harness:
    """Everything a test needs, wired and inspectable."""

    settings: Settings
    #: ``None`` when the harness was given a SQL unit of work: the lifecycle then
    #: runs against a real database with its foreign keys enforced, and there is
    #: no dictionary to inspect.
    db: MemoryDatabase | None
    object_store: Any
    search: Any
    clock: FixedClock
    ids: SequentialIds
    connectors: ConnectorRegistryImpl
    llm: FakeLLMProvider
    embedder: HashingEmbeddingProvider
    scanner: StubScanner
    context: ServiceContext
    handlers: HandlerRegistry
    orchestrator: TaskOrchestrator
    accounts: AccountService
    extras: dict[str, Any] = field(default_factory=dict)

    async def run_all_tasks(
        self, *, limit: int = 200, worker_classes: tuple[str, ...] = ("network", "compute")
    ) -> int:
        """Drain the queue the way the workers do, deterministically."""
        executed = 0
        for _ in range(limit):
            progressed = False
            for worker_class in worker_classes:
                result = await self.orchestrator.run_once(
                    owner=f"test-{worker_class}", worker_class=worker_class
                )
                if not result.idle:
                    executed += result.executed
                    progressed = True
            if not progressed:
                break
        return executed


async def reset_database(engine: Any) -> None:
    """Empty every table this application declares, leaving the schema in place.

    SQLite's ``:memory:`` gives each engine its own empty database, so the
    offline suite never needed this. A PostgreSQL test database is shared by
    every test in the session, and the deterministic id factory hands the second
    test the same UUIDs as the first — so the second test fails on a primary key
    before it reaches its subject. That is why the PostgreSQL suite was
    documented in CONTRIBUTING and was not green.

    Only tables in ``METADATA`` are touched. A table this application did not
    declare is left alone and the schema itself is never dropped, because
    ``LAB_TEST_DATABASE_URL`` points at whatever the operator set and a test
    helper has no business dropping what it did not create. Point it at a
    disposable database regardless.
    """
    from sqlalchemy import text

    from lab_assistant.adapters.db.tables import METADATA

    names = [table.name for table in METADATA.sorted_tables]
    if not names:  # pragma: no cover - the schema is never empty in practice
        return
    async with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            quoted = ", ".join(f'"{name}"' for name in names)
            await connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
        else:
            for name in reversed(names):
                await connection.execute(text(f'DELETE FROM "{name}"'))


def build_harness(
    *,
    connectors: dict[str, Any] | None = None,
    rights_policy: RightsPolicy | None = None,
    settings_overrides: dict[str, Any] | None = None,
    llm: FakeLLMProvider | None = None,
    extractor: Any | None = None,
    embedding_dimensions: int = 64,
    uow_factory: Any | None = None,
    object_store: Any | None = None,
    search_index: Any | None = None,
    installation_role: InstallationRole = InstallationRole.PUBLISHER,
) -> Harness:
    """Wire a complete application, in memory by default.

    ``uow_factory``, ``object_store`` and ``search_index`` let a test run the same
    lifecycle against a real database and a real store. That is not a convenience:
    the in-memory repositories enforce workspace scoping but no referential
    integrity, so a path that writes a reference to a row it never created passes
    here and fails on PostgreSQL. One fixture that swaps these three is the
    difference between a suite that would have caught that and one that did not.
    """
    settings = Settings(
        env=LabEnv.LOCAL,
        installation_role=installation_role,
        llm_provider="fake",
        external_model_enabled=False,
        embedding_profile="hashing-v1",
        malware_scan_required_for_uploads=True,
        malware_scan_required_for_open_access=False,
        retrieval_final_top_k=5,
        **(settings_overrides or {}),
    )
    db = MemoryDatabase() if uow_factory is None else None
    clock = FixedClock(FIXED_START)
    ids = SequentialIds()
    store = object_store if object_store is not None else InMemoryObjectStore(clock=clock)
    search = search_index if search_index is not None else MemorySearchIndex()
    registry = ConnectorRegistryImpl(connectors=dict(connectors or {}))
    provider = llm or FakeLLMProvider()
    embedder = HashingEmbeddingProvider(dimensions=embedding_dimensions)
    scanner = StubScanner()

    context = ServiceContext(
        uow_factory=uow_factory if uow_factory is not None else memory_uow_factory(db),
        object_store=store,
        clock=clock,
        id_factory=ids,
        policy_engine=PolicyEngine(),
        rights_policy=rights_policy or default_open_access_policy(),
        model_transfer_policy=ModelTransferPolicy(),
        settings=settings,
        search_index=search,
        connectors=registry,
        parsers=default_parser_registry(),
        llm_provider=provider,
        embedding_provider=embedder,
        extractor=extractor or FakeExtractor(),
        malware_scanner=scanner,
        installation=InstanceCapabilities(role=installation_role),
    )
    handlers = build_handler_registry(context)
    if context.installation.has(InstallationCapability.PUBLISH_RELEASES):
        from lab_assistant.evaluation.service import EvaluationHandler

        handlers.register(EvaluationHandler(context))
    return Harness(
        settings=settings,
        db=db,
        object_store=store,
        search=search,
        clock=clock,
        ids=ids,
        connectors=registry,
        llm=provider,
        embedder=embedder,
        scanner=scanner,
        context=context,
        handlers=handlers,
        orchestrator=TaskOrchestrator(context, handlers),
        accounts=AccountService(context),
    )


def make_actor(
    *,
    user_id: UUID,
    workspace_id: UUID,
    role: Role = Role.DATA_STEWARD,
    group_ids: tuple[UUID, ...] = (),
    grants: tuple = (),
    extra_capabilities: frozenset[Capability] = frozenset(),
) -> ActorContext:
    """An actor with a synthetic membership, for tests that skip provisioning."""
    return ActorContext(
        user_id=user_id,
        workspace_id=workspace_id,
        membership=Membership(
            id=user_id,
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            status=MembershipStatus.ACTIVE,
            extra_capabilities=extra_capabilities,
        ),
        group_ids=group_ids,
        grants=grants,
    )


DEFAULT_DEMO_WORKS: tuple[tuple[str, str], ...] = (
    (
        "Lipid production in an oleaginous yeast",
        "Strain YL-1 reached a lipid yield of 0.21 g/g substrate at 30 degrees C "
        "on glycerol in a 1 L bioreactor.",
    ),
)


@dataclass(frozen=True, slots=True)
class DemoCorpus:
    """What a seeded, published corpus gives a caller to assert on."""

    release_id: UUID
    served_release_id: UUID
    #: Title -> work id, so a benchmark can name a work without knowing its uuid.
    work_ids: dict[str, UUID]
    rendition_digests: dict[str, str]


async def publish_demo_corpus(
    harness: Harness,
    actor: ActorContext,
    *,
    works: tuple[tuple[str, str], ...] = DEFAULT_DEMO_WORKS,
    name: str = "demo-corpus",
    semantic_version: str = "1.0.0",
    restrict_titles_to_group: dict[str, UUID] | None = None,
) -> DemoCorpus:
    """Seed titled works with ready renditions, then draft, validate, index, publish.

    Shared by ``lab demo``, the benchmark runner, and the tests that need a served
    release, so each of them exercises the real path rather than a simplified one
    that could keep working after the real path broke.

    ``works`` is ``(title, body text)`` pairs. The title is written as a source
    assertion, because a title in this system is something a source *asserted* — it
    is not a column on the work.

    ``restrict_titles_to_group`` restricts a work by amending its **rights
    decision**, which is where restriction belongs: it is a property of the
    material, so every subject is measured against it by one rule rather than by
    whatever grant someone happened to create.
    """
    from lab_assistant.application.acquisition import AcquisitionService
    from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
    from lab_assistant.contracts.canonical import canonical_json
    from lab_assistant.contracts.errors import SourceOutcome
    from lab_assistant.contracts.parsing import (
        NodeKind,
        ParseQualityMetrics,
        RenditionDocument,
        RenditionNode,
    )
    from lab_assistant.contracts.policy import PolicyAction
    from lab_assistant.contracts.stage import StageCommand, StageName
    from lab_assistant.domain.assets import (
        AcquisitionRoute,
        Asset,
        MalwareScanStatus,
        ValidationStatus,
    )
    from lab_assistant.domain.identity import Work, WorkKind
    from lab_assistant.domain.observation import (
        DiscoveryRun,
        DiscoveryRunStatus,
        Predicate,
        SourceAssertion,
        SourceObservation,
        SourceRequest,
    )
    from lab_assistant.domain.renditions import ParseRun, Rendition
    from lab_assistant.domain.scope import ScopeVersion
    from lab_assistant.domain.states import ParseState, RelevanceState

    context = harness.context
    workspace_id = actor.workspace_id
    acquisition = AcquisitionService(context)
    work_ids: dict[str, UUID] = {}
    rendition_digests: dict[str, str] = {}

    # The demo corpus carries provenance rather than a fabricated observation id.
    # A title is something a source asserted, so the assertion needs an
    # observation, which needs a request, a run, and a scope. This seed used to
    # invent the observation id; on a database that enforces its foreign keys
    # that is an insert that cannot succeed.
    scope = ScopeVersion(
        id=context.new_id(),
        workspace_id=workspace_id,
        scope_id=context.new_id(),
        version=1,
        name=f"{name} seed scope",
        scientific_objective="Seeded material for demonstrations and tests.",
        inclusion_criteria=("seeded demonstration material",),
        intended_uses=("demonstration",),
        authored_by=actor.user_id,
        created_at=context.now(),
    )
    discovery_run = DiscoveryRun(
        id=context.new_id(),
        workspace_id=workspace_id,
        scope_version_id=scope.id,
        status=DiscoveryRunStatus.FROZEN,
        run_fingerprint=f"demo_discovery_run:{name}",
        requested_by=actor.user_id,
        started_at=context.now(),
        completed_at=context.now(),
        frozen_at=context.now(),
    )
    source_request = SourceRequest(
        id=context.new_id(),
        workspace_id=workspace_id,
        discovery_run_id=discovery_run.id,
        connector_id="demo-seed",
        connector_version="1.0.0",
        request_fingerprint=f"demo_source_request:{name}",
        method="GET",
        canonical_url_redacted="demo://seed",
        requested_at=context.now(),
        completed_at=context.now(),
        response_status=200,
        record_count=len(works),
        outcome=SourceOutcome.SUCCEEDED,
    )
    async with context.uow_factory() as uow:
        await uow.configs.add_scope_version(scope)
        await uow.observations.add_run(discovery_run)
        await uow.observations.add_request(source_request)
        await uow.commit()

    for index, (title, text) in enumerate(works):
        document = RenditionDocument(
            format="jats",
            nodes=(RenditionNode(node_id="p-1", kind=NodeKind.PARAGRAPH, text=text),),
            quality=ParseQualityMetrics(characters=len(text), section_count=1),
        )
        rendition_object = await context.object_store.put(
            canonical_json(document.model_dump(mode="json")),
            workspace_id=workspace_id,
            object_class="rendition",
            media_type="application/json",
        )
        asset_object = await context.object_store.put(
            text.encode("utf-8"), workspace_id=workspace_id, object_class="asset"
        )
        async with context.uow_factory() as uow:
            work = Work(
                id=context.new_id(),
                workspace_id=workspace_id,
                work_kind=WorkKind.ARTICLE,
                relevance=RelevanceState.INCLUDED,
                created_at=context.now(),
            )
            await uow.works.add(work)
            observation = SourceObservation(
                id=context.new_id(),
                workspace_id=workspace_id,
                source_request_id=source_request.id,
                source_record_locator=f"demo:{index}",
                parser_id="demo-seed-parser",
                parser_version="1",
                schema_version="1.0",
                observed_at=context.now(),
            )
            await uow.observations.add_observation(observation)
            # A title is something a source asserted, not a column on the work.
            await uow.observations.add_assertions(
                [
                    SourceAssertion(
                        id=context.new_id(),
                        workspace_id=workspace_id,
                        source_observation_id=observation.id,
                        subject_hint=f"demo:{index}",
                        predicate=Predicate.TITLE,
                        value_json=title,
                        # Whitespace collapse only, matching what discovery does for
                        # a title. Case is never folded: canonical metadata is what
                        # gets displayed, and `Ura3` is not `URA3`.
                        normalised_value_json=" ".join(title.split()),
                        source_priority_class="biomedical_index",
                        work_id=work.id,
                    )
                ]
            )
            asset = Asset(
                id=context.new_id(),
                workspace_id=workspace_id,
                sha256=asset_object.sha256,
                byte_length=asset_object.byte_length,
                object_id=asset_object.id,
                media_type_detected="application/xml",
                malware_scan_status=MalwareScanStatus.PASSED,
                validation_status=ValidationStatus.PASSED,
                work_id=work.id,
                identity_verified=True,
                created_at=context.now(),
            )
            await register_object(uow, asset_object)
            await uow.assets.add_asset(asset)
            await uow.commit()

        rights = await acquisition.decide_rights(
            actor,
            subject_type="asset",
            subject_id=asset.id,
            licence_expression="cc-by-4.0",
            access_basis="open_access_repository",
            route=AcquisitionRoute.OPEN_ACCESS_API,
            requested_actions=frozenset(
                {
                    PolicyAction.STORE_CONTENT,
                    PolicyAction.PROCESS_LOCAL,
                    PolicyAction.INDEX,
                    PolicyAction.QUERY,
                    PolicyAction.DISPLAY_EXCERPT,
                    PolicyAction.DISPLAY_FULL_CONTENT,
                    # Asked for, and granted only where the harness's policy
                    # grants it: the default policy does not, which is the
                    # point of having to ask.
                    PolicyAction.REPLICATE_CONTENT,
                }
            ),
        )
        async with context.uow_factory() as uow:
            asset.rights_decision_id = rights.id
            await uow.assets.update_asset(asset)
            restricted_group = (restrict_titles_to_group or {}).get(title)
            if restricted_group is not None:
                decision = await uow.policies.get_rights_decision(workspace_id, rights.id)
                assert decision is not None
                await uow.policies.add_rights_decision(
                    decision.model_copy(update={"restricted_to_group_ids": (restricted_group,)})
                )
            parse_run = ParseRun(
                id=context.new_id(),
                workspace_id=workspace_id,
                asset_id=asset.id,
                parser_id="jats-etree",
                parser_version="1.0.0",
                input_sha256=asset.sha256,
                run_fingerprint=f"demo_parse_run:{index}",
                status="succeeded",
            )
            await uow.renditions.add_parse_run(parse_run)
            await register_object(uow, rendition_object)
            await uow.renditions.add_rendition(
                Rendition(
                    id=context.new_id(),
                    workspace_id=workspace_id,
                    parse_run_id=parse_run.id,
                    asset_id=asset.id,
                    work_id=work.id,
                    object_id=rendition_object.id,
                    sha256=rendition_object.sha256,
                    quality_status=ParseState.READY,
                    created_at=context.now(),
                )
            )
            await uow.commit()
        work_ids[title] = work.id
        rendition_digests[title] = rendition_object.sha256

    releases = ReleaseService(context)
    release, _manifest, _diff = await releases.draft(
        actor,
        ReleaseDraftRequest(
            name=name,
            semantic_version=semantic_version,
            embedding_profile_ids=(harness.settings.embedding_profile,),
            # M1: renditions with verified spans, no claim graph.
            include_claims=False,
        ),
    )
    report = await releases.validate(actor, release_id=release.id)
    if not report.passed:
        raise AssertionError(f"demo release failed its gates: {report.failures}")
    await releases.approve(actor, release_id=release.id)
    await harness.handlers.get(StageName.INDEXING).execute(
        StageCommand(
            stage=StageName.INDEXING,
            workspace_id=workspace_id,
            actor_id=actor.user_id,
            inputs={"release_id": str(release.id)},
        )
    )
    alias = await releases.publish(actor, release_id=release.id)
    assert alias.release_id is not None
    return DemoCorpus(
        release_id=release.id,
        served_release_id=alias.release_id,
        work_ids=work_ids,
        rendition_digests=rendition_digests,
    )


def source_page(
    *,
    query: str,
    records: list[dict[str, Any]],
    cursor_in: str | None = None,
    cursor_out: str | None = None,
    declared_total: int | None = None,
) -> FixturePage:
    """Build one recorded connector page."""
    import json

    return FixturePage(
        query=query,
        cursor_in=cursor_in,
        cursor_out=cursor_out,
        body=json.dumps({"records": records}, sort_keys=True).encode("utf-8"),
        declared_total=declared_total if declared_total is not None else len(records),
        exhausted=cursor_out is None,
    )


def record(
    *,
    locator: str,
    identifiers: list[tuple[str, str]],
    title: str | None = None,
    abstract: str | None = None,
    year: int | None = None,
    journal: str | None = None,
    licence: str | None = None,
    full_text_location: str | None = None,
    subject_terms: list[str] | None = None,
    resource_kind: str | None = None,
    authority: str = "biomedical_index",
) -> dict[str, Any]:
    """Build one canonical source record with field-level assertions."""
    assertions: list[dict[str, Any]] = [
        {
            "predicate": "identifier",
            "value": {"scheme": scheme, "value": value},
            "authority": authority,
        }
        for scheme, value in identifiers
    ]
    simple = {
        "title": title,
        "abstract": abstract,
        "year": year,
        "journal": journal,
        "licence": licence,
        "full_text_location": full_text_location,
        "resource_kind": resource_kind,
    }
    for predicate, value in simple.items():
        if value is not None:
            assertions.append({"predicate": predicate, "value": value, "authority": authority})
    for term in subject_terms or []:
        assertions.append({"predicate": "subject_term", "value": term, "authority": authority})
    return {"locator": locator, "assertions": assertions}


def fake_source(connector_id: str, pages: list[FixturePage], **kwargs: Any) -> FakeSourceConnector:
    return FakeSourceConnector(connector_id=connector_id, pages=tuple(pages), **kwargs)


def fake_acquirer(
    assets: dict[str, tuple[bytes, str, str]], **kwargs: Any
) -> FakeAcquisitionConnector:
    return FakeAcquisitionConnector(assets=assets, **kwargs)


def sample_jats(
    *,
    doi: str = "10.1234/example",
    title: str = "Lipid production in an oleaginous yeast",
    include_table: bool = True,
    include_references: bool = True,
    yield_value: str = "0.42",
) -> bytes:
    """A small but realistic JATS document.

    Contains nested sections (so the duplicate-text defect would show), a table
    with two experimental rows (so multi-experiment extraction is exercised), and
    a reference list (so focal-content filtering is exercised).
    """
    table = f"""
      <table-wrap id="tbl1">
        <caption><p>Table 1. Yields by strain.</p></caption>
        <table>
          <thead><tr><th>Strain</th><th>Lipid yield (g/g)</th><th>Temperature</th></tr></thead>
          <tbody>
            <tr><td>YL-A</td><td>{yield_value}</td><td>30 degrees C</td></tr>
            <tr><td>YL-B</td><td>0.28</td><td>28 degrees C</td></tr>
          </tbody>
        </table>
        <table-wrap-foot><p>Values are means of three biological replicates.</p></table-wrap-foot>
      </table-wrap>
"""
    references = """
  <back>
    <ref-list>
      <ref id="r1"><mixed-citation>Smith J. Unrelated lipid yield of 9.99 g/g.
      2019.</mixed-citation></ref>
    </ref-list>
  </back>
"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <journal-meta><journal-title>Journal of Applied Microbiology</journal-title></journal-meta>
    <article-meta>
      <article-id pub-id-type="doi">{doi}</article-id>
      <title-group><article-title>{title}</article-title></title-group>
      <pub-date><year>2025</year></pub-date>
      <license xlink:href="cc-by-4.0"><license-p>Open access.</license-p></license>
      <abstract><p>We report lipid production in two engineered strains.</p></abstract>
    </article-meta>
  </front>
  <body>
    <sec id="s1">
      <title>Introduction</title>
      <p>Oleaginous yeasts accumulate lipids under nitrogen limitation.</p>
      <sec id="s1a">
        <title>Background</title>
        <p>Earlier work established the baseline conditions used here.</p>
      </sec>
    </sec>
    <sec id="s2">
      <title>Results</title>
      <p>Strain YL-A reached a lipid yield of {yield_value} g/g substrate at 30 degrees C.</p>
      {table if include_table else ""}
    </sec>
  </body>
  {references if include_references else ""}
</article>
"""
    return document.encode("utf-8")
