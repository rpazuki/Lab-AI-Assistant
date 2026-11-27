"""E10 acceptance: a release builds in proportion to its diff, not the corpus.

Without this, publish time grows with corpus size rather than with change size and
frequent releases become impractical (scale §5). The proof compares a full build
against a one-item-diff build over the same corpus and counts recomputed work.
"""

from __future__ import annotations

from typing import Any

import pytest

from lab_assistant.application.releases import ReleaseDraftRequest, ReleaseService
from lab_assistant.contracts.canonical import canonical_json
from lab_assistant.contracts.parsing import (
    NodeKind,
    ParseQualityMetrics,
    RenditionDocument,
    RenditionNode,
)
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.contracts.stage import StageCommand, StageName
from lab_assistant.domain.assets import (
    AcquisitionRoute,
    Asset,
    MalwareScanStatus,
    ValidationStatus,
)
from lab_assistant.domain.identity import Work, WorkKind
from lab_assistant.domain.renditions import ParseRun, Rendition
from lab_assistant.domain.states import ParseState, RelevanceState
from lab_assistant.domain.workspace import Role
from lab_assistant.testing import build_harness, make_actor

CORPUS_SIZE = 12


async def _seed_corpus(harness, actor, workspace_id) -> list[Rendition]:
    """Twelve works, each with one rendition."""
    from lab_assistant.application.acquisition import AcquisitionService

    context = harness.context
    renditions: list[Rendition] = []
    acquisition = AcquisitionService(context)
    for index in range(CORPUS_SIZE):
        text = (
            f"Strain YL-{index} reached a lipid yield of 0.{index:02d} g/g substrate "
            f"at 30 degrees C in run {index}."
        )
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
            text.encode(), workspace_id=workspace_id, object_class="asset"
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
                }
            ),
        )
        async with context.uow_factory() as uow:
            asset.rights_decision_id = rights.id
            await uow.assets.update_asset(asset)
            parse_run = ParseRun(
                id=context.new_id(),
                workspace_id=workspace_id,
                asset_id=asset.id,
                parser_id="jats-etree",
                parser_version="1.0.0",
                input_sha256=asset.sha256,
                run_fingerprint=f"parse_run:{index}",
                status="succeeded",
            )
            await uow.renditions.add_parse_run(parse_run)
            rendition = Rendition(
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
            await uow.renditions.add_rendition(rendition)
            await uow.commit()
        renditions.append(rendition)
    return renditions


@pytest.fixture
async def corpus() -> dict[str, Any]:
    harness = build_harness()
    owner = await harness.accounts.create_user(
        email="steward@lab.invalid", password="a-long-passphrase"
    )
    workspace, _ = await harness.accounts.create_workspace(
        slug="rla-lab", display_name="RLA Lab", owner_user_id=owner.id
    )
    admin = make_actor(user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN)
    grants = [
        await harness.accounts.grant(
            admin,
            subject_type=SubjectType.WORKSPACE,
            subject_id=workspace.id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace.id,
            action=action,
            effect=Effect.ALLOW,
            policy_basis="workspace_corpus",
        )
        for action in (PolicyAction.QUERY, PolicyAction.VIEW_CONTENT, PolicyAction.INDEX)
    ]
    actor = make_actor(
        user_id=owner.id, workspace_id=workspace.id, role=Role.ADMIN, grants=tuple(grants)
    )
    renditions = await _seed_corpus(harness, actor, workspace.id)
    return {
        "harness": harness,
        "actor": actor,
        "workspace": workspace,
        "owner": owner,
        "renditions": renditions,
    }


async def _build(harness, actor, request: ReleaseDraftRequest) -> dict[str, Any]:
    releases = ReleaseService(harness.context)
    release, manifest, diff = await releases.draft(actor, request)
    report = await releases.validate(actor, release_id=release.id)
    assert report.passed, report.failures
    await releases.approve(actor, release_id=release.id)
    handler = harness.handlers.get(StageName.INDEXING)
    result = await handler.execute(
        StageCommand(
            stage=StageName.INDEXING,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            inputs={"release_id": str(release.id)},
        )
    )
    return {"release": release, "manifest": manifest, "diff": diff, "result": result}


class TestIncrementalProjection:
    async def test_one_item_diff_recomputes_one_item(self, corpus) -> None:
        harness, actor = corpus["harness"], corpus["actor"]
        first = await _build(
            harness,
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.0.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
            ),
        )
        full_metrics = first["result"].metrics
        assert full_metrics["rebuilt_items"] == CORPUS_SIZE
        assert full_metrics["chunks_written"] > 0
        assert full_metrics["chunks_copied"] == 0

        # Change exactly one work: a new rendition for one asset.
        changed = corpus["renditions"][3]
        text = "Strain YL-3 reached a lipid yield of 0.99 g/g substrate after reparse."
        document = RenditionDocument(
            format="jats",
            nodes=(RenditionNode(node_id="p-1", kind=NodeKind.PARAGRAPH, text=text),),
            quality=ParseQualityMetrics(characters=len(text), section_count=1),
        )
        stored = await harness.context.object_store.put(
            canonical_json(document.model_dump(mode="json")),
            workspace_id=actor.workspace_id,
            object_class="rendition",
            media_type="application/json",
        )
        async with harness.context.uow_factory() as uow:
            live = await uow.renditions.get_rendition(actor.workspace_id, changed.id)
            assert live is not None
            live.object_id = stored.id
            live.sha256 = stored.sha256
            live.version += 1
            await uow.renditions.update_rendition(live)
            await uow.commit()

        second = await _build(
            harness,
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.1.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
                parent_release_id=first["release"].id,
            ),
        )
        diff = second["diff"]
        metrics = second["result"].metrics

        assert len(diff.changed_work_ids) == 1
        assert len(diff.unchanged_work_ids) == CORPUS_SIZE - 1
        # The proof: recomputation tracks the diff, copying the rest by digest.
        assert metrics["rebuilt_items"] == 1
        assert metrics["reused_items"] == CORPUS_SIZE - 1
        assert metrics["chunks_copied"] >= CORPUS_SIZE - 1
        assert metrics["chunks_written"] < full_metrics["chunks_written"]
        assert metrics["embeddings_copied"] >= CORPUS_SIZE - 1

    async def test_the_successor_projection_is_complete(self, corpus) -> None:
        harness, actor = corpus["harness"], corpus["actor"]
        first = await _build(
            harness,
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.0.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
            ),
        )
        second = await _build(
            harness,
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.1.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
                parent_release_id=first["release"].id,
            ),
        )
        # A copied projection is a complete projection: same chunk count, and the
        # membership gate agrees.
        first_count = await harness.search.count_chunks(actor.workspace_id, first["release"].id)
        second_count = await harness.search.count_chunks(actor.workspace_id, second["release"].id)
        assert second_count == first_count
        assert second["result"].quality_gates["projection_membership"]

    async def test_rebuilding_the_same_release_is_deterministic(self, corpus) -> None:
        harness, actor = corpus["harness"], corpus["actor"]
        built = await _build(
            harness,
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.0.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
            ),
        )
        keys_before = set(harness.search.chunks[(actor.workspace_id, built["release"].id)].keys())
        handler = harness.handlers.get(StageName.INDEXING)
        await handler.execute(
            StageCommand(
                stage=StageName.INDEXING,
                workspace_id=actor.workspace_id,
                actor_id=actor.user_id,
                inputs={"release_id": str(built["release"].id)},
            )
        )
        keys_after = set(harness.search.chunks[(actor.workspace_id, built["release"].id)].keys())
        # Chunk identity is derived from content and profile, so a rerun is a no-op.
        assert keys_before == keys_after

    async def test_a_failed_build_never_moves_the_alias(self, corpus) -> None:
        harness, actor = corpus["harness"], corpus["actor"]
        releases = ReleaseService(harness.context)
        built = await _build(
            harness,
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.0.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
            ),
        )
        await releases.publish(actor, release_id=built["release"].id)

        # A second release that never built its projection cannot take the alias.
        second, _manifest, _diff = await releases.draft(
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.2.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
                parent_release_id=built["release"].id,
            ),
        )
        await releases.validate(actor, release_id=second.id)
        await releases.approve(actor, release_id=second.id)
        from lab_assistant.contracts.errors import ValidationFailure

        with pytest.raises(ValidationFailure):
            await releases.publish(actor, release_id=second.id)
        alias_release = await releases.active_release_id(actor.workspace_id)
        assert alias_release == built["release"].id


