"""Importing a fetched snapshot into a reader's own database (phase 3).

The success definition of the whole shared-archive plan lands here: a fresh
reader, with no corpus and a GET-only archive credential, imports an approved
snapshot and can search, ask and open citations without downloading from a
scholarly source, reparsing, re-extracting or re-embedding anything. So this
module is notable mostly for what it never calls — no connector, no parser, no
extractor, no document embedder. It writes rows a signed, validated snapshot
carried, and nothing it had to work out.

**Additive, under the producer's ids** (ADR-0029). Releases, renditions, spans
and claim revisions are immutable by id, so a second snapshot of the same
library adds rows and never rewrites one; the serving alias moves last, in the
same transaction as the rows, so a request sees the old release or the new one
and never half of each. What does change after publication — permission — is
not rewritten from the catalog at all: the reader's rights rows are the
*narrowed* decisions (`domain/archive_policy.narrow`), the snapshot's frozen
decision intersected with the current policy checkpoint.

**Checked before it is trusted, again.** The directory is validated with its
payloads, its signature and its policy before anything is written, and then
every evidence span is re-verified against the rendition bytes it points into:
a span row means "this text was there", and the reader checks that for itself
rather than taking a manifest's word. A span that does not verify refuses the
import.

**Never backwards.** The reader records each import, and refuses a snapshot
from an older head than the one it serves, a different snapshot claiming the
same head, and a policy checkpoint older than the one it applied (S65).

**Groups are mapped, not trusted.** A restriction names the producer's group
ids, which mean nothing here. ``group_map`` translates the ones an operator has
mapped to local groups; one that is not mapped stays as it is, matches no local
group, and so admits nobody (S22).

**Vectors are reused only under the same profile.** The embedding profile digest
must match this installation's query embedder; dimension alone is not
compatibility (S36). A mismatch imports the chunks without the vectors — search
is lexical, and the report says so — rather than ranking queries against vectors
from a different model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from lab_assistant.application.archive_catalog import INVENTORY_KIND
from lab_assistant.application.archive_fetch import FETCHED_HEAD_NAME
from lab_assistant.application.archive_validate import validate_snapshot
from lab_assistant.application.context import ServiceContext
from lab_assistant.application.objects import register_object
from lab_assistant.contracts.archive import ArtifactRef, content_location
from lab_assistant.contracts.archive_catalog import RecordKind, record_for
from lab_assistant.contracts.archive_import import ImportRecord, ImportState
from lab_assistant.contracts.archive_policy import POLICY_DIRECTORY, PolicyCheckpoint
from lab_assistant.contracts.archive_projection import (
    ProjectionChunkRecord,
    ProjectionVectorRecord,
    embedding_profile_digest,
)
from lab_assistant.contracts.canonical import sha256_text
from lab_assistant.contracts.errors import AbstentionReason
from lab_assistant.contracts.extraction import ProposalSource
from lab_assistant.contracts.parsing import RenditionDocument
from lab_assistant.contracts.publication import HeadDescriptor
from lab_assistant.contracts.release import ReleaseItemRecord
from lab_assistant.domain.archive_policy import evaluate_checkpoint, narrow, overlay
from lab_assistant.domain.assets import (
    Asset,
    MalwareScanStatus,
    Manifestation,
    RightsDecision,
    ValidationStatus,
)
from lab_assistant.domain.claims import (
    Claim,
    ClaimRevision,
    Entity,
    EpistemicStatus,
    Experiment,
    Review,
)
from lab_assistant.domain.identity import Work, WorkIdentifier
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.objects import ObjectRecord
from lab_assistant.domain.release import CorpusRelease, ReleaseItem, SearchAlias
from lab_assistant.domain.renditions import EvidenceSpan, ParseRun, Rendition
from lab_assistant.domain.scope import ApprovalState, ScopeVersion
from lab_assistant.domain.states import (
    IdentityState,
    ParseState,
    ReleaseState,
    RelevanceState,
    ReviewState,
    RightsState,
)

__all__ = ["ImportReport", "SnapshotImporter"]


@dataclass(frozen=True, slots=True)
class ImportReport:
    snapshot_id: UUID | None
    imported: bool = False
    #: Already the snapshot this reader serves: nothing written, nothing wrong.
    unchanged: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    lexical_only: bool = False
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "imported": self.imported,
            "unchanged": self.unchanged,
            "lexical_only": self.lexical_only,
            "counts": dict(sorted(self.counts.items())),
            "problems": list(self.problems),
            "notes": list(self.notes),
            "ok": self.ok,
        }


class SnapshotImporter:
    def __init__(
        self,
        context: ServiceContext,
        *,
        library_id: UUID,
        verifier: Any | None = None,
        require_signature: bool = False,
        group_map: dict[UUID, UUID] | None = None,
    ) -> None:
        self._context = context
        self._library_id = library_id
        self._verifier = verifier
        self._require_signature = require_signature
        self._group_map = dict(group_map or {})

    async def import_snapshot(self, workspace_id: UUID, directory: Path) -> ImportReport:
        self._context.require_installation(InstallationCapability.IMPORT_LIBRARY)
        directory = Path(directory)
        now = self._context.now()

        head = _read_head(directory)
        if head is None:
            return ImportReport(
                snapshot_id=None,
                problems=(f"{FETCHED_HEAD_NAME} is absent: import what `lab archive fetch` wrote",),
            )
        policy = _latest_checkpoint(directory)
        report = validate_snapshot(
            directory,
            verifier=self._verifier,
            require_signature=self._require_signature,
            policy=policy,
            now=now,
            payloads=True,
        )
        manifest = report.manifest
        if manifest is None or not report.ok:
            return ImportReport(
                snapshot_id=manifest.snapshot_id if manifest else None,
                problems=(
                    "the snapshot does not validate, so nothing was imported",
                    *(f"{finding.code}: {finding.detail}" for finding in report.blocking),
                ),
            )
        if manifest.library_id != self._library_id or head.snapshot_id != manifest.snapshot_id:
            return ImportReport(
                snapshot_id=manifest.snapshot_id,
                problems=("the snapshot belongs to another library, or another head",),
            )

        async with self._context.uow_factory() as uow:
            last = await uow.imports.latest(self._library_id)
        refusal = _ordering_refusal(last, head, policy)
        if refusal is not None:
            return ImportReport(snapshot_id=manifest.snapshot_id, problems=(refusal,))
        if (
            last is not None
            and last.snapshot_id == manifest.snapshot_id
            and ((policy.policy_sequence if policy else None) == last.policy_sequence)
        ):
            return ImportReport(snapshot_id=manifest.snapshot_id, unchanged=True)

        rows = _read_rows(directory, manifest)
        inventory: dict[UUID, ArtifactRef] = rows.pop(INVENTORY_KIND, {})
        verdict = (
            evaluate_checkpoint(
                policy,
                library_id=self._library_id,
                now=now,
                minimum_sequence=manifest.policy_sequence,
                minimum_digest=manifest.policy_artifact_digest,
            )
            if policy is not None
            else None
        )
        if policy is not None and verdict is not None:
            applied = overlay(
                policy,
                verdict=verdict,
                now=now,
                rights=rows[RecordKind.RIGHTS_DECISION],
                assets=rows[RecordKind.ASSET],
                renditions=rows[RecordKind.RENDITION],
                inventory=inventory,
                release_ids=tuple(rows[RecordKind.RELEASE]),
            )
            standings = {standing.decision_id: standing for standing in policy.rights}
            effective = {
                decision_id: narrow(record, standings.get(decision_id))
                for decision_id, record in rows[RecordKind.RIGHTS_DECISION].items()
            }
            withdrawn = applied.withdrawn_releases
        else:
            effective = {}
            withdrawn = frozenset()

        documents, problems = await self._store_payloads(workspace_id, directory, inventory, rows)
        problems += _verify_spans(rows, documents)
        if problems:
            return await self._failed(workspace_id, manifest.snapshot_id, head, policy, problems)

        counts: dict[str, int] = {}
        # Rows first — every projection row references its release — then the
        # projections, then the alias with the import record in one
        # transaction. Rows nobody's alias names are invisible to serving, so a
        # request sees the old release or the new one, never half of each.
        await self._write_rows(
            workspace_id,
            rows,
            inventory,
            effective=effective,
            withdrawn=withdrawn,
            manifest=manifest,
            counts=counts,
        )
        lexical_only = await self._write_projections(workspace_id, directory, manifest, counts)
        await self._write_claim_projection(workspace_id, rows)
        await self._activate(
            workspace_id,
            rows,
            withdrawn=withdrawn,
            manifest=manifest,
            head=head,
            policy=policy,
            counts=counts,
            projection_profiles=() if lexical_only else _profiles(manifest, self._context),
        )
        notes = []
        if lexical_only:
            notes.append(
                "the snapshot's vectors were made under another embedding profile than this "
                "installation's query embedder; search here is lexical until they match"
            )
        if withdrawn:
            notes.append(f"{len(withdrawn)} release(s) are withdrawn by current policy")
        return ImportReport(
            snapshot_id=manifest.snapshot_id,
            imported=True,
            counts=counts,
            lexical_only=lexical_only,
            notes=tuple(notes),
        )

    # ── payloads ───────────────────────────────────────────────────────────

    async def _store_payloads(
        self,
        workspace_id: UUID,
        directory: Path,
        inventory: dict[UUID, ArtifactRef],
        rows: dict[RecordKind, dict[UUID, Any]],
    ) -> tuple[dict[UUID, RenditionDocument], list[str]]:
        """Bytes into this reader's own store; withheld bytes as tombstoned rows.

        A withheld payload still needs a registry row — an asset must name an
        object — and a tombstoned row with no bytes is the existing way to say
        "recorded, deliberately not held", which every integrity sweep already
        understands.
        """
        self._object_ids: dict[UUID, UUID] = {}
        store = self._context.object_store
        for artifact_id, ref in inventory.items():
            if ref.is_resolvable:
                data = (directory / content_location(ref.sha256)).read_bytes()
                stored = await store.put(
                    data,
                    workspace_id=workspace_id,
                    object_class=ref.artifact_class.value,
                    media_type=ref.media_type,
                )
                async with self._context.uow_factory() as uow:
                    await register_object(uow, stored)
                    await uow.commit()
                self._object_ids[artifact_id] = stored.id
            else:
                object_id = uuid5(NAMESPACE_URL, f"withheld:{workspace_id}:{ref.sha256}")
                async with self._context.uow_factory() as uow:
                    await uow.objects.register(
                        ObjectRecord(
                            id=object_id,
                            workspace_id=workspace_id,
                            sha256=ref.sha256,
                            byte_length=ref.byte_length,
                            media_type=ref.media_type,
                            object_class=ref.artifact_class.value,
                            storage_key="",
                            tombstoned=True,
                            tombstone_reason=(
                                f"{ref.unavailable.value if ref.unavailable else 'withheld'}"
                                ":archive"
                            ),
                            created_at=self._context.now(),
                        )
                    )
                    await uow.commit()
                self._object_ids[artifact_id] = object_id

        documents: dict[UUID, RenditionDocument] = {}
        for rendition_id, rendition in rows[RecordKind.RENDITION].items():
            held = inventory.get(rendition.artifact_id) if rendition.artifact_id else None
            if held is None or not held.is_resolvable:
                continue
            data = (directory / content_location(held.sha256)).read_bytes()
            documents[rendition_id] = RenditionDocument.model_validate(json.loads(data))
        return documents, []

    # ── projections ────────────────────────────────────────────────────────

    async def _write_projections(
        self, workspace_id: UUID, directory: Path, manifest, counts: dict[str, int]
    ) -> bool:
        """Chunks always; vectors only under this installation's own profile.

        Written before the rows, keyed by release: until the alias moves they
        are rows for a release nobody can resolve, which is harmless. A release
        that already has its projection is not written again.
        """
        index = self._context.require_search_index()
        provider = self._context.embedding_provider
        local = embedding_profile_digest(provider.profile) if provider is not None else None
        lexical_only = False
        chunks = vectors = 0
        for ref in manifest.projection_refs:
            if await index.count_chunks(workspace_id, ref.release_id):
                continue
            # A vector row names its chunk; the index also wants the chunk's
            # work, which the chunk shard — always first — carries.
            works_by_chunk: dict[str, str] = {}
            for shard in ref.shards:
                lines = [
                    json.loads(line)
                    for line in (directory / shard.location).read_bytes().splitlines()
                    if line.strip()
                ]
                if shard.kind == "projection_chunk":
                    records = [ProjectionChunkRecord.model_validate(line) for line in lines]
                    works_by_chunk.update(
                        (record.chunk_key, str(record.work_id)) for record in records
                    )
                    chunks += await index.write_chunks(
                        workspace_id,
                        ref.release_id,
                        [_chunk_row(record) for record in records],
                    )
                elif shard.kind == "projection_vector":
                    if ref.embedding_profile_digest != local:
                        lexical_only = True
                        continue
                    records_v = [ProjectionVectorRecord.model_validate(line) for line in lines]
                    vectors += await index.write_embeddings(
                        workspace_id,
                        ref.release_id,
                        profile_id=records_v[0].profile_id if records_v else "",
                        rows=[
                            {
                                "chunk_key": record.chunk_key,
                                "release_id": str(ref.release_id),
                                "work_id": works_by_chunk[record.chunk_key],
                                "profile_id": record.profile_id,
                                "vector": list(record.values()),
                            }
                            for record in records_v
                        ],
                    )
        counts["projection_chunk"] = chunks
        counts["projection_vector"] = vectors
        return lexical_only

    # ── rows ───────────────────────────────────────────────────────────────

    async def _write_rows(
        self,
        workspace_id: UUID,
        rows: dict[RecordKind, dict[UUID, Any]],
        inventory: dict[UUID, ArtifactRef],
        *,
        effective: dict[UUID, Any],
        withdrawn: frozenset[UUID],
        manifest,
        counts: dict[str, int],
    ) -> None:
        """Every corpus row, in one transaction. No alias moves here."""
        ws = workspace_id
        now = self._context.now()
        async with self._context.uow_factory() as uow:
            for record in rows[RecordKind.SCOPE_VERSION].values():
                if await uow.configs.get_scope_version(ws, record.record_id) is None:
                    await uow.configs.add_scope_version(
                        ScopeVersion(
                            id=record.record_id,
                            workspace_id=ws,
                            scope_id=record.scope_id or record.record_id,
                            version=record.version or 1,
                            name=record.name,
                            scientific_objective=record.scientific_objective or "",
                            inclusion_criteria=record.inclusion_criteria,
                            exclusion_criteria=record.exclusion_criteria,
                            concepts=record.concepts,
                            connector_plan=record.connector_plan,
                            approval_state=ApprovalState(record.approval_state or "approved"),
                            frozen_at=record.frozen_at,
                            created_at=record.frozen_at or now,
                        )
                    )
            for record in rows[RecordKind.WORK].values():
                if await uow.works.get(ws, record.record_id) is None:
                    await uow.works.add(
                        Work(
                            id=record.record_id,
                            workspace_id=ws,
                            work_kind=record.work_kind,
                            identity_status=IdentityState(record.identity_status or "resolved"),
                            # A published release's works are included by
                            # definition: the steward included them.
                            relevance=RelevanceState.INCLUDED,
                            retraction_status=record.retraction_status or "none",
                            correction_status=record.correction_status or "none",
                            created_at=now,
                        )
                    )
                    for entry in record.identifiers:
                        await uow.works.add_identifier(
                            WorkIdentifier(
                                id=uuid5(
                                    record.record_id, f"{entry.scheme}:{entry.value_normalized}"
                                ),
                                workspace_id=ws,
                                work_id=record.record_id,
                                scheme=entry.scheme,
                                value_normalized=entry.value_normalized,
                                value_display=entry.value_display,
                                status=entry.status,
                                normalisation_version=entry.normalisation_version,
                            )
                        )
                await uow.imports.put_work_metadata(
                    ws,
                    record.record_id,
                    library_id=self._library_id,
                    title=record.title,
                    publication_year=record.publication_year,
                    container_title=record.container_title,
                )
            for record in rows[RecordKind.MANIFESTATION].values():
                if await uow.assets.get_manifestation(ws, record.record_id) is None:
                    await uow.assets.add_manifestation(
                        Manifestation(
                            id=record.record_id,
                            workspace_id=ws,
                            work_id=record.work_id,
                            manifestation_kind=record.manifestation_kind,
                            version_label=record.version_label,
                            publication_state=record.publication_state,
                            declared_mime=record.declared_mime,
                            language=record.language,
                            status=record.status or "active",
                            created_at=now,
                        )
                    )
            # Rights rows are the current policy projection: rewritten on every
            # import, narrowed by the checkpoint, groups mapped to local ones.
            for decision_id, record in rows[RecordKind.RIGHTS_DECISION].items():
                decided = effective.get(decision_id)
                await uow.policies.add_rights_decision(
                    RightsDecision(
                        id=decision_id,
                        workspace_id=ws,
                        subject_type=record.subject_type,
                        subject_id=record.subject_id,
                        licence_expression=record.licence_expression,
                        access_basis=record.access_basis,
                        allowed_actions=frozenset(
                            decided.allowed_actions if decided else record.allowed_actions
                        ),
                        restrictions=tuple(
                            decided.restrictions if decided else record.restrictions
                        ),
                        restricted_to_group_ids=tuple(
                            self._group_map.get(group, group)
                            for group in (
                                decided.restricted_to_group_ids
                                if decided
                                else record.restricted_to_group_ids
                            )
                        ),
                        decided_by_type="policy_import",
                        decided_at=record.decided_at or now,
                        review_due_at=decided.review_due_at if decided else record.review_due_at,
                        status=RightsState(
                            (decided.status if decided else record.status) or "unknown"
                        ),
                        note="archive import",
                    )
                )
            for record in rows[RecordKind.ASSET].values():
                if await uow.assets.get_asset(ws, record.record_id) is None:
                    await uow.assets.add_asset(
                        Asset(
                            id=record.record_id,
                            workspace_id=ws,
                            sha256=record.sha256,
                            byte_length=record.byte_length,
                            object_id=self._object_ids[record.artifact_id],
                            media_type_detected=record.media_type or "application/octet-stream",
                            malware_scan_status=record.malware_scan_status
                            or MalwareScanStatus.NOT_SCANNED,
                            validation_status=ValidationStatus.PASSED,
                            manifestation_id=record.manifestation_id,
                            work_id=record.work_id,
                            rights_decision_id=record.rights_decision_id,
                            identity_verified=True,
                            created_at=record.created_at or now,
                        )
                    )
            for record in rows[RecordKind.PARSE_RUN].values():
                if await uow.renditions.get_parse_run(ws, record.record_id) is None:
                    await uow.renditions.add_parse_run(
                        ParseRun(
                            id=record.record_id,
                            workspace_id=ws,
                            asset_id=record.asset_id,
                            parser_id=record.parser_id,
                            parser_version=record.parser_version,
                            input_sha256=record.input_sha256,
                            run_fingerprint=record.run_fingerprint,
                            status=record.status,
                            quality_metrics=record.quality_metrics,
                            started_at=record.started_at,
                            completed_at=record.completed_at,
                        )
                    )
            for record in _supersession_order(
                rows[RecordKind.RENDITION], "supersedes_rendition_id"
            ):
                if await uow.renditions.get_rendition(ws, record.record_id) is None:
                    ref = inventory.get(record.artifact_id) if record.artifact_id else None
                    await uow.renditions.add_rendition(
                        Rendition(
                            id=record.record_id,
                            workspace_id=ws,
                            parse_run_id=record.parse_run_id,
                            asset_id=record.asset_id,
                            work_id=record.work_id,
                            format=record.format,
                            schema_version=record.schema_version,
                            object_id=self._object_ids.get(record.artifact_id)
                            if ref is not None and ref.is_resolvable
                            else None,
                            sha256=record.sha256,
                            language=record.language,
                            page_count=record.page_count,
                            section_count=record.section_count,
                            table_count=record.table_count,
                            figure_count=record.figure_count,
                            quality_status=ParseState(record.quality_status or "ready"),
                            accepted_degraded_reason=record.accepted_degraded_reason,
                            supersedes_rendition_id=record.supersedes_rendition_id,
                            created_at=record.created_at or now,
                        )
                    )
            spans = [
                EvidenceSpan(
                    id=record.record_id,
                    workspace_id=ws,
                    rendition_id=record.rendition_id,
                    node_id=record.node_id,
                    start_offset=record.start_offset,
                    end_offset=record.end_offset,
                    quote_sha256=record.quote_sha256,
                    quote_preview=record.quote_preview or "",
                    normalisation_version=record.normalisation_version,
                    locator=record.locator or {},
                    is_focal=record.is_focal,
                    created_at=now,
                )
                for record in rows[RecordKind.SPAN].values()
            ]
            new_spans = [
                span for span in spans if await uow.renditions.get_span(ws, span.id) is None
            ]
            if new_spans:
                await uow.renditions.add_spans(new_spans)
            for record in rows[RecordKind.ENTITY].values():
                await uow.claims.add_entity(
                    Entity(
                        id=record.record_id,
                        workspace_id=ws,
                        entity_type=record.entity_type,
                        preferred_label=record.preferred_label,
                        normalised_value=record.normalised_value,
                        ontology_namespace=record.ontology_namespace,
                        ontology_id=record.ontology_id,
                        resolution_status=record.resolution_status,
                        created_at=now,
                    )
                )
            for record in rows[RecordKind.EXPERIMENT].values():
                await uow.claims.add_experiment(
                    Experiment(
                        id=record.record_id,
                        workspace_id=ws,
                        work_id=record.work_id,
                        rendition_id=record.rendition_id,
                        local_label=record.local_label,
                        experiment_type=record.experiment_type,
                        review_status=ReviewState(record.review_status or "unreviewed"),
                        created_at=now,
                    )
                )
            latest: dict[UUID, tuple[int, UUID]] = {}
            for revision in rows[RecordKind.CLAIM_REVISION].values():
                best = latest.get(revision.claim_id)
                if best is None or revision.revision_number > best[0]:
                    latest[revision.claim_id] = (revision.revision_number, revision.record_id)
            for record in rows[RecordKind.CLAIM].values():
                if await uow.claims.get_claim(ws, record.record_id) is None:
                    await uow.claims.add_claim(
                        Claim(
                            id=record.record_id,
                            workspace_id=ws,
                            work_id=record.work_id,
                            experiment_id=record.experiment_id,
                            subject_entity_id=record.subject_entity_id,
                            subject_label=record.subject_label,
                            predicate=record.predicate,
                            object_kind=record.object_kind or "quantity",
                            unit_entity_id=record.unit_entity_id,
                            qualifiers=record.qualifiers,
                            polarity=record.polarity or "positive",
                            epistemic_status=EpistemicStatus(record.epistemic_status or "reported"),
                            current_revision_id=latest.get(record.record_id, (0, None))[1],
                            template_id=record.template_id,
                            template_version=record.template_version,
                            target_id=record.target_id,
                            lifecycle_status=record.lifecycle_status or "active",
                            created_at=now,
                        )
                    )
            for record in sorted(
                rows[RecordKind.CLAIM_REVISION].values(),
                key=lambda item: (str(item.claim_id), item.revision_number),
            ):
                if await uow.claims.get_revision(ws, record.record_id) is None:
                    await uow.claims.add_revision(
                        ClaimRevision(
                            id=record.record_id,
                            workspace_id=ws,
                            claim_id=record.claim_id,
                            revision_number=record.revision_number,
                            value=record.value or {},
                            evidence_span_ids=record.evidence_span_ids,
                            proposal_source=ProposalSource(record.proposal_source or "model"),
                            extractor_fingerprint=record.extractor_fingerprint,
                            validation_status=record.validation_status or "passed",
                            review_status=ReviewState(record.review_status or "unreviewed"),
                            abstention_reason=AbstentionReason(record.abstention_reason or "none"),
                            epistemic_status=EpistemicStatus(record.epistemic_status or "reported"),
                            created_at=record.created_at or now,
                            supersedes_revision_id=record.supersedes_revision_id,
                        )
                    )
            existing_reviews = {review.id for review in await uow.claims.list_reviews(ws)}
            for record in rows[RecordKind.REVIEW].values():
                if record.record_id in existing_reviews:
                    continue
                await uow.claims.add_review(
                    Review(
                        id=record.record_id,
                        workspace_id=ws,
                        subject_type=record.subject_type or "claim_revision",
                        subject_id=record.subject_id or record.revision_id or record.record_id,
                        revision_id=record.revision_id,
                        decision=record.decision,
                        before=record.before or {},
                        after=record.after or {},
                        reason_code=record.reason_code or "",
                        comment=record.comment,
                        # Attribution, not an account: the origin's reviewer id
                        # is no user here and grants nothing (S22).
                        reviewer_id=record.origin_reviewer_id or manifest.origin_installation_id,
                        reviewed_at=record.reviewed_at or now,
                    )
                )
            imported_releases = rows[RecordKind.RELEASE]
            for record in _supersession_order(imported_releases, "parent_release_id"):
                status = (
                    ReleaseState.WITHDRAWN
                    if record.record_id in withdrawn
                    else ReleaseState(record.status)
                )
                existing = await uow.releases.get(ws, record.record_id)
                release = CorpusRelease(
                    id=record.record_id,
                    workspace_id=ws,
                    name=record.name,
                    semantic_version=record.semantic_version,
                    status=status,
                    parent_release_id=record.parent_release_id
                    if record.parent_release_id in imported_releases
                    else None,
                    scope_version_ids=record.scope_version_ids,
                    embedding_profile_ids=record.embedding_profile_ids,
                    manifest_sha256=record.manifest_sha256,
                    published_at=record.published_at,
                    created_at=record.published_at or now,
                    gate_results={"imported_from_archive": True},
                )
                if existing is None:
                    await uow.releases.add(release)
                elif existing.status is not status:
                    await uow.releases.update(
                        existing.model_copy(
                            update={"status": status, "version": existing.version + 1}
                        )
                    )
            for release_id in imported_releases:
                if await uow.releases.list_items(ws, release_id):
                    continue
                items = [
                    ReleaseItem(
                        id=entry.record_id,
                        workspace_id=ws,
                        release_id=release_id,
                        record=ReleaseItemRecord(
                            work_id=entry.work_id,
                            manifestation_id=entry.manifestation_id,
                            asset_id=entry.asset_id,
                            rendition_id=entry.rendition_id,
                            rendition_digest=entry.rendition_digest,
                            claim_revision_ids=entry.claim_revision_ids,
                            access_policy_snapshot_id=entry.access_policy_snapshot_id,
                            inclusion_reason=entry.inclusion_reason,
                        ),
                    )
                    for entry in rows[RecordKind.RELEASE_ITEM].values()
                    if entry.release_id == release_id
                ]
                if items:
                    await uow.releases.add_items(items)

            for kind in (
                RecordKind.WORK,
                RecordKind.ASSET,
                RecordKind.RENDITION,
                RecordKind.SPAN,
                RecordKind.CLAIM_REVISION,
                RecordKind.RELEASE,
            ):
                counts[kind.value] = len(rows[kind])
            await uow.commit()

    async def _activate(
        self,
        ws: UUID,
        rows,
        *,
        withdrawn: frozenset[UUID],
        manifest,
        head: HeadDescriptor,
        policy: PolicyCheckpoint | None,
        counts: dict[str, int],
        projection_profiles: tuple[str, ...],
    ) -> None:
        """The alias and the import record, together: the moment it serves."""
        now = self._context.now()
        async with self._context.uow_factory() as uow:
            for release_id in rows[RecordKind.RELEASE]:
                release = await uow.releases.get(ws, release_id)
                if release is not None and tuple(release.projection_ready_profiles) != tuple(
                    projection_profiles
                ):
                    await uow.releases.update(
                        release.model_copy(
                            update={
                                "projection_ready_profiles": projection_profiles,
                                "version": release.version + 1,
                            }
                        )
                    )
            for alias, release_id in manifest.selected_alias_targets.items():
                if release_id in withdrawn:
                    continue
                current = await uow.releases.get_alias(ws, alias)
                if current is not None and current.release_id == release_id:
                    continue
                await uow.releases.upsert_alias(
                    SearchAlias(
                        workspace_id=ws,
                        alias=alias,
                        release_id=release_id,
                        previous_release_id=current.release_id if current else None,
                        switched_at=now,
                        version=(current.version + 1) if current else 1,
                    )
                )
            await uow.imports.add(
                ImportRecord(
                    id=self._context.new_id(),
                    workspace_id=ws,
                    library_id=self._library_id,
                    snapshot_id=manifest.snapshot_id,
                    commit_sequence=head.commit_sequence,
                    policy_sequence=policy.policy_sequence if policy else None,
                    policy_checkpoint_digest=policy.signing_digest() if policy else None,
                    state=ImportState.ACTIVE,
                    counts=counts,
                    imported_at=now,
                )
            )
            await uow.commit()

    async def _write_claim_projection(self, ws: UUID, rows) -> None:
        """Rebuilt from the imported claims: derived rows, no model involved."""
        index = self._context.require_search_index()
        revisions = rows[RecordKind.CLAIM_REVISION]
        claims = rows[RecordKind.CLAIM]
        by_release: dict[UUID, list[dict[str, Any]]] = {}
        for entry in rows[RecordKind.RELEASE_ITEM].values():
            for revision_id in entry.claim_revision_ids:
                revision = revisions.get(revision_id)
                claim = claims.get(revision.claim_id) if revision else None
                if revision is None or claim is None:
                    continue
                value = revision.value or {}
                by_release.setdefault(entry.release_id, []).append(
                    {
                        "release_id": str(entry.release_id),
                        "work_id": str(claim.work_id),
                        "claim_id": str(claim.record_id),
                        "revision_id": str(revision.record_id),
                        "experiment_id": str(claim.experiment_id) if claim.experiment_id else None,
                        "predicate": claim.predicate,
                        "subject": claim.subject_label,
                        "object_kind": claim.object_kind,
                        "value": value,
                        "unit": value.get("unit_canonical") or value.get("unit_verbatim"),
                        "review_status": revision.review_status,
                        "abstention_reason": revision.abstention_reason,
                        "evidence_span_ids": [str(i) for i in revision.evidence_span_ids],
                        "access_policy_snapshot_id": str(entry.access_policy_snapshot_id)
                        if entry.access_policy_snapshot_id
                        else None,
                    }
                )
        for release_id, projection in by_release.items():
            existing = await index.claim_search(ws, release_id, top_k=1)
            if not existing:
                await index.write_claim_projection(ws, release_id, projection)

    async def _failed(self, workspace_id, snapshot_id, head, policy, problems) -> ImportReport:
        async with self._context.uow_factory() as uow:
            await uow.imports.add(
                ImportRecord(
                    id=self._context.new_id(),
                    workspace_id=workspace_id,
                    library_id=self._library_id,
                    snapshot_id=snapshot_id,
                    commit_sequence=head.commit_sequence,
                    policy_sequence=policy.policy_sequence if policy else None,
                    state=ImportState.FAILED,
                    detail="; ".join(problems)[:2000],
                    imported_at=self._context.now(),
                )
            )
            await uow.commit()
        return ImportReport(snapshot_id=snapshot_id, problems=tuple(problems))


# ── helpers ────────────────────────────────────────────────────────────────


def _read_head(directory: Path) -> HeadDescriptor | None:
    path = directory / FETCHED_HEAD_NAME
    if not path.is_file():
        return None
    return HeadDescriptor.model_validate_json(path.read_bytes())


def _latest_checkpoint(directory: Path) -> PolicyCheckpoint | None:
    folder = directory / POLICY_DIRECTORY
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*.json"))
    if not files:
        return None
    return PolicyCheckpoint.model_validate_json(files[-1].read_bytes())


def _ordering_refusal(last: ImportRecord | None, head: HeadDescriptor, policy) -> str | None:
    """Never backwards: the reader's own record of where it is (S65)."""
    if last is None:
        return None
    if head.commit_sequence < last.commit_sequence:
        return (
            f"this snapshot was fetched under head sequence {head.commit_sequence}, and this "
            f"reader already serves sequence {last.commit_sequence}: a replayed or rolled-back "
            "head. Refused"
        )
    if head.commit_sequence == last.commit_sequence and head.snapshot_id != last.snapshot_id:
        return (
            f"two different snapshots claim head sequence {head.commit_sequence}: a fork. Refused"
        )
    if (
        policy is not None
        and last.policy_sequence is not None
        and policy.policy_sequence < last.policy_sequence
    ):
        return (
            f"the snapshot's policy checkpoint is sequence {policy.policy_sequence}, older than "
            f"{last.policy_sequence}, which this reader already applied"
        )
    return None


