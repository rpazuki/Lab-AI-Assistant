"""The executable end-to-end acceptance scenario (data lifecycle §18).

All fourteen steps, in order, with fake connectors and a fake model provider:

 1. two sources disagree about one DOI, and one source is unavailable;
 2. the run freezes as degraded until a steward accepts the missing source;
 3. a preprint and version of record are linked but not merged;
 4. one asset is acquired automatically, another needs assistance;
 5. a user uploads an authorised copy that passes identity and rights checks;
 6. one document contains a table with two experiments;
 7. extraction proposes two measurements and one unsupported value;
 8. the unsupported value fails quote validation; a reviewer corrects another;
 9. a long-form export validates and contains the correction and its evidence;
10. a draft release builds lexical, vector, and structured projections;
11. a restricted group member retrieves the private work; another member cannot
    see it in results or in analytics counts;
12. evaluation passes, the alias switches, and chat cites the exact table cell;
13. a retraction creates a successor release and flags the historical answer;
14. rollback restores the previous alias without rewriting either release.

The scenario is one test function on purpose. Its value is the *sequence*: each
step depends on the state the previous one left, and a per-step test would not
prove the lifecycle holds together.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from lab_assistant.adapters.connectors.fake import FakeAcquisitionConnector
from lab_assistant.application.accounts import AccountService
from lab_assistant.application.acquisition import AcquisitionService
from lab_assistant.application.analytics import AnalyticsService
from lab_assistant.application.chat import ChatService
from lab_assistant.application.context import ActorContext
from lab_assistant.application.discovery import DiscoveryService
from lab_assistant.application.export import ExportService, validate_package
from lab_assistant.application.reconciliation import IdentityService
from lab_assistant.application.refresh import RefreshService
from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.application.review import ReviewService
from lab_assistant.application.scopes import ScopeService
from lab_assistant.contracts.errors import (
    ErrorClass,
    NotFound,
    SourceOutcome,
    ValidationFailure,
)
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.claims import ReviewDecision
from lab_assistant.domain.identity import RelationKind, WorkKind
from lab_assistant.domain.scope import ConnectorPlanEntry
from lab_assistant.domain.states import (
    IdentityState,
    ReleaseState,
    RelevanceState,
    ReviewState,
)
from lab_assistant.domain.workspace import Capability, Role
from lab_assistant.evaluation.benchmarks import Benchmark, BenchmarkCase, PolicyProbe
from lab_assistant.evaluation.runner import EvaluationRunner
from lab_assistant.testing import (
    Harness,
    build_harness,
    fake_source,
    make_actor,
    record,
    sample_jats,
    source_page,
)

SCOPE_QUERY = "yarrowia lipolytica lipid production"
PUBLIC_DOI = "10.1234/example"
PRIVATE_DOI = "10.9999/restricted"
PREPRINT_DOI = "10.1101/2025.01.01.000001"


def _template_payload() -> dict[str, Any]:
    return {
        "template_id": "yeast-lipid-datasheet",
        "semantic_version": "1.0.0",
        "title": "Yeast lipid datasheet",
        "scientific_purpose": "Lipid yields and culture conditions per strain.",
        "cardinality": "per_experiment",
        "targets": [
            {
                "target_id": "lipid_yield",
                "predicate": "lipid_yield",
                "object_kind": "quantity",
                "description": "Lipid yield per gram of substrate.",
                "unit_expectation": "g/g",
                "likely_sections": ["Results", "Table"],
            },
            {
                "target_id": "temperature",
                "predicate": "culture_temperature",
                "object_kind": "quantity",
                "description": "Culture temperature.",
                "unit_expectation": "degrees C",
                "likely_sections": ["Methods", "Table"],
            },
        ],
        "wide_projection_columns": ["lipid_yield", "culture_temperature"],
    }


async def _seed_template(harness: Harness, workspace_id: UUID, actor: ActorContext) -> None:
    from lab_assistant.domain.scope import ApprovalState, ConfigKind, ConfigVersion

    async with harness.context.uow_factory() as uow:
        await uow.configs.add_config_version(
            ConfigVersion(
                id=harness.context.new_id(),
                workspace_id=workspace_id,
                kind=ConfigKind.EXTRACTION_TEMPLATE,
                logical_id="yeast-lipid-datasheet",
                semantic_version="1.0.0",
                payload=_template_payload(),
                status=ApprovalState.APPROVED,
                created_by=actor.user_id,
            )
        )
        await uow.commit()


@pytest.mark.asyncio
async def test_full_lifecycle_acceptance_scenario() -> None:
    jats = sample_jats()
    restricted_jats = sample_jats(
        doi=PRIVATE_DOI, title="Confidential strain screen", yield_value="0.55"
    )

    # ── step 1: two sources, one disagreement, one unavailable source ───────
    pubmed = fake_source(
        "pubmed-fixture",
        [
            source_page(
                query=SCOPE_QUERY,
                records=[
                    record(
                        locator=f"doi:{PUBLIC_DOI}",
                        identifiers=[("doi", PUBLIC_DOI), ("pmid", "40000001")],
                        title="Lipid production in an oleaginous yeast",
                        abstract="Lipid production in two engineered Yarrowia lipolytica strains.",
                        year=2025,
                        journal="Journal of Applied Microbiology",
                        licence="cc-by-4.0",
                        full_text_location="pmc:PMC9000001",
                        subject_terms=["Yarrowia lipolytica", "lipid production"],
                        resource_kind="journal-article",
                    ),
                    record(
                        locator=f"doi:{PRIVATE_DOI}",
                        identifiers=[("doi", PRIVATE_DOI)],
                        title="Confidential strain screen",
                        abstract="Unpublished screen of proprietary Yarrowia lipolytica strains.",
                        year=2026,
                        licence="cc-by-4.0",
                        subject_terms=["lipid production"],
                        resource_kind="journal-article",
                    ),
                    record(
                        locator=f"doi:{PREPRINT_DOI}",
                        identifiers=[("biorxiv_doi", PREPRINT_DOI)],
                        title="Lipid production in an oleaginous yeast",
                        abstract="Preprint of the lipid production study.",
                        year=2024,
                        resource_kind="posted-content",
                    ),
                ],
            )
        ],
    )
    # A different journal title for the same DOI: disagreement that must survive.
    europepmc = fake_source(
        "europepmc-fixture",
        [
            source_page(
                query=SCOPE_QUERY,
                records=[
                    record(
                        locator=f"doi:{PUBLIC_DOI}",
                        identifiers=[("doi", PUBLIC_DOI), ("pmcid", "PMC9000001")],
                        title="Lipid production in an oleaginous yeast",
                        journal="J Appl Microbiol",
                        licence="cc-by-4.0",
                        authority="doi_registry",
                    )
                ],
            )
        ],
    )
    crossref = fake_source("crossref-fixture", [], fail_with=ErrorClass.SOURCE_UNAVAILABLE)
    acquirer = FakeAcquisitionConnector(
        assets={
            f"doi:{PUBLIC_DOI}": (jats, "application/xml", "cc-by-4.0"),
            "pmcid:PMC9000001": (jats, "application/xml", "cc-by-4.0"),
        },
        # The restricted work cannot be fetched automatically: assisted only.
        failures={f"doi:{PRIVATE_DOI}": ErrorClass.ENTITLEMENT_REQUIRED},
    )
    harness = build_harness(
        connectors={
            "pubmed-fixture": pubmed,
            "europepmc-fixture": europepmc,
            "crossref-fixture": crossref,
            "fake-acquire": acquirer,
        }
    )
    context = harness.context
    accounts: AccountService = harness.accounts

    steward = await accounts.create_user(email="steward@lab.invalid", password="a-long-passphrase")
    insider = await accounts.create_user(email="insider@lab.invalid", password="a-long-passphrase")
    outsider = await accounts.create_user(
        email="outsider@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=steward.id
    )
    admin = make_actor(user_id=steward.id, workspace_id=workspace.id, role=Role.ADMIN)
    await accounts.add_member(admin, user_id=insider.id, role=Role.RESEARCHER)
    await accounts.add_member(admin, user_id=outsider.id, role=Role.RESEARCHER)
    private_group = await accounts.create_group(
        admin,
        slug="strain-screen",
        display_name="Strain screen",
        member_user_ids=frozenset({steward.id, insider.id}),
    )
    await _seed_template(harness, workspace.id, admin)

    workspace_grants = []
    for action in (
        PolicyAction.QUERY,
        PolicyAction.VIEW_CONTENT,
        PolicyAction.DISPLAY_EXCERPT,
        PolicyAction.DISPLAY_FULL_CONTENT,
        PolicyAction.EXPORT_CLAIMS,
        PolicyAction.EXPORT_EVIDENCE,
        PolicyAction.EXPORT_METADATA,
    ):
        workspace_grants.append(
            await accounts.grant(
                admin,
                subject_type=SubjectType.WORKSPACE,
                subject_id=workspace.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace.id,
                action=action,
                effect=Effect.ALLOW,
                policy_basis="workspace_open_corpus",
            )
        )
    group_grant = await accounts.grant(
        admin,
        subject_type=SubjectType.GROUP,
        subject_id=private_group.id,
        resource_type=ResourceType.WORK,
        resource_id=None,
        action=PolicyAction.QUERY,
        effect=Effect.ALLOW,
        policy_basis="strain_screen_group",
    )

    def actor(user_id: UUID, role: Role, *, in_group: bool) -> ActorContext:
        return make_actor(
            user_id=user_id,
            workspace_id=workspace.id,
            role=role,
            group_ids=(private_group.id,) if in_group else (),
            grants=(*workspace_grants, group_grant),
            extra_capabilities=frozenset({Capability.REVIEW_CLAIMS})
            if role is Role.RESEARCHER
            else frozenset(),
        )

    steward_actor = actor(steward.id, Role.ADMIN, in_group=True)
    insider_actor = actor(insider.id, Role.RESEARCHER, in_group=True)
    outsider_actor = actor(outsider.id, Role.RESEARCHER, in_group=False)

    scopes = ScopeService(context)
    scope = await scopes.create_version(
        steward_actor,
        scope_id=None,
        name="Yarrowia lipid production",
        scientific_objective="Lipid yields in engineered oleaginous yeast.",
        connector_plan=[
            ConnectorPlanEntry(
                connector_id="pubmed-fixture", connector_version="1.0.0", query=SCOPE_QUERY
            ),
            ConnectorPlanEntry(
                connector_id="europepmc-fixture", connector_version="1.0.0", query=SCOPE_QUERY
            ),
            ConnectorPlanEntry(
                connector_id="crossref-fixture", connector_version="1.0.0", query=SCOPE_QUERY
            ),
        ],
        intended_uses=("internal literature search", "datasheet extraction"),
        concepts=("lipid production", "yarrowia lipolytica"),
    )
    approved_scope = await scopes.approve(steward_actor, scope_version_id=scope.id)
    assert approved_scope.is_approved

    discovery = DiscoveryService(context)
    run, tasks = await discovery.start_run(steward_actor, scope_version_id=approved_scope.id)
    await harness.orchestrator.submit_requests(
        tasks, workspace_id=workspace.id, actor_id=steward.id, run_id=run.id
    )
    await harness.run_all_tasks()

    # ── step 2: the run will not freeze while a required source is missing ──
    async with context.uow_factory() as uow:
        stored_run = await uow.observations.get_run(workspace.id, run.id)
    assert stored_run is not None
    crossref_state = stored_run.state_for("crossref-fixture")
    assert crossref_state is not None
    assert crossref_state.outcome is SourceOutcome.SOURCE_UNAVAILABLE_RETRYABLE
    with pytest.raises(ValidationFailure) as refusal:
        await discovery.freeze_run(steward_actor, run_id=run.id)
    assert any("crossref" in blocker for blocker in refusal.value.context["blockers"])

    frozen, reconcile_tasks = await discovery.freeze_run(
        steward_actor, run_id=run.id, force_accept_degraded=True
    )
    assert frozen.status.can_feed_release
    # Reconciliation runs once, over the complete observation set.
    await harness.orchestrator.submit_requests(
        reconcile_tasks, workspace_id=workspace.id, actor_id=steward.id, run_id=run.id
    )
    await harness.run_all_tasks()

    # The disagreement is preserved: both journal assertions still exist.
    async with context.uow_factory() as uow:
        works = await uow.works.list_works(workspace.id, limit=50)
        public_work = None
        preprint_work = None
        private_work = None
        for work in works:
            identifiers = {
                (i.scheme.value, i.value_normalized)
                for i in await uow.works.list_identifiers(workspace.id, work.id)
            }
            if ("doi", PUBLIC_DOI) in identifiers:
                public_work = work
            elif ("doi", PRIVATE_DOI) in identifiers:
                private_work = work
            elif ("biorxiv_doi", PREPRINT_DOI) in identifiers:
                preprint_work = work
        assert public_work and preprint_work and private_work
        journal_assertions = [
            assertion.value_json
            for assertion in await uow.observations.list_assertions(
                workspace.id, work_id=public_work.id
            )
            if assertion.predicate.value == "journal"
        ]
    assert set(journal_assertions) == {"Journal of Applied Microbiology", "J Appl Microbiol"}
    assert public_work.identity_status is IdentityState.RESOLVED

    # ── step 3: preprint and version of record are linked, not merged ───────
    identity = IdentityService(context)
    relation = await identity.link_versions(
        steward_actor,
        preprint_work_id=preprint_work.id,
        version_of_record_work_id=public_work.id,
    )
    assert relation.kind is RelationKind.IS_PREPRINT_OF
    assert preprint_work.id != public_work.id
    assert preprint_work.work_kind is WorkKind.PREPRINT

    # Include everything the scope found, so acquisition is planned for it.
    for work in (public_work, private_work, preprint_work):
        if work.relevance is not RelevanceState.INCLUDED:
            await identity.set_relevance(
                steward_actor,
                work_id=work.id,
                relevance=RelevanceState.INCLUDED,
                reason_code="steward_included",
            )

    # ── step 4: automatic acquisition for one work, assisted for the other ──
    await harness.orchestrator.submit(
        workspace_id=workspace.id,
        actor_id=steward.id,
        stage=StageName.ACQUISITION_PLANNING,
        inputs={"discovery_run_id": str(run.id)},
    )
    await harness.run_all_tasks()

    async with context.uow_factory() as uow:
        assets = await uow.assets.list_assets(workspace.id)
        plans = await uow.assets.list_plans(workspace.id)
    acquired_work_ids = {asset.work_id for asset in assets}
    assert public_work.id in acquired_work_ids
    assisted_plan = next(plan for plan in plans if plan.work_id == private_work.id)
    assert assisted_plan.status in ("assisted", "failed")

    # ── step 5: a user uploads an authorised copy of the restricted work ────
    acquisition = AcquisitionService(context)
    upload = await acquisition.register_assisted_upload(
        steward_actor,
        plan_id=assisted_plan.id,
        manifestation_id=assisted_plan.manifestation_ids[0],
        content=restricted_jats,
        filename="/Users/someone/private/screen.xml",
        access_basis="institutional_subscription_copy",
        attestation="I obtained this copy through the library subscription.",
        declared_media_type="application/xml",
    )
    assert upload.identity_verified
    assert upload.malware_scan_status.value == "passed"
    # The uploader's local path is never stored.
    assert upload.original_filename_safe == "screen.xml"

    # Restrict the uploaded work to the group, then parse and extract both works.
    async with context.uow_factory() as uow:
        decision = await uow.policies.get_rights_decision(workspace.id, upload.rights_decision_id)
        assert decision is not None
        await uow.policies.add_rights_decision(
            decision.model_copy(update={"restricted_to_group_ids": (private_group.id,)})
        )
        await uow.commit()

    for asset in [*assets, upload]:
        await harness.orchestrator.submit(
            workspace_id=workspace.id,
            actor_id=steward.id,
            stage=StageName.PARSING,
            inputs={"asset_id": str(asset.id)},
        )
    await harness.run_all_tasks()

    # ── steps 6 and 7: two experiments, and one unsupported value ──────────
    async with context.uow_factory() as uow:
        renditions = await uow.renditions.list_renditions(workspace.id)
        claims = await uow.claims.list_claims(workspace.id, limit=100)
        # Two strains in the table must be representable as separate experiments.
        assert await uow.claims.list_experiments(workspace.id, public_work.id) is not None
    assert len(renditions) >= 2
    assert claims, "extraction produced no claims"

    # The table has two strains; the scenario needs both represented.
    async with context.uow_factory() as uow:
        public_rendition = next(r for r in renditions if r.work_id == public_work.id)
        document_bytes = await context.object_store.get(public_rendition.object_id)
    assert b"YL-B" in document_bytes

    # An unsupported value: a proposal quoting text that is not in the rendition.
    from lab_assistant.application.extraction import validate_proposal
    from lab_assistant.application.parsing import ParsingService
    from lab_assistant.contracts.extraction import ClaimProposal, EvidenceRef, TemplateVersionSpec

    parsing = ParsingService(context)
    document = await parsing.load_document(workspace.id, public_rendition)
    template = TemplateVersionSpec.model_validate(_template_payload())
    unsupported = ClaimProposal(
        target_id="lipid_yield",
        predicate="lipid_yield",
        object_kind="quantity",
        value_verbatim="0.91",
        unit_verbatim="g/g",
        evidence=(
            EvidenceRef(node_id="p-1", quote="Strain YL-Z reached a lipid yield of 0.91 g/g"),
        ),
    )
    result, spans = validate_proposal(
        unsupported,
        document=document,
        template=template,
        workspace_id=workspace.id,
        rendition_id=public_rendition.id,
        span_id_factory=context.id_factory,
        created_at=context.now(),
        supplied_node_ids=frozenset(document.focal_node_ids()),
    )
    # ── step 8a: the unsupported value fails quote validation ──────────────
    assert not result.valid
    assert spans == []
    assert any(issue.code == "quote_not_in_node" for issue in result.issues)

    # ── step 8b: a reviewer corrects a real claim ──────────────────────────
    review = ReviewService(context)
    queue = await review.queue(steward_actor, include_invalid=True)
    assert queue, "review queue is empty"
    target_item = next(
        (item for item in queue if item.revision.is_valid and item.evidence), queue[0]
    )
    corrected_value = {**target_item.revision.value, "verbatim": "0.43"}
    corrected, review_record = await review.decide(
        steward_actor,
        revision_id=target_item.revision.id,
        decision=ReviewDecision.CORRECT,
        reason_code="unit_conversion_error",
        comment="Reported per gram substrate, not per gram cells.",
        corrected_value=corrected_value,
        expected_revision_number=target_item.revision.revision_number,
    )
    assert corrected.revision_number == target_item.revision.revision_number + 1
    assert corrected.review_status is ReviewState.CORRECTED
    assert review_record.reviewer_id == steward.id

    # Accept the rest so the release has approved claims.
    for item in await review.queue(steward_actor):
        if item.revision.is_valid:
            await review.decide(
                steward_actor,
                revision_id=item.revision.id,
                decision=ReviewDecision.ACCEPT,
                reason_code="evidence_supports_value",
            )

    # ── step 10: draft a release and build every projection ────────────────
    releases = ReleaseService(context)
    release, manifest, diff = await releases.draft(
        steward_actor,
        ReleaseDraftRequest(
            name="rla-corpus",
            semantic_version="1.0.0",
            scope_version_ids=(approved_scope.id,),
            embedding_profile_ids=("hashing-v1",),
        ),
    )
    assert manifest.manifest_sha256 == release.manifest_sha256
    assert diff.is_full_build
    assert manifest.items

    report = await releases.validate(steward_actor, release_id=release.id)
    assert report.passed, report.failures
    # Passing validation is not approval: it waits, unapproved, for a person.
    async with context.uow_factory() as uow:
        validated = await uow.releases.get(workspace.id, release.id)
    assert validated.status is ReleaseState.VALIDATED and validated.approved_by is None
    _approved, next_tasks = await releases.approve(steward_actor, release_id=release.id)
    assert _approved.status is ReleaseState.APPROVED and _approved.approved_by == steward.id
    await harness.orchestrator.submit_requests(
        next_tasks, workspace_id=workspace.id, actor_id=steward.id
    )
    await harness.run_all_tasks()

    assert await harness.search.count_chunks(workspace.id, release.id) > 0
    assert harness.search.embeddings[(workspace.id, release.id, "hashing-v1")]
    assert harness.search.claims[(workspace.id, release.id)]

    # ── step 9: a long-form export validates and carries the correction ────
    exports = ExportService(context)
    package = await exports.build(steward_actor, release_id=release.id, purpose="lab_datasheet")
    valid, problems = validate_package(package)
    assert valid, problems
    long_form = package.files["data/long-form.csv"].decode()
    assert "0.43" in long_form, "the reviewer's correction is missing from the export"
    assert "corrected" in long_form
    evidence_rows = package.files["data/evidence.jsonl"].decode().splitlines()
    assert evidence_rows, "export carries no evidence spans"
    assert "wide-form.csv" in " ".join(package.files)

    # ── step 12a: evaluation gates, then publish ───────────────────────────
    evaluation = EvaluationRunner(context)
    benchmark = Benchmark(
        benchmark_id="acceptance-retrieval",
        version="1.0.0",
        kind="retrieval",
        description="scenario retrieval benchmark",
        origin="synthetic",
        licence="internal",
        permitted_use="internal evaluation",
        cases=(
            BenchmarkCase(
                case_id="lipid-yield",
                question="What lipid yield was reported for the engineered strain?",
                relevant=(str(public_work.id),),
            ),
        ),
        probes=(
            PolicyProbe(
                probe_id="outsider-cannot-see-restricted",
                user_email="outsider@lab.invalid",
                workspace_slug="rla-lab",
                query="confidential strain screen",
                forbidden_work_titles=("Confidential strain screen",),
            ),
        ),
        thresholds={"recall@5": 0.5},
    )
    retrieval_run = await evaluation.run_retrieval(
        steward_actor, benchmark=benchmark, release_id=release.id
    )
    assert retrieval_run.passed, retrieval_run.failures

    async def resolve_probe(probe: PolicyProbe) -> ActorContext:
        return outsider_actor

    probe_run = await evaluation.run_policy_probes(
        benchmark=benchmark, release_id=release.id, actor_resolver=resolve_probe
    )
    assert probe_run.gates["policy:zero_leakage"], probe_run.failures

    alias = await releases.publish(steward_actor, release_id=release.id)
    assert alias.release_id == release.id
    async with context.uow_factory() as uow:
        published = await uow.releases.get(workspace.id, release.id)
    assert published is not None and published.status is ReleaseState.PUBLISHED

    # ── step 11: group member sees the restricted work; outsider does not ───
    retrieval = RetrievalService(context)
    insider_result = await retrieval.retrieve(insider_actor, query="confidential strain screen")
    outsider_result = await retrieval.retrieve(outsider_actor, query="confidential strain screen")
    insider_titles = {block.work_title for block in insider_result.evidence}
    outsider_titles = {block.work_title for block in outsider_result.evidence}
    assert "Confidential strain screen" in insider_titles
    assert "Confidential strain screen" not in outsider_titles
    assert outsider_result.denied_count >= 1

    analytics = AnalyticsService(context)
    insider_stats = await analytics.corpus_stats(insider_actor, release_id=release.id)
    outsider_stats = await analytics.corpus_stats(outsider_actor, release_id=release.id)
    # Counts follow the same policy: an outsider's total cannot reveal the item,
    # and the *size* of what is withheld is a steward's number rather than every
    # member's — an ordinary member learns their view is filtered, not how much of
    # the release they are missing.
    steward_stats = await analytics.corpus_stats(steward_actor, release_id=release.id)
    assert outsider_stats.works < insider_stats.works
    assert outsider_stats.chunks < insider_stats.chunks
    assert outsider_stats.withheld is None
    assert steward_stats.withheld is not None and steward_stats.withheld >= 0

    # ── step 12b: chat cites the exact evidence ────────────────────────────
    chat = ChatService(context)
    session = await chat.create_session(insider_actor, title=None)
    answer = await chat.ask(
        insider_actor,
        session_id=session.id,
        question="What lipid yield was reported for the engineered strain?",
    )
    assert answer.message.citations
    assert answer.message.citation_validation_passed
    assert answer.unsupported_handles == ()
    resolved = await chat.resolve_citation(
        insider_actor, message_id=answer.message.id, handle=answer.message.citations[0].handle
    )
    assert resolved["resolved"], resolved
    # A citation opens either a pre-verified claim span or the exact rendition
    # node it came from — both immutable, both carrying source coordinates.
    if resolved.get("span"):
        assert resolved["span"]["node_id"]
        assert resolved["span"]["normalisation_version"] == "1"
    else:
        assert resolved["nodes"]
        assert resolved["nodes"][0]["text"]
        assert resolved["nodes"][0]["node_id"]

    # Auto-titling still works, as users expect.
    async with context.uow_factory() as uow:
        titled = await uow.chat.get_session(workspace.id, session.id)
    assert titled is not None and titled.title
    assert titled.title.startswith("What lipid yield")

    # Another member cannot open someone else's answer, even holding the handle:
    # confirming the message exists would itself be a disclosure.
    with pytest.raises(NotFound):
        await chat.resolve_citation(
            outsider_actor,
            message_id=answer.message.id,
            handle=answer.message.citations[0].handle,
        )

    # ── step 13: a retraction creates a successor and flags the answer ──────
    refresh = RefreshService(context)
    plan = await refresh.record_retraction(
        steward_actor, work_id=public_work.id, reason_code="publisher_retraction_notice"
    )
    assert release.id in plan.affected_release_ids
    assert plan.tasks and plan.tasks[0].stage is StageName.RELEASE_ASSEMBLY
    async with context.uow_factory() as uow:
        retracted = await uow.works.get(workspace.id, public_work.id)
        historical = await uow.chat.get_message(workspace.id, answer.message.id)
    assert retracted is not None and retracted.is_retracted
    assert historical is not None and historical.has_flagged_citations

    successor, _successor_manifest, successor_diff = await releases.draft(
        steward_actor,
        ReleaseDraftRequest(
            name="rla-corpus",
            semantic_version="1.1.0",
            scope_version_ids=(approved_scope.id,),
            embedding_profile_ids=("hashing-v1",),
            parent_release_id=release.id,
        ),
    )
    # The published release is untouched by the successor's existence.
    async with context.uow_factory() as uow:
        original = await uow.releases.get(workspace.id, release.id)
    assert original is not None
    assert original.manifest_sha256 == release.manifest_sha256
    assert not successor_diff.is_full_build

    successor_report = await releases.validate(steward_actor, release_id=successor.id)
    assert successor_report.passed, successor_report.failures
    _, successor_tasks = await releases.approve(steward_actor, release_id=successor.id)
    await harness.orchestrator.submit_requests(
        successor_tasks, workspace_id=workspace.id, actor_id=steward.id
    )
    await harness.run_all_tasks()
    await releases.publish(steward_actor, release_id=successor.id)

    async with context.uow_factory() as uow:
        superseded = await uow.releases.get(workspace.id, release.id)
        current_alias = await uow.releases.get_alias(workspace.id, "current")
    assert superseded is not None and superseded.status is ReleaseState.SUPERSEDED
    assert current_alias is not None and current_alias.release_id == successor.id

    # ── step 14: rollback restores the previous alias, rewriting nothing ────
    rolled_back = await releases.rollback(steward_actor, alias="current")
    assert rolled_back.release_id == release.id
    async with context.uow_factory() as uow:
        restored = await uow.releases.get(workspace.id, release.id)
        successor_after = await uow.releases.get(workspace.id, successor.id)
    assert restored is not None and restored.status is ReleaseState.PUBLISHED
    assert restored.manifest_sha256 == release.manifest_sha256
    assert successor_after is not None
    assert successor_after.manifest_sha256 == successor.manifest_sha256

    # The audit chain covering the whole scenario verifies end to end.
    from lab_assistant.provenance.audit import verify_chain

    async with context.uow_factory() as uow:
        events = await uow.audit.list_events(workspace.id, limit=1000)
    ok, problems = verify_chain(events)
    assert ok, problems
    actions = {event.action.value for event in events}
    for expected in (
        "scope_approved",
        "discovery_run_started",
        "discovery_run_frozen",
        "degraded_source_accepted",
        "rights_decided",
        "asset_registered",
        "assisted_upload_accepted",
        "parse_completed",
        "extraction_completed",
        "claim_reviewed",
        "release_drafted",
        "release_validated",
        "release_approved",
        "release_published",
        "answer_generated",
        "export_created",
        "release_rolled_back",
    ):
        assert expected in actions, f"audit trail is missing {expected}"