class TestWhatAReleaseChanges:
    """What a draft adds and drops against the release it follows, and why."""

    async def test_a_successor_names_its_parent_and_why_papers_dropped(self, corpus) -> None:
        harness, actor = corpus["harness"], corpus["actor"]
        releases = ReleaseService(harness.context)
        first, _, _ = await releases.draft(
            actor, ReleaseDraftRequest(name="corpus", semantic_version="1.0.0")
        )
        excluded, merged, keeper = (corpus["renditions"][i].work_id for i in (1, 2, 3))
        async with harness.context.uow_factory() as uow:
            work = await uow.works.get(actor.workspace_id, excluded)
            work.relevance = RelevanceState.EXCLUDED
            await uow.works.update(work)
            work = await uow.works.get(actor.workspace_id, merged)
            work.merged_into_work_id = keeper
            await uow.works.update(work)
            await uow.commit()
        second, _, _ = await releases.draft(
            actor,
            ReleaseDraftRequest(
                name="corpus", semantic_version="1.1.0", parent_release_id=first.id
            ),
        )

        changes = await releases.changes(actor, release_id=second.id)

        assert changes["parent"] == {
            "id": str(first.id),
            "name": "corpus",
            "semantic_version": "1.0.0",
        }
        assert (changes["items"], changes["added"], changes["changed"]) == (
            CORPUS_SIZE - 2,
            0,
            0,
        )
        assert (changes["removed"], changes["unchanged"]) == (2, CORPUS_SIZE - 2)
        why = {row["work_id"]: row["why"] for row in changes["papers"]["removed"]}
        assert why == {str(excluded): "not_included", str(merged): "merged"}

        # Included again, it comes back as added, and is listed.
        async with harness.context.uow_factory() as uow:
            work = await uow.works.get(actor.workspace_id, excluded)
            work.relevance = RelevanceState.INCLUDED
            await uow.works.update(work)
            await uow.commit()
        third, _, _ = await releases.draft(
            actor,
            ReleaseDraftRequest(
                name="corpus", semantic_version="1.2.0", parent_release_id=second.id
            ),
        )
        changes = await releases.changes(actor, release_id=third.id)
        assert changes["added"] == 1
        assert [row["work_id"] for row in changes["papers"]["added"]] == [str(excluded)]
        assert "why" not in changes["papers"]["added"][0]

    async def test_a_release_with_no_parent_is_all_added_and_lists_none(self, corpus) -> None:
        harness, actor = corpus["harness"], corpus["actor"]
        releases = ReleaseService(harness.context)
        first, _, _ = await releases.draft(
            actor, ReleaseDraftRequest(name="corpus", semantic_version="1.0.0")
        )

        changes = await releases.changes(actor, release_id=first.id)

        assert changes["parent"] is None
        assert (changes["items"], changes["added"], changes["removed"]) == (
            CORPUS_SIZE,
            CORPUS_SIZE,
            0,
        )
        # A full build lists no papers: every one of them is "added".
        assert changes["papers"] == {"added": [], "changed": [], "removed": []}