def _read_rows(directory: Path, manifest) -> dict[Any, dict[UUID, Any]]:
    rows: dict[Any, dict[UUID, Any]] = {kind: {} for kind in RecordKind}
    for shard in manifest.catalog_shards:
        lines = [
            json.loads(line)
            for line in (directory / shard.location).read_bytes().splitlines()
            if line.strip()
        ]
        if shard.kind == INVENTORY_KIND:
            rows[INVENTORY_KIND] = {
                ref.artifact_id: ref for ref in (ArtifactRef.model_validate(line) for line in lines)
            }
            continue
        model = record_for(shard.kind)
        rows[RecordKind(shard.kind)] = {
            record.record_id: record for record in (model.model_validate(line) for line in lines)
        }
    return rows


def _verify_spans(rows, documents: dict[UUID, RenditionDocument]) -> list[str]:
    """Each span's quote is what the imported rendition holds at those offsets."""
    problems: list[str] = []
    for span in rows[RecordKind.SPAN].values():
        document = documents.get(span.rendition_id)
        if document is None:
            continue  # the rendition was withheld; its spans travel as previews
        text = document.text_of(span.node_id)
        if text is None or sha256_text(text[span.start_offset : span.end_offset]) != (
            span.quote_sha256
        ):
            problems.append(
                f"evidence span {span.record_id} does not verify against rendition "
                f"{span.rendition_id}: the text is not at the offsets the span records"
            )
    return problems


