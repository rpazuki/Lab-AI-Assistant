"""Two-workspace negative suite.

The suite actively *attempts* inference: through search, counts, errors, exports,
citation handles, and object ids. "No test found a leak" is only meaningful if the
tests tried to leak.

The second workspace also uses a different scientific domain, which is the plan's
definition of lab-agnostic: the same fixtures create two isolated workspaces with
different domains and prove zero leakage.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from lab_assistant.application.accounts import AccountService
from lab_assistant.application.analytics import AnalyticsService
from lab_assistant.application.chat import ChatService
from lab_assistant.application.export import ExportService
from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import NotFound, PolicyDenied
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.identity import Work, WorkIdentifier, WorkKind
from lab_assistant.domain.observation import (
    DiscoveryRun,
    Predicate,
    SourceAssertion,
    SourceObservation,
    SourceRequest,
)
from lab_assistant.domain.states import RelevanceState
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor

#: Two labs, two domains: yeast lipids and coral microbiome.
LEFT = {
    "slug": "rla-lab",
    "title": "Lipid production in an oleaginous yeast",
    "text": (
        "Strain YL-A reached a lipid yield of 0.42 g/g substrate at 30 degrees C "
        "in the yeast bioreactor."
    ),
    "doi": "10.1234/yeast",
    "query": "lipid yield yeast",
}
RIGHT = {
    "slug": "coral-lab",
    "title": "Symbiont shuffling in reef-building coral",
    "text": (
        "Symbiodinium clade D dominated after the 2024 bleaching event in the "
        "coral holobiont survey."
    ),
    "doi": "10.5678/coral",
    "query": "symbiont clade coral",
}


async def _seed_workspace(harness, spec: dict[str, str]) -> dict[str, Any]:
    """Build one workspace with one work, one rendition, and one published release."""
    from lab_assistant.contracts.canonical import canonical_json
    from lab_assistant.contracts.parsing import (
        NodeKind,
        ParseQualityMetrics,
        RenditionDocument,
        RenditionNode,
    )
    from lab_assistant.domain.assets import Asset, MalwareScanStatus, ValidationStatus
    from lab_assistant.domain.renditions import ParseRun, Rendition
    from lab_assistant.domain.states import ParseState

    context = harness.context
    accounts: AccountService = harness.accounts
    owner = await accounts.create_user(
        email=f"owner@{spec['slug']}.invalid", password="a-long-passphrase"
    )
    workspace, _ = await accounts.create_workspace(
        slug=spec["slug"], display_name=spec["slug"], owner_user_id=owner.id
    )
    admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    grants = []
    for action in (
        PolicyAction.QUERY,
        PolicyAction.VIEW_CONTENT,
        PolicyAction.DISPLAY_EXCERPT,
        PolicyAction.DISPLAY_FULL_CONTENT,
        PolicyAction.EXPORT_METADATA,
        PolicyAction.EXPORT_CLAIMS,
        PolicyAction.EXPORT_EVIDENCE,
    ):
        grants.append(
            await accounts.grant(
                admin,
                subject_type=SubjectType.WORKSPACE,
                subject_id=workspace.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace.id,
                action=action,
                effect=Effect.ALLOW,
                policy_basis="workspace_corpus",
            )
        )
    actor = make_actor(
        user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )

    document = RenditionDocument(
        format="jats",
        nodes=(RenditionNode(node_id="p-1", kind=NodeKind.PARAGRAPH, text=spec["text"]),),
        quality=ParseQualityMetrics(characters=len(spec["text"]), section_count=1),
    )
    stored = await context.object_store.put(
        canonical_json(document.model_dump(mode="json")),
        workspace_id=workspace.id,
        object_class="rendition",
        media_type="application/json",
    )
    asset_object = await context.object_store.put(
        spec["text"].encode(), workspace_id=workspace.id, object_class="asset"
    )

    async with context.uow_factory() as uow:
        work = Work(
            id=context.new_id(),
            workspace_id=workspace.id,
            work_kind=WorkKind.ARTICLE,
            relevance=RelevanceState.INCLUDED,
            created_at=context.now(),
        )
        await uow.works.add(work)
        await uow.works.add_identifier(
            WorkIdentifier(
                id=context.new_id(),
                workspace_id=workspace.id,
                work_id=work.id,
                scheme=IdentifierScheme.DOI,
                value_normalized=spec["doi"],
                value_display=spec["doi"],
            )
        )
        run = DiscoveryRun(
            id=context.new_id(),
            workspace_id=workspace.id,
            scope_version_id=context.new_id(),
            run_fingerprint="discovery_run:seed",
            requested_by=owner.id,
            started_at=context.now(),
        )
        await uow.observations.add_run(run)
        request = SourceRequest(
            id=context.new_id(),
            workspace_id=workspace.id,
            discovery_run_id=run.id,
            connector_id="seed",
            connector_version="1",
            request_fingerprint="source_request:seed",
            method="GET",
            canonical_url_redacted="https://seed.invalid",
            requested_at=context.now(),
        )
        await uow.observations.add_request(request)
        observation = SourceObservation(
            id=context.new_id(),
            workspace_id=workspace.id,
            source_request_id=request.id,
            source_record_locator=f"doi:{spec['doi']}",
            parser_id="seed-record-parser",
            parser_version="1",
            schema_version="1.0",
            observed_at=context.now(),
        )
        await uow.observations.add_observation(observation)
        await uow.observations.add_assertions(
            [
                SourceAssertion(
                    id=context.new_id(),
                    workspace_id=workspace.id,
                    source_observation_id=observation.id,
                    subject_hint=f"doi:{spec['doi']}",
                    predicate=Predicate.TITLE,
                    value_json=spec["title"],
                    work_id=work.id,
                    source_priority_class="doi_registry",
                ),
                SourceAssertion(
                    id=context.new_id(),
                    workspace_id=workspace.id,
                    source_observation_id=observation.id,
                    subject_hint=f"doi:{spec['doi']}",
                    predicate=Predicate.YEAR,
                    value_json=2025,
                    work_id=work.id,
                    source_priority_class="doi_registry",
                ),
            ]
        )
        asset_id = context.new_id()
        asset = Asset(
            id=asset_id,
            workspace_id=workspace.id,
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
        await uow.assets.add_asset(asset)
        await uow.commit()

    # Rights are decided through the service, so the release gate sees a real
    # decision rather than a seeded shortcut.
    from lab_assistant.application.acquisition import AcquisitionService
    from lab_assistant.domain.assets import AcquisitionRoute

    rights = await AcquisitionService(context).decide_rights(
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
                PolicyAction.EXPORT_EVIDENCE,
            }
        ),
    )
    async with context.uow_factory() as uow:
        asset.rights_decision_id = rights.id
        await uow.assets.update_asset(asset)
        parse_run = ParseRun(
            id=context.new_id(),
            workspace_id=workspace.id,
            asset_id=asset.id,
            parser_id="jats-etree",
            parser_version="1.0.0",
            input_sha256=asset.sha256,
            run_fingerprint="parse_run:seed",
            status="succeeded",
        )
        await uow.renditions.add_parse_run(parse_run)
        rendition = Rendition(
            id=context.new_id(),
            workspace_id=workspace.id,
            parse_run_id=parse_run.id,
            asset_id=asset.id,
            work_id=work.id,
            object_id=stored.id,
            sha256=stored.sha256,
            quality_status=ParseState.READY,
            created_at=context.now(),
        )
        await uow.renditions.add_rendition(rendition)
        await uow.commit()

    releases = ReleaseService(context)
    release, _manifest, _diff = await releases.draft(
        actor,
        ReleaseDraftRequest(
            name=f"{spec['slug']}-corpus",
            semantic_version="1.0.0",
            embedding_profile_ids=("hashing-v1",),
            include_claims=False,
        ),
    )
    report = await releases.validate(actor, release_id=release.id)
    assert report.passed, report.failures
    _, tasks = await releases.approve(actor, release_id=release.id)
    await harness.orchestrator.submit_requests(tasks, workspace_id=workspace.id, actor_id=owner.id)
    await harness.run_all_tasks()
    await releases.publish(actor, release_id=release.id)
    return {
        "workspace": workspace,
        "owner": owner,
        "actor": actor,
        "release": release,
        "work": work,
        "rendition": rendition,
        "asset": asset,
    }


@pytest.fixture
async def two_workspaces():
    harness = build_harness()
    left = await _seed_workspace(harness, LEFT)
    right = await _seed_workspace(harness, RIGHT)
    return harness, left, right


class TestSearchIsolation:
    async def test_each_workspace_finds_only_its_own_corpus(self, two_workspaces) -> None:
        harness, left, right = two_workspaces
        retrieval = RetrievalService(harness.context)
        left_result = await retrieval.retrieve(left["actor"], query=LEFT["query"])
        right_result = await retrieval.retrieve(right["actor"], query=RIGHT["query"])
        assert left_result.evidence
        assert right_result.evidence
        assert all("coral" not in block.text.lower() for block in left_result.evidence)
        assert all("yeast" not in block.text.lower() for block in right_result.evidence)

    async def test_a_query_for_the_other_lab_returns_nothing(self, two_workspaces) -> None:
        harness, left, _right = two_workspaces
        retrieval = RetrievalService(harness.context)
        result = await retrieval.retrieve(left["actor"], query=RIGHT["query"])
        assert all("coral" not in block.text.lower() for block in result.evidence)

    async def test_visible_work_ids_never_cross_the_boundary(self, two_workspaces) -> None:
        harness, left, right = two_workspaces
        retrieval = RetrievalService(harness.context)
        left_ids, _ = await retrieval.visible_work_ids(left["actor"])
        right_ids, _ = await retrieval.visible_work_ids(right["actor"])
        assert set(left_ids).isdisjoint(right_ids)
        assert right["work"].id not in left_ids

    async def test_naming_the_other_workspaces_release_does_not_read_it(
        self, two_workspaces
    ) -> None:
        harness, left, right = two_workspaces
        retrieval = RetrievalService(harness.context)
        # A caller who guesses a release id still gets their own workspace key
        # applied, and no cross-workspace projection rows exist under it.
        result = await retrieval.retrieve(
            left["actor"], query=RIGHT["query"], release_id=right["release"].id
        )
        assert result.evidence == []


class TestCountAndAnalyticsIsolation:
    async def test_counts_do_not_include_the_other_workspace(self, two_workspaces) -> None:
        harness, left, right = two_workspaces
        analytics = AnalyticsService(harness.context)
        left_stats = await analytics.corpus_stats(left["actor"], release_id=left["release"].id)
        right_stats = await analytics.corpus_stats(right["actor"], release_id=right["release"].id)
        assert left_stats.works == 1
        assert right_stats.works == 1

    async def test_facets_do_not_leak_titles_or_journals(self, two_workspaces) -> None:
        harness, left, _right = two_workspaces
        analytics = AnalyticsService(harness.context)
        temporal = await analytics.temporal(left["actor"], release_id=left["release"].id)
        assert temporal == [{"year": 2025, "count": 1}]
        subjects = await analytics.subject_terms(left["actor"], release_id=left["release"].id)
        assert all("coral" not in row["term"].lower() for row in subjects)

    async def test_chunk_counts_are_release_scoped(self, two_workspaces) -> None:
        harness, left, right = two_workspaces
        left_chunks = await harness.search.count_chunks(left["workspace"].id, left["release"].id)
        cross = await harness.search.count_chunks(left["workspace"].id, right["release"].id)
        assert left_chunks > 0
        assert cross == 0


class TestRepositoryAndErrorIsolation:
    async def test_reading_another_workspaces_row_by_id_is_refused(self, two_workspaces) -> None:
        harness, left, right = two_workspaces
        async with harness.context.uow_factory() as uow:
            with pytest.raises(AssertionError, match="belongs to workspace"):
                await uow.works.get(left["workspace"].id, right["work"].id)

    async def test_release_lookup_across_workspaces_is_refused(self, two_workspaces) -> None:
        harness, left, right = two_workspaces
        async with harness.context.uow_factory() as uow:
            with pytest.raises(AssertionError, match="belongs to workspace"):
                await uow.releases.get(left["workspace"].id, right["release"].id)

    async def test_error_messages_do_not_disclose_the_other_workspaces_content(
        self, two_workspaces
    ) -> None:
        harness, left, _right = two_workspaces
        chat = ChatService(harness.context)
        with pytest.raises(NotFound) as caught:
            await chat.get_session(left["actor"], session_id=uuid4())
        assert RIGHT["title"] not in str(caught.value)
        assert RIGHT["doi"] not in str(caught.value)

    async def test_object_ids_differ_for_identical_bytes(self, two_workspaces) -> None:
        harness, left, right = two_workspaces
        shared = b"identical payload in both labs"
        left_object = await harness.object_store.put(
            shared, workspace_id=left["workspace"].id, object_class="asset"
        )
        right_object = await harness.object_store.put(
            shared, workspace_id=right["workspace"].id, object_class="asset"
        )
        assert left_object.id != right_object.id
        assert left_object.sha256 == right_object.sha256


class TestExportIsolation:
    async def test_export_contains_only_the_callers_workspace(self, two_workspaces) -> None:
        harness, left, _right = two_workspaces
        exports = ExportService(harness.context)
        package = await exports.build(left["actor"], release_id=left["release"].id)
        blob = b"".join(package.files.values()).decode("utf-8", errors="ignore")
        assert LEFT["title"] in blob
        assert RIGHT["title"] not in blob
        assert RIGHT["doi"] not in blob

    async def test_exporting_another_workspaces_release_is_refused(self, two_workspaces) -> None:
        harness, left, right = two_workspaces
        exports = ExportService(harness.context)
        with pytest.raises((AssertionError, PolicyDenied, NotFound)):
            await exports.build(left["actor"], release_id=right["release"].id)


class TestAnswerIsolation:
    async def test_an_answer_cites_only_the_callers_workspace(self, two_workspaces) -> None:
        harness, left, _right = two_workspaces
        chat = ChatService(harness.context)
        session = await chat.create_session(left["actor"])
        answer = await chat.ask(left["actor"], session_id=session.id, question=LEFT["query"])
        assert answer.message.citations
        assert "coral" not in answer.text.lower()
        assert answer.message.citation_validation_passed

    async def test_a_handle_from_one_workspace_is_meaningless_in_the_other(
        self, two_workspaces
    ) -> None:
        harness, left, right = two_workspaces
        chat = ChatService(harness.context)
        left_session = await chat.create_session(left["actor"])
        left_answer = await chat.ask(
            left["actor"], session_id=left_session.id, question=LEFT["query"]
        )
        handle = left_answer.message.citations[0].handle
        right_session = await chat.create_session(right["actor"])
        right_answer = await chat.ask(
            right["actor"], session_id=right_session.id, question=RIGHT["query"]
        )
        # The other workspace's message cannot be addressed at all, handle or not.
        with pytest.raises((NotFound, AssertionError)):
            await chat.resolve_citation(
                right["actor"], message_id=left_answer.message.id, handle=handle
            )
        assert handle not in {c.handle for c in right_answer.message.citations}


class TestAuditIsolation:
    async def test_audit_chains_are_per_workspace(self, two_workspaces) -> None:
        from lab_assistant.provenance.audit import verify_chain

        harness, left, right = two_workspaces
        async with harness.context.uow_factory() as uow:
            left_events = await uow.audit.list_events(left["workspace"].id, limit=500)
            right_events = await uow.audit.list_events(right["workspace"].id, limit=500)
        assert left_events and right_events
        assert verify_chain(left_events) == (True, ())
        assert verify_chain(right_events) == (True, ())
        assert {event.id for event in left_events}.isdisjoint({event.id for event in right_events})