class TestRebuildingAFailedIndex:
    """An index whose chunk count failed its check is built again, not abandoned.

    On the deployment one paper repeated a passage, the chunker emitted the same
    chunk twice, and the build counted one more chunk than it stored. The release
    was approved and indexed but could never be published.
    """

    async def test_a_repeated_passage_is_one_chunk(self) -> None:
        from lab_assistant.domain.chunking import chunk_rendition

        # Longer than half a window and repeated: each window is the same text.
        sentence = "Lipid accumulation in Yarrowia lipolytica rose under nitrogen limit " * 30
        text = " ".join([sentence.strip() + "."] * 4)
        document = RenditionDocument(
            format="pdf",
            nodes=(RenditionNode(node_id="p-1", kind=NodeKind.PARAGRAPH, text=text),),
        )

        chunks = chunk_rendition(document, release_item_digest="item", rendition_digest="r")

        assert len(chunks) == 1
        assert len({chunk.chunk_key for chunk in chunks}) == len(chunks)

    async def test_a_failed_check_is_rebuilt_and_then_publishes(self, corpus) -> None:
        from lab_assistant.contracts.errors import StateTransitionError

        harness, actor = corpus["harness"], corpus["actor"]
        releases = ReleaseService(harness.context)
        built = await _build(
            harness,
            actor,
            ReleaseDraftRequest(
                name="corpus",
                semantic_version="1.0.0",
                embedding_profile_ids=("hashing-v1",),
                include_claims=False,
            ),
        )
        release_id = built["release"].id
        # A healthy index has nothing to rebuild.
        with pytest.raises(StateTransitionError):
            await releases.reindex(actor, release_id=release_id)

        async with harness.context.uow_factory() as uow:
            stored = await uow.releases.get(actor.workspace_id, release_id)
            assert stored is not None
            stored.gate_results = {**stored.gate_results, "projection_membership": False}
            stored.version += 1
            await uow.releases.update(stored)
            await uow.commit()

        release, tasks = await releases.reindex(actor, release_id=release_id)

        # It reads as being indexed again, and publishing waits for the rebuild.
        assert release.projection_ready_profiles == ()
        assert "projection_membership" not in release.gate_results
        assert [task.stage for task in tasks] == [StageName.INDEXING]
        assert tasks[0].idempotency_key is not None
        with pytest.raises(StateTransitionError):
            await releases.reindex(actor, release_id=release_id)

        result = await harness.handlers.get(StageName.INDEXING).execute(
            StageCommand(
                stage=StageName.INDEXING,
                workspace_id=actor.workspace_id,
                actor_id=actor.user_id,
                inputs=dict(tasks[0].inputs),
            )
        )

        assert result.quality_gates["projection_membership"] is True
        await releases.publish(actor, release_id=release_id)
        assert await releases.active_release_id(actor.workspace_id) == release_id
