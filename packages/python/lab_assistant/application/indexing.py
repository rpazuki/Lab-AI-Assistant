"""Stage 10: ingestion into serving indexes.

This is the *only* stage called ingestion. It never fetches anything.

The build is incremental by construction: an item whose content digest matches its
parent release's item copies its projection rows instead of recomputing them. A
release whose diff is N items therefore costs time proportional to N, not to the
whole corpus — the E10 acceptance criterion, proved by
``tests/integration/test_incremental_projection.py``.

A failed build never switches an alias. Projections are marked ready, and
publication is a separate, human-approved step.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import UUID

from lab_assistant.application.context import ServiceContext
from lab_assistant.application.parsing import ParsingService
from lab_assistant.contracts.errors import NotFound
from lab_assistant.contracts.stage import StageCommand, StageName, StageResult, StageStatus
from lab_assistant.domain.chunking import ChunkerProfile, chunk_rendition
from lab_assistant.domain.states import ReleaseState
from lab_assistant.provenance.fingerprints import projection_fingerprint, task_fingerprint

__all__ = ["IndexingHandler", "ProjectionOutcome"]

IMPLEMENTATION_VERSION = "indexing/1"


@dataclass(frozen=True)
class ProjectionOutcome:
    chunks_written: int
    chunks_copied: int
    embeddings_written: int
    embeddings_copied: int
    claim_rows: int
    profile_ids: tuple[str, ...]

    @property
    def total_chunks(self) -> int:
        return self.chunks_written + self.chunks_copied


class IndexingHandler:
    stage_name = StageName.INDEXING
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._parsing = ParsingService(context)

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        context = self._context
        workspace_id = command.workspace_id
        release_id = UUID(str(command.inputs["release_id"]))
        index = context.require_search_index()
        embedder = context.embedding_provider

        async with context.uow_factory() as uow:
            release = await uow.releases.get(workspace_id, release_id)
            if release is None:
                raise NotFound(f"release {release_id} not found")
            if release.status not in (ReleaseState.APPROVED, ReleaseState.PUBLISHING):
                return StageResult(
                    stage=StageName.INDEXING,
                    status=StageStatus.BLOCKED,
                    error_detail=(
                        f"release is {release.status.value}; only an approved release is indexed"
                    ),
                )
            items = await uow.releases.list_items(workspace_id, release_id)
            parent_items = (
                await uow.releases.list_items(workspace_id, release.parent_release_id)
                if release.parent_release_id
                else []
            )

        parent_digests = {
            item.record.work_id: item.record.content_digest() for item in parent_items
        }
        profile = ChunkerProfile()
        chunks_written = 0
        chunks_copied = 0
        embeddings_written = 0
        embeddings_copied = 0
        claim_rows = 0
        reused_work_ids: list[UUID] = []
        rebuilt_work_ids: list[UUID] = []
        rows: list[dict] = []
        claim_projection: list[dict] = []

        for item in items:
            record = item.record
            digest = record.content_digest()
            if release.parent_release_id and parent_digests.get(record.work_id) == digest:
                reused_work_ids.append(record.work_id)
                continue
            rebuilt_work_ids.append(record.work_id)
            if record.rendition_id is None:
                continue
            async with context.uow_factory() as uow:
                rendition = await uow.renditions.get_rendition(workspace_id, record.rendition_id)
            if rendition is None:
                continue
            document = await self._parsing.load_document(workspace_id, rendition)
            for chunk in chunk_rendition(
                document,
                release_item_digest=digest,
                rendition_digest=rendition.sha256,
                profile=profile,
            ):
                rows.append(
                    {
                        "chunk_key": chunk.chunk_key,
                        "release_id": str(release_id),
                        "work_id": str(record.work_id),
                        "rendition_id": str(rendition.id),
                        "node_ids": list(chunk.node_ids),
                        "content": chunk.content,
                        "content_sha256": chunk.content_sha256,
                        "chunk_type": chunk.chunk_type,
                        "section_label": chunk.section_label,
                        "page": chunk.page,
                        "token_count": chunk.token_count,
                        "chunker_id": profile.chunker_id,
                        "chunker_version": profile.chunker_version,
                        "access_policy_snapshot_id": (
                            str(record.access_policy_snapshot_id)
                            if record.access_policy_snapshot_id
                            else None
                        ),
                        "metadata": dict(chunk.metadata),
                    }
                )

        if release.parent_release_id and reused_work_ids:
            chunks_copied = await index.copy_chunks(
                workspace_id,
                from_release_id=release.parent_release_id,
                to_release_id=release_id,
                work_ids=reused_work_ids,
            )

        if rows:
            chunks_written = await index.write_chunks(workspace_id, release_id, rows)

        profile_ids: list[str] = []
        if embedder is not None:
            embedding_profile = embedder.profile
            profile_ids.append(embedding_profile.profile_id)
            if release.parent_release_id and reused_work_ids:
                embeddings_copied = await index.copy_embeddings(
                    workspace_id,
                    profile_id=embedding_profile.profile_id,
                    from_release_id=release.parent_release_id,
                    to_release_id=release_id,
                    work_ids=reused_work_ids,
                )
            if rows:
                batch = context.settings.embedding_batch_size
                vectors: list[dict] = []
                for start in range(0, len(rows), batch):
                    window = rows[start : start + batch]
                    embedded = await embedder.embed_documents([row["content"] for row in window])
                    for row, vector in zip(window, embedded, strict=True):
                        vectors.append(
                            {
                                "chunk_key": row["chunk_key"],
                                "release_id": str(release_id),
                                "work_id": row["work_id"],
                                "profile_id": embedding_profile.profile_id,
                                "vector": list(vector),
                            }
                        )
                embeddings_written = await index.write_embeddings(
                    workspace_id,
                    release_id,
                    profile_id=embedding_profile.profile_id,
                    rows=vectors,
                )

        # Structured claim projection: approved revisions only.
        async with context.uow_factory() as uow:
            for item in items:
                for revision_id in item.record.claim_revision_ids:
                    revision = await uow.claims.get_revision(workspace_id, revision_id)
                    if revision is None:
                        continue
                    claim = await uow.claims.get_claim(workspace_id, revision.claim_id)
                    if claim is None:
                        continue
                    claim_projection.append(
                        {
                            "release_id": str(release_id),
                            "work_id": str(claim.work_id),
                            "claim_id": str(claim.id),
                            "revision_id": str(revision.id),
                            "experiment_id": str(claim.experiment_id)
                            if claim.experiment_id
                            else None,
                            "predicate": claim.predicate,
                            "subject": claim.subject_label,
                            "object_kind": claim.object_kind,
                            "value": revision.value,
                            "unit": revision.value.get("unit_canonical")
                            or revision.value.get("unit_verbatim"),
                            "review_status": revision.review_status.value,
                            "abstention_reason": revision.abstention_reason.value,
                            "evidence_span_ids": [str(i) for i in revision.evidence_span_ids],
                            "access_policy_snapshot_id": (
                                str(item.record.access_policy_snapshot_id)
                                if item.record.access_policy_snapshot_id
                                else None
                            ),
                        }
                    )
        if claim_projection:
            claim_rows = await index.write_claim_projection(
                workspace_id, release_id, claim_projection
            )

        total_chunks = await index.count_chunks(workspace_id, release_id)
        membership_ok = total_chunks == chunks_written + chunks_copied

        async with context.uow_factory() as uow:
            release = await uow.releases.get(workspace_id, release_id)
            assert release is not None
            release.projection_ready_profiles = tuple(
                sorted(set(release.projection_ready_profiles) | set(profile_ids))
            )
            release.gate_results = {
                **release.gate_results,
                "projection_membership": membership_ok,
                "projection_built": True,
            }
            release.version += 1
            await uow.releases.update(release)
            await uow.commit()

        outcome = ProjectionOutcome(
            chunks_written=chunks_written,
            chunks_copied=chunks_copied,
            embeddings_written=embeddings_written,
            embeddings_copied=embeddings_copied,
            claim_rows=claim_rows,
            profile_ids=tuple(profile_ids),
        )
        return StageResult(
            stage=StageName.INDEXING,
            status=StageStatus.SUCCEEDED if membership_ok else StageStatus.DEGRADED,
            metrics={
                "chunks_written": chunks_written,
                "chunks_copied": chunks_copied,
                "embeddings_written": embeddings_written,
                "embeddings_copied": embeddings_copied,
                "claim_rows": claim_rows,
                "reused_items": len(reused_work_ids),
                "rebuilt_items": len(rebuilt_work_ids),
                "total_chunks": total_chunks,
                "projection_fingerprint": projection_fingerprint(
                    release_id=release_id,
                    chunker_key=profile.logical_key,
                    embedding_profile=embedder.profile if embedder else None,
                ),
                "outcome": asdict(outcome),
            },
            quality_gates={
                "projection_membership": membership_ok,
                "incremental_reuse": (
                    not release.parent_release_id or bool(reused_work_ids) or not parent_digests
                ),
            },
            # Evaluation is not scheduled from here. Which benchmark gates a
            # release, and at what threshold, is a scientific decision made by a
            # steward against a named benchmark version — not a side effect of a
            # projection build.
        )