def _supersession_order(records: dict[UUID, Any], attribute: str) -> list[Any]:
    """Predecessors first, so a self-referencing key never points forward."""
    ordered: list[Any] = []
    placed: set[UUID] = set()

    def place(record) -> None:
        if record.record_id in placed:
            return
        parent = getattr(record, attribute, None)
        if parent is not None and parent in records and parent not in placed:
            place(records[parent])
        placed.add(record.record_id)
        ordered.append(record)

    for record in sorted(records.values(), key=lambda item: str(item.record_id)):
        place(record)
    return ordered


def _chunk_row(record: ProjectionChunkRecord) -> dict[str, Any]:
    return {
        "chunk_key": record.chunk_key,
        "release_id": str(record.release_id),
        "work_id": str(record.work_id),
        "rendition_id": str(record.rendition_id),
        "node_ids": list(record.node_ids),
        "content": record.content,
        "content_sha256": record.content_sha256,
        "chunk_type": record.chunk_type,
        "section_label": record.section_label,
        "page": record.page,
        "token_count": record.token_count,
        "chunker_id": record.chunker_id,
        "chunker_version": record.chunker_version,
        "access_policy_snapshot_id": str(record.access_policy_snapshot_id)
        if record.access_policy_snapshot_id
        else None,
        "metadata": dict(record.metadata),
    }


def _profiles(manifest, context: ServiceContext) -> tuple[str, ...]:
    provider = context.embedding_provider
    if provider is None:
        return ()
    return (provider.profile.profile_id,)
