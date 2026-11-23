"""Turning a published release into a portable catalog (phase 2).

The object store already holds the bytes. What it does not hold is the meaning:
which work a payload belongs to, which revision superseded which, who reviewed
it, what question the corpus was built to answer, and who may see any of it.
That lives in the producer's database, and this is how it travels.

The operation is a **transitive closure, not a table dump**. Starting from the
releases named, it walks: release → entries → works → manifestations → assets →
rights decisions → renditions → spans, and release → entries → claim revisions →
claims → reviews. Every row reached is written; nothing else is. A table dump
would carry the producer's drafts, its rejected candidates and its queue, none
of which belong in a library — and would still miss the scope version, which
nothing points at from the release row.

Three decisions worth stating.

**Pinned, not current.** A release entry names claim *revisions*, so the catalog
carries what the release said when it was published. An exporter that followed
`claim.current_revision_id` would silently rewrite last quarter's answer every
time somebody corrected a number, which is the one thing a citation must not do.

**Superseded and negative rows travel.** The revision a correction replaced, the
review that made the correction, and an abstention that records "this source
reports nothing" are all written. A catalog of current accepted values would
lose the reason a number changed and make every reader re-run an extraction that
already completed (scenarios S12 and S15).

**Payloads are referenced, never inlined.** A record names an artifact by digest
and the bytes sit beside the catalog under `content/sha256/…`, outside every
shard. So a catalog is small, diffable and cheap to validate, and the thing that
is expensive to move is moved once — a payload is uploaded once per library,
however many snapshots name it.

**Only what may be replicated leaves.** A payload, and everything that is its
text in another shape — its rendition, its search chunks, their vectors — is
copied only when a governing rights decision grants `replicate_content`.
Otherwise the inventory says it is withheld by rights, and the records still
travel: a reader knows the paper and its reviewed claims and cannot serve its
text. Permission to store content here is not permission to store it on every
reader, and the architecture asks for denial while that is undecided.

**Every payload says what it depends on and what governs it.** The inventory
carries the retention edges — a rendition depends on its asset, and both are
governed by the asset's rights decision — so a revocation in a later policy
checkpoint resolves to the exact bytes it withholds without the producer's
database, and an eventual purge can tell which bytes another acquisition still
needs.

What this does *not* do: publish. There is no head, no compare-and-swap and no
writer epoch here — building a snapshot and committing it are separate
operations, and the second one is the rest of phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from lab_assistant.application.context import ServiceContext
from lab_assistant.application.pagination import DEFAULT_PAGE_SIZE, iter_pages
from lab_assistant.contracts.archive import (
    ARCHIVE_FORMAT_VERSION,
    ArtifactClass,
    ArtifactRef,
    CatalogShardRef,
    PackageRef,
    ProjectionRef,
    SnapshotManifest,
    UnavailableReason,
    content_location,
)
from lab_assistant.contracts.archive_catalog import (
    APPLY_ORDER,
    CATALOG_SCHEMA_VERSION,
    AssetRecord,
    CatalogRecord,
    ClaimRecord,
    ClaimRevisionRecord,
    EntityRecord,
    ExperimentRecord,
    IdentifierEntry,
    ManifestationRecord,
    ParseRunRecord,
    RecordKind,
    ReleaseEntryRecord,
    ReleaseRecord,
    RenditionRecord,
    ReviewRecord,
    RightsDecisionRecord,
    ScopeVersionRecord,
    SpanRecord,
    WorkRecord,
    shard_digest,
)
from lab_assistant.contracts.archive_projection import (
    PROJECTION_SCHEMA_VERSION,
    ProjectionChunkRecord,
    ProjectionVectorRecord,
    chunker_digest,
    embedding_profile_digest,
    encode_vector,
    projection_record_id,
)
from lab_assistant.contracts.canonical import canonical_json, sha256_bytes
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.contracts.signing import statement_for

__all__ = [
    "CATALOG_DIRECTORY",
    "INVENTORY_KIND",
    "MANIFEST_NAME",
    "PROJECTION_CHUNK_KIND",
    "PROJECTION_DIRECTORY",
    "PROJECTION_VECTOR_KIND",
    "ArchiveCatalogBuilder",
    "CatalogBuild",
]

MANIFEST_NAME = "manifest.json"
CATALOG_DIRECTORY = "catalog"
#: The shard kind holding `ArtifactRef` rows rather than catalog records.
INVENTORY_KIND = "artifact_inventory"
#: Projections live beside the catalog, referenced from `projection_refs`
#: rather than from `catalog_shards`: they are rebuildable from the
#: renditions given the same chunker, and the catalog is not.
PROJECTION_DIRECTORY = "projections"
PROJECTION_CHUNK_KIND = "projection_chunk"
PROJECTION_VECTOR_KIND = "projection_vector"

#: Namespace for deriving an artifact id from the payload it names. Derived
#: rather than allocated so two snapshots of the same corpus agree about which
#: artifact is which, which is what makes an incremental publication possible.
_ARTIFACT_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


@dataclass(frozen=True, slots=True)
class CatalogBuild:
    """What a build produced, and everything it could not resolve."""

    snapshot_id: UUID
    directory: str
    manifest: SnapshotManifest | None = None
    #: Rows written, by kind. The number an operator reads out loud.
    counts: dict[str, int] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": str(self.snapshot_id),
            "directory": self.directory,
            "counts": dict(sorted(self.counts.items())),
            "shards": len(self.manifest.catalog_shards) if self.manifest else 0,
            "problems": list(self.problems),
            "ok": self.ok,
        }


class _Closure:
    """Rows reached so far, deduplicated, in the order they were reached."""

    def __init__(self) -> None:
        self.rows: dict[RecordKind, dict[UUID, CatalogRecord]] = {kind: {} for kind in APPLY_ORDER}
        self.artifacts: dict[UUID, ArtifactRef] = {}
        #: The decisions themselves, not only their records: whether bytes may
        #: be replicated is asked of the decision, with its expiry.
        self.decisions: dict[UUID, Any] = {}

    def add(self, record: CatalogRecord) -> bool:
        """Record a row. Returns False when it was already reached."""
        bucket = self.rows[record.kind]
        if record.record_id in bucket:
            return False
        bucket[record.record_id] = record
        return True

    def seen(self, kind: RecordKind, record_id: UUID) -> bool:
        return record_id in self.rows[kind]

    def count(self, kind: RecordKind) -> int:
        return len(self.rows[kind])


class ArchiveCatalogBuilder:
    """Walks a release's closure and writes it as typed, digested shards."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        library_id: UUID,
        installation_id: UUID,
        writer_epoch: int = 0,
        signer: Any | None = None,
    ) -> None:
        self._context = context
        self._library_id = library_id
        self._installation_id = installation_id
        self._writer_epoch = writer_epoch
        #: Optional. A library published by an installation with no key yet is
        #: readable; it simply carries no statement about who approved it.
        self._signer = signer

    async def build(
        self,
        workspace_id: UUID,
        destination: Path,
        *,
        release_ids: tuple[UUID, ...] | None = None,
        publication_sequence: int = 1,
        parent_snapshot_id: UUID | None = None,
        policy_sequence: int = 0,
        policy_artifact_digest: str | None = None,
    ) -> CatalogBuild:
        """Write a snapshot of the named releases, or of every servable one.

        ``policy_sequence`` and ``policy_artifact_digest`` name the policy
        checkpoint current at the library's policy head when this was built.
        A reader applies that checkpoint or a later one, so the snapshot can
        never pull a reader's policy backwards; zero means none was issued.
        """
        destination = Path(destination)
        snapshot_id = uuid4()
        problems: list[str] = []

        releases = await self._releases(workspace_id, release_ids, problems)
        if not releases:
            return CatalogBuild(
                snapshot_id=snapshot_id,
                directory=str(destination),
                problems=tuple(problems or ["no servable release to package"]),
            )

        closure = _Closure()
        for release in releases:
            await self._walk_release(workspace_id, release, closure, problems)

        copied = await self._write_payloads(workspace_id, destination, closure, problems)
        (destination / CATALOG_DIRECTORY).mkdir(parents=True, exist_ok=True)
        shards = self._write_shards(destination, closure)
        # The artifact inventory is a shard of its own rather than a field on
        # the manifest. The manifest is the thing a reader fetches first to
        # decide anything, so it stays small; a corpus with fifty thousand
        # payloads would otherwise put all of them in it.
        shards += (self._write_inventory(destination, closure),)
        projections = await self._write_projections(
            destination, workspace_id, releases, servable=_servable_renditions(closure)
        )
        withheld = tuple(
            sorted(
                (ref.artifact_id for ref in closure.artifacts.values() if not ref.is_resolvable),
                key=str,
            )
        )

        manifest = SnapshotManifest(
            library_id=self._library_id,
            source_workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            origin_installation_id=self._installation_id,
            writer_epoch=self._writer_epoch,
            publication_sequence=publication_sequence,
            parent_snapshot_id=parent_snapshot_id,
            created_at=self._context.now(),
            producer_app_version=_app_version(),
            scientific_release_ids=tuple(release.id for release in releases),
            selected_alias_targets=await self._aliases(workspace_id, releases),
            policy_sequence=policy_sequence,
            policy_artifact_digest=policy_artifact_digest,
            catalog_shards=shards,
            projection_refs=projections,
            package_refs=self._package_refs(closure),
            required_object_inventory_digest=_inventory_digest(closure.artifacts.values()),
            missing_optional_artifacts=withheld,
        )
        manifest = self._sign(manifest)
        _write_manifest(destination, manifest)

        return CatalogBuild(
            snapshot_id=snapshot_id,
            directory=str(destination),
            manifest=manifest,
            counts={
                **{kind.value: closure.count(kind) for kind in APPLY_ORDER},
                "artifact": len(closure.artifacts),
                "payload_copied": copied,
                "payload_withheld": len(withheld),
                "projection_chunk": sum(
                    shard.row_count
                    for ref in projections
                    for shard in ref.shards
                    if shard.kind == PROJECTION_CHUNK_KIND
                ),
                "projection_vector": sum(
                    shard.row_count
                    for ref in projections
                    for shard in ref.shards
                    if shard.kind == PROJECTION_VECTOR_KIND
                ),
            },
            problems=tuple(problems),
        )

    # ── the closure ────────────────────────────────────────────────────────

    async def _releases(
        self, workspace_id: UUID, release_ids: tuple[UUID, ...] | None, problems: list[str]
    ):
        async with self._context.uow_factory() as uow:
            if release_ids:
                found = []
                for release_id in release_ids:
                    release = await uow.releases.get(workspace_id, release_id)
                    if release is None:
                        problems.append(f"release {release_id} does not exist")
                        continue
                    if not release.status.is_servable:
                        problems.append(
                            f"release {release_id} is {release.status.value}, which is not "
                            "servable: a snapshot carries published state, not a draft"
                        )
                        continue
                    found.append(release)
                return found
            return [
                release
                async for release in iter_pages(
                    lambda after, size: uow.releases.list_all_releases(
                        workspace_id, limit=size, after_id=after
                    )
                )
                if release.status.is_servable
            ]

    async def _aliases(self, workspace_id: UUID, releases) -> dict[str, UUID]:
        """Which release each alias points at, for the ones being packaged.

        An alias the snapshot does not contain is left out rather than carried
        dangling: a reader resolving `current` to a release it does not have is
        a worse failure than a reader with no `current` at all.
        """
        packaged = {release.id for release in releases}
        async with self._context.uow_factory() as uow:
            aliases = await uow.releases.list_aliases(workspace_id)
        return {
            alias.alias: alias.release_id
            for alias in aliases
            if alias.release_id is not None and alias.release_id in packaged
        }

    async def _walk_release(
        self, workspace_id: UUID, release, closure: _Closure, problems: list[str]
    ) -> None:
        async with self._context.uow_factory() as uow:
            closure.add(
                ReleaseRecord(
                    library_id=self._library_id,
                    record_id=release.id,
                    name=release.name,
                    semantic_version=release.semantic_version,
                    status=release.status.value,
                    manifest_sha256=release.manifest_sha256,
                    scope_version_ids=tuple(release.scope_version_ids),
                    parent_release_id=release.parent_release_id,
                    embedding_profile_ids=tuple(release.embedding_profile_ids),
                    projection_ready_profiles=tuple(release.projection_ready_profiles),
                    published_at=release.published_at,
                )
            )

            # The frozen question. Nothing points at it from the release row's
            # own table, and without it "nothing was found" is uninterpretable.
            for scope_version_id in release.scope_version_ids:
                if closure.seen(RecordKind.SCOPE_VERSION, scope_version_id):
                    continue
                scope = await uow.configs.get_scope_version(workspace_id, scope_version_id)
                if scope is None:
                    problems.append(
                        f"release {release.id}: scope version {scope_version_id} is absent, "
                        "so the question this release answers cannot travel"
                    )
                    continue
                closure.add(_scope_record(self._library_id, scope))

            for item in await uow.releases.list_items(workspace_id, release.id):
                record = item.record
                closure.add(
                    ReleaseEntryRecord(
                        library_id=self._library_id,
                        record_id=item.id,
                        release_id=release.id,
                        work_id=record.work_id,
                        manifestation_id=record.manifestation_id,
                        asset_id=record.asset_id,
                        rendition_id=record.rendition_id,
                        rendition_digest=record.rendition_digest,
                        claim_revision_ids=tuple(record.claim_revision_ids),
                        access_policy_snapshot_id=record.access_policy_snapshot_id,
                        inclusion_reason=record.inclusion_reason,
                        content_digest=record.content_digest(),
                    )
                )
                await self._walk_work(workspace_id, record.work_id, uow, closure, problems)
                await self._walk_manifestation(workspace_id, record, uow, closure)
                await self._walk_asset(workspace_id, record.asset_id, uow, closure, problems)
                await self._walk_rendition(
                    workspace_id, record.rendition_id, uow, closure, problems
                )
                for revision_id in record.claim_revision_ids:
                    await self._walk_revision(workspace_id, revision_id, uow, closure, problems)

    async def _walk_work(
        self, workspace_id: UUID, work_id: UUID, uow, closure: _Closure, problems: list[str]
    ) -> None:
        if closure.seen(RecordKind.WORK, work_id):
            return
        work = await uow.works.get(workspace_id, work_id)
        if work is None:
            problems.append(f"work {work_id} is referenced by a release and is absent")
            return
        identifiers = await uow.works.list_identifiers(workspace_id, work_id)
        relations = await uow.works.list_relations(workspace_id, work_id)
        assertions = await uow.observations.list_assertions(workspace_id, work_id=work_id)

        from lab_assistant.application.reconciliation import canonical_metadata

        metadata = canonical_metadata(list(assertions))
        year = metadata.get("year")
        closure.add(
            WorkRecord(
                library_id=self._library_id,
                record_id=work.id,
                work_kind=work.work_kind.value
                if hasattr(work.work_kind, "value")
                else str(work.work_kind),
                title=metadata.get("title"),
                publication_year=year if isinstance(year, int) else None,
                container_title=metadata.get("container_title") or metadata.get("journal"),
                identity_status=_value(work.identity_status),
                retraction_status=_value(work.retraction_status),
                correction_status=_value(work.correction_status),
                identifiers=tuple(
                    IdentifierEntry(
                        scheme=_value(identifier.scheme),
                        value_display=identifier.value_display,
                        value_normalized=identifier.value_normalized,
                        normalisation_version=identifier.normalisation_version,
                        status=identifier.status,
                    )
                    for identifier in sorted(
                        identifiers, key=lambda item: (_value(item.scheme), item.value_normalized)
                    )
                ),
                relations=tuple(
                    {
                        "kind": _value(relation.kind),
                        "to_work_id": str(relation.to_work_id),
                    }
                    for relation in relations
                ),
            )
        )

    async def _walk_manifestation(self, workspace_id: UUID, record, uow, closure: _Closure) -> None:
        manifestation_id = record.manifestation_id
        if manifestation_id is None or closure.seen(RecordKind.MANIFESTATION, manifestation_id):
            return
        manifestation = await uow.assets.get_manifestation(workspace_id, manifestation_id)
        if manifestation is None:
            return
        closure.add(
            ManifestationRecord(
                library_id=self._library_id,
                record_id=manifestation.id,
                work_id=manifestation.work_id,
                manifestation_kind=_value(manifestation.manifestation_kind),
                version_label=manifestation.version_label,
                publication_state=_value(manifestation.publication_state),
                declared_mime=manifestation.declared_mime,
                language=manifestation.language,
                status=_value(manifestation.status),
            )
        )

    async def _walk_asset(
        self,
        workspace_id: UUID,
        asset_id: UUID | None,
        uow,
        closure: _Closure,
        problems: list[str],
    ) -> None:
        if asset_id is None or closure.seen(RecordKind.ASSET, asset_id):
            return
        asset = await uow.assets.get_asset(workspace_id, asset_id)
        if asset is None:
            problems.append(f"asset {asset_id} is referenced by a release and is absent")
            return
        artifact_id = self._artifact(
            closure,
            sha256=asset.sha256,
            byte_length=asset.byte_length,
            media_type=asset.media_type_detected or "application/octet-stream",
            artifact_class=ArtifactClass.ASSET,
            governed_by=(asset.rights_decision_id,) if asset.rights_decision_id else (),
        )
        closure.add(
            AssetRecord(
                library_id=self._library_id,
                record_id=asset.id,
                work_id=asset.work_id,
                manifestation_id=asset.manifestation_id,
                sha256=asset.sha256,
                byte_length=asset.byte_length,
                media_type=asset.media_type_detected,
                rights_decision_id=asset.rights_decision_id,
                malware_scan_status=_value(asset.malware_scan_status),
                artifact_id=artifact_id,
                created_at=asset.created_at,
            )
        )
        await self._walk_rights(workspace_id, asset.rights_decision_id, uow, closure, problems)

    async def _walk_rights(
        self,
        workspace_id: UUID,
        decision_id: UUID | None,
        uow,
        closure: _Closure,
        problems: list[str],
    ) -> None:
        """The rights decision travels with the asset.

        An archive that carried evidence without it would have a reader serving
        material the publisher restricted, and "it was in the archive" is not a
        licence (scenario S39).
        """
        if decision_id is None or closure.seen(RecordKind.RIGHTS_DECISION, decision_id):
            return
        decision = await uow.policies.get_rights_decision(workspace_id, decision_id)
        if decision is None:
            problems.append(
                f"rights decision {decision_id} is absent, so an asset would travel "
                "without the decision that permits it"
            )
            return
        closure.decisions[decision.id] = decision
        closure.add(
            RightsDecisionRecord(
                library_id=self._library_id,
                record_id=decision.id,
                subject_type=_value(decision.subject_type),
                subject_id=decision.subject_id,
                access_basis=_value(decision.access_basis),
                licence_expression=decision.licence_expression,
                allowed_actions=tuple(_value(action) for action in decision.allowed_actions),
                restrictions=tuple(_value(item) for item in decision.restrictions),
                restricted_to_group_ids=tuple(decision.restricted_to_group_ids),
                status=_value(decision.status),
                decided_at=decision.decided_at,
                review_due_at=decision.review_due_at,
            )
        )

    async def _walk_rendition(
        self,
        workspace_id: UUID,
        rendition_id: UUID | None,
        uow,
        closure: _Closure,
        problems: list[str],
    ) -> None:
        if rendition_id is None or closure.seen(RecordKind.RENDITION, rendition_id):
            return
        rendition = await uow.renditions.get_rendition(workspace_id, rendition_id)
        if rendition is None:
            problems.append(f"rendition {rendition_id} is referenced and is absent")
            return
        # The rendition's own asset, which need not be the entry's. Walked
        # first because the rendition's retention edges point at it.
        await self._walk_asset(workspace_id, rendition.asset_id, uow, closure, problems)
        found = closure.rows[RecordKind.ASSET].get(rendition.asset_id)
        source = found if isinstance(found, AssetRecord) else None
        artifact_id = self._artifact(
            closure,
            sha256=rendition.sha256,
            byte_length=0,
            media_type="application/json",
            artifact_class=ArtifactClass.RENDITION,
            # A rendition is its asset's text, so it is retained and withheld
            # on its asset's terms (`ArtifactRef.depends_on`, `governed_by`).
            depends_on=(source.artifact_id,) if source and source.artifact_id else (),
            governed_by=(source.rights_decision_id,)
            if source and source.rights_decision_id
            else (),
        )
        await self._walk_parse_run(workspace_id, rendition, uow, closure, problems)
        closure.add(
            RenditionRecord(
                library_id=self._library_id,
                record_id=rendition.id,
                asset_id=rendition.asset_id,
                parse_run_id=rendition.parse_run_id,
                work_id=rendition.work_id,
                sha256=rendition.sha256,
                format=_value(rendition.format),
                schema_version=rendition.schema_version,
                quality_status=_value(rendition.quality_status),
                accepted_degraded_reason=rendition.accepted_degraded_reason,
                supersedes_rendition_id=rendition.supersedes_rendition_id,
                artifact_id=artifact_id,
                language=rendition.language,
                page_count=rendition.page_count,
                section_count=rendition.section_count,
                table_count=rendition.table_count,
                figure_count=rendition.figure_count,
                created_at=rendition.created_at,
            )
        )
        for span in await uow.renditions.list_spans(workspace_id, rendition_id):
            closure.add(
                SpanRecord(
                    library_id=self._library_id,
                    record_id=span.id,
                    rendition_id=span.rendition_id,
                    node_id=span.node_id,
                    start_offset=span.start_offset,
                    end_offset=span.end_offset,
                    quote_sha256=span.quote_sha256,
                    quote_preview=span.quote_preview,
                    normalisation_version=span.normalisation_version,
                    locator=span.locator,
                    is_focal=span.is_focal,
                )
            )

    async def _walk_parse_run(
        self, workspace_id: UUID, rendition, uow, closure: _Closure, problems: list[str]
    ) -> None:
        """The parse that produced a rendition, which a reader must not invent."""
        if closure.seen(RecordKind.PARSE_RUN, rendition.parse_run_id):
            return
        run = await uow.renditions.get_parse_run(workspace_id, rendition.parse_run_id)
        if run is None:
            problems.append(
                f"rendition {rendition.id} names parse run {rendition.parse_run_id}, which is "
                "absent: its text would travel without saying where it came from"
            )
            return
        closure.add(
            ParseRunRecord(
                library_id=self._library_id,
                record_id=run.id,
                asset_id=run.asset_id,
                parser_id=run.parser_id,
                parser_version=run.parser_version,
                input_sha256=run.input_sha256,
                run_fingerprint=run.run_fingerprint,
                status=_value(run.status),
                quality_metrics=run.quality_metrics.model_dump(mode="json"),
                started_at=run.started_at,
                completed_at=run.completed_at,
            )
        )

    async def _walk_claim_context(
        self, workspace_id: UUID, claim, uow, closure: _Closure, problems: list[str]
    ) -> None:
        """The entities a claim is about and the experiment it was observed in.

        Without them a measurement arrives detached from what was measured and
        under which conditions — and, on a reader that enforces its keys, does
        not arrive at all.
        """
        for entity_id in (claim.subject_entity_id, claim.unit_entity_id):
            if entity_id is None or closure.seen(RecordKind.ENTITY, entity_id):
                continue
            entity = await uow.claims.get_entity(workspace_id, entity_id)
            if entity is None:
                problems.append(f"claim {claim.id} names entity {entity_id}, which is absent")
                continue
            closure.add(
                EntityRecord(
                    library_id=self._library_id,
                    record_id=entity.id,
                    entity_type=_value(entity.entity_type),
                    preferred_label=entity.preferred_label,
                    normalised_value=entity.normalised_value,
                    ontology_namespace=entity.ontology_namespace,
                    ontology_id=entity.ontology_id,
                    resolution_status=entity.resolution_status,
                )
            )
        if claim.experiment_id is None or closure.seen(RecordKind.EXPERIMENT, claim.experiment_id):
            return
        experiment = next(
            (
                found
                for found in await uow.claims.list_experiments(workspace_id, claim.work_id)
                if found.id == claim.experiment_id
            ),
            None,
        )
        if experiment is None:
            problems.append(
                f"claim {claim.id} names experiment {claim.experiment_id}, which is absent"
            )
            return
        closure.add(
            ExperimentRecord(
                library_id=self._library_id,
                record_id=experiment.id,
                work_id=experiment.work_id,
                rendition_id=experiment.rendition_id,
                local_label=experiment.local_label,
                experiment_type=experiment.experiment_type,
                review_status=_value(experiment.review_status),
            )
        )

    async def _walk_revision(
        self,
        workspace_id: UUID,
        revision_id: UUID,
        uow,
        closure: _Closure,
        problems: list[str],
    ) -> None:
        """A revision, its claim, the revision it replaced, and its reviews.

        Walking *backwards* through `supersedes_revision_id` is the point: the
        release pins one revision, and the chain behind it is the reason the
        number is what it is.
        """
        if closure.seen(RecordKind.CLAIM_REVISION, revision_id):
            return
        revision = await uow.claims.get_revision(workspace_id, revision_id)
        if revision is None:
            problems.append(f"claim revision {revision_id} is pinned by a release and is absent")
            return
        closure.add(
            ClaimRevisionRecord(
                library_id=self._library_id,
                record_id=revision.id,
                claim_id=revision.claim_id,
                revision_number=revision.revision_number,
                value=revision.value,
                abstention_reason=_value(revision.abstention_reason),
                epistemic_status=_value(revision.epistemic_status),
                evidence_span_ids=tuple(revision.evidence_span_ids),
                proposal_source=_value(revision.proposal_source),
                extractor_fingerprint=revision.extractor_fingerprint,
                validation_status=_value(revision.validation_status),
                review_status=_value(revision.review_status),
                supersedes_revision_id=revision.supersedes_revision_id,
                created_at=revision.created_at,
            )
        )

        if not closure.seen(RecordKind.CLAIM, revision.claim_id):
            claim = await uow.claims.get_claim(workspace_id, revision.claim_id)
            if claim is None:
                problems.append(f"claim {revision.claim_id} is absent for a pinned revision")
            else:
                closure.add(
                    ClaimRecord(
                        library_id=self._library_id,
                        record_id=claim.id,
                        work_id=claim.work_id,
                        experiment_id=claim.experiment_id,
                        predicate=claim.predicate,
                        object_kind=_value(claim.object_kind),
                        subject_entity_id=claim.subject_entity_id,
                        subject_label=claim.subject_label,
                        unit_entity_id=claim.unit_entity_id,
                        qualifiers=dict(claim.qualifiers or {}),
                        polarity=_value(claim.polarity),
                        epistemic_status=_value(claim.epistemic_status),
                        template_id=claim.template_id,
                        template_version=claim.template_version,
                        target_id=claim.target_id,
                        lifecycle_status=_value(claim.lifecycle_status),
                    )
                )
                await self._walk_claim_context(workspace_id, claim, uow, closure, problems)

        for review in await uow.claims.list_reviews(workspace_id, subject_id=revision.id):
            closure.add(
                ReviewRecord(
                    library_id=self._library_id,
                    record_id=review.id,
                    revision_id=review.revision_id,
                    subject_type=_value(review.subject_type),
                    subject_id=review.subject_id,
                    decision=_value(review.decision),
                    reason_code=review.reason_code,
                    comment=review.comment,
                    before=review.before,
                    after=review.after,
                    origin_reviewer_id=review.reviewer_id,
                    reviewed_at=review.reviewed_at,
                )
            )

        # The revision this one replaced, and the one before that.
        if revision.supersedes_revision_id is not None:
            await self._walk_revision(
                workspace_id, revision.supersedes_revision_id, uow, closure, problems
            )

    # ── writing ────────────────────────────────────────────────────────────

    def _artifact(
        self,
        closure: _Closure,
        *,
        sha256: str,
        byte_length: int,
        media_type: str,
        artifact_class: ArtifactClass,
        depends_on: tuple[UUID, ...] = (),
        governed_by: tuple[UUID, ...] = (),
    ) -> UUID:
        """Reference a payload by digest, deriving a stable artifact id.

        Derived from the library and the digest rather than allocated, so two
        snapshots of the same corpus agree about which artifact is which —
        which is what lets a later publication carry only what changed.

        Identical bytes reached twice are one artifact with the *union* of
        both uses' edges. Bytes deduplicate and acquisitions do not: the same
        PDF acquired under two rights decisions is governed by both, and is
        withheld only when both withhold it (scenarios S14 and S40).
        """
        artifact_id = uuid5(_ARTIFACT_NAMESPACE, f"{self._library_id}:{sha256}")
        existing = closure.artifacts.get(artifact_id)
        if existing is None:
            closure.artifacts[artifact_id] = ArtifactRef(
                artifact_id=artifact_id,
                artifact_class=artifact_class,
                sha256=sha256,
                byte_length=byte_length,
                media_type=media_type,
                library_id=self._library_id,
                depends_on=_sorted_ids(depends_on),
                governed_by=_sorted_ids(governed_by),
            )
        elif not set(depends_on) <= set(existing.depends_on) or not set(governed_by) <= set(
            existing.governed_by
        ):
            closure.artifacts[artifact_id] = existing.model_copy(
                update={
                    "depends_on": _sorted_ids((*existing.depends_on, *depends_on)),
                    "governed_by": _sorted_ids((*existing.governed_by, *governed_by)),
                }
            )
        return artifact_id

    async def _write_payloads(
        self, workspace_id: UUID, destination: Path, closure: _Closure, problems: list[str]
    ) -> int:
        """Copy the payloads that may be replicated; mark the rest withheld.

        Lengths and media types come from the object registry, which is the
        producer's record of what it stored. Bytes are re-digested on the way
        out: a payload that rotted in the producer's store must not be the
        first thing a reader verifies.

        Tombstoned bytes do not travel, whatever their rights: the library has
        disowned them. And a payload derived from one that is withheld is
        withheld with it — a rendition is its asset's text.
        """
        now = self._context.now()
        settled: dict[UUID, ArtifactRef] = {}
        async with self._context.uow_factory() as uow:
            for artifact_id in sorted(closure.artifacts, key=str):
                ref = closure.artifacts[artifact_id]
                registered = await uow.objects.get_by_digest(workspace_id, ref.sha256)
                update: dict[str, Any] = {}
                if registered is not None:
                    update = {
                        "byte_length": registered.byte_length,
                        "media_type": registered.media_type or ref.media_type,
                    }
                reason: UnavailableReason | None = None
                if registered is not None and registered.tombstoned:
                    reason = UnavailableReason.TOMBSTONED
                elif not any(
                    (decision := closure.decisions.get(decision_id)) is not None
                    and decision.permits(PolicyAction.REPLICATE_CONTENT, now=now)
                    for decision_id in ref.governed_by
                ):
                    reason = UnavailableReason.WITHHELD_BY_RIGHTS
                if reason is not None:
                    update |= {"required": False, "unavailable": reason}
                settled[artifact_id] = ref.model_copy(update=update)

        # Derived bytes follow their source.
        for artifact_id, ref in list(settled.items()):
            for dependency in ref.depends_on:
                source = settled.get(dependency)
                if source is not None and not source.is_resolvable and ref.is_resolvable:
                    settled[artifact_id] = ref.model_copy(
                        update={"required": False, "unavailable": source.unavailable}
                    )

        copied = 0
        for artifact_id, ref in settled.items():
            closure.artifacts[artifact_id] = ref
            if not ref.is_resolvable:
                continue
            data = await self._context.object_store.get_by_digest(
                ref.sha256, workspace_id=workspace_id
            )
            if data is None:
                problems.append(
                    f"artifact {artifact_id} ({ref.sha256[:12]}…) is not in the object store; "
                    "a snapshot that names it would be published unreadable"
                )
                continue
            if sha256_bytes(data) != ref.sha256:
                problems.append(
                    f"artifact {artifact_id} does not digest to {ref.sha256[:12]}… in the "
                    "producer's own store; it is not copied"
                )
                continue
            path = destination / content_location(ref.sha256)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            closure.artifacts[artifact_id] = ref.model_copy(update={"byte_length": len(data)})
            copied += 1
        return copied

    def _write_shards(self, destination: Path, closure: _Closure) -> tuple[CatalogShardRef, ...]:
        """One shard per kind, rows sorted by id, digested over the bytes.

        Sorted so two builds of the same closure produce identical bytes: a
        snapshot whose digest changed because a dictionary iterated differently
        would make every incremental publication a full one.
        """
        shards: list[CatalogShardRef] = []
        for kind in APPLY_ORDER:
            rows = closure.rows[kind]
            if not rows:
                continue
            serialised = [
                canonical_json(rows[record_id].model_dump(mode="python")) + b"\n"
                for record_id in sorted(rows, key=str)
            ]
            location = f"{CATALOG_DIRECTORY}/{kind.value}.jsonl"
            path = destination / location
            path.write_bytes(b"".join(serialised))
            shards.append(
                CatalogShardRef(
                    kind=kind.value,
                    schema_version=CATALOG_SCHEMA_VERSION,
                    sha256=shard_digest(serialised),
                    byte_length=sum(len(row) for row in serialised),
                    row_count=len(serialised),
                    location=location,
                )
            )
        return tuple(shards)

    def _write_inventory(self, destination: Path, closure: _Closure) -> CatalogShardRef:
        """Every payload the snapshot needs, by digest, one per line.

        This is what tells an importer which bytes to fetch and which of them
        it may activate without. Sorted by artifact id so two builds of the
        same closure write identical bytes.
        """
        serialised = [
            canonical_json(closure.artifacts[artifact_id].model_dump(mode="python")) + b"\n"
            for artifact_id in sorted(closure.artifacts, key=str)
        ]
        location = f"{CATALOG_DIRECTORY}/{INVENTORY_KIND}.jsonl"
        (destination / location).write_bytes(b"".join(serialised))
        return CatalogShardRef(
            kind=INVENTORY_KIND,
            schema_version=ARCHIVE_FORMAT_VERSION,
            sha256=shard_digest(serialised),
            byte_length=sum(len(row) for row in serialised),
            row_count=len(serialised),
            location=location,
        )

    def _sign(self, manifest: SnapshotManifest) -> SnapshotManifest:
        """Attach a signature over the manifest digest *and* its identity.

        The digest alone would be replayable: the catalog is built
        deterministically, so the same bytes can appear under another library
        or sequence and an digest-only signature would vouch for them there
        too. `statement_for` binds the two together.
        """
        if self._signer is None:
            return manifest
        statement = statement_for(manifest)
        return manifest.model_copy(
            update={
                "signature": self._signer.sign(statement.to_bytes()),
                "key_identifier": self._signer.key_identifier,
            }
        )

    async def _write_projections(
        self, destination: Path, workspace_id: UUID, releases, *, servable: frozenset[UUID]
    ) -> tuple[ProjectionRef, ...]:
        """Chunks and vectors, so a reader need not re-chunk or re-embed.

        This is what makes the producer's SQL projection disposable: delete it
        and the published meaning is still here, because the passages a release
        serves and the vectors it was searched by both travelled.

        The physical index does not travel. A reader builds its own, tuned for
        its own hardware and query load; HNSW parameters are not evidence, and
        carrying them would only invite a reader to refuse vectors it could use.
        """
        index = self._context.search_index
        if index is None:
            return ()

        refs: list[ProjectionRef] = []
        for release in releases:
            # A chunk is its rendition's text, so it travels on the same terms.
            chunks = [
                chunk
                for chunk in await self._collect_chunks(index, workspace_id, release.id)
                if chunk.rendition_id in servable
            ]
            if not chunks:
                continue
            kept = {chunk.chunk_key for chunk in chunks}
            chunk_rows = [
                canonical_json(record.model_dump(mode="python")) + b"\n" for record in chunks
            ]
            base = f"{PROJECTION_DIRECTORY}/{release.id}"
            chunk_shard = _write_shard(
                destination,
                f"{base}/chunks.jsonl",
                PROJECTION_CHUNK_KIND,
                PROJECTION_SCHEMA_VERSION,
                chunk_rows,
            )

            first = chunks[0]
            for profile_id in release.projection_ready_profiles:
                vectors = [
                    vector
                    for vector in await self._collect_vectors(
                        index, workspace_id, release.id, profile_id
                    )
                    if vector.chunk_key in kept
                ]
                if not vectors:
                    continue
                vector_rows = [
                    canonical_json(record.model_dump(mode="python")) + b"\n" for record in vectors
                ]
                vector_shard = _write_shard(
                    destination,
                    f"{base}/vectors-{profile_id}.jsonl",
                    PROJECTION_VECTOR_KIND,
                    PROJECTION_SCHEMA_VERSION,
                    vector_rows,
                )
                profile = getattr(self._context.embedding_provider, "profile", None)
                refs.append(
                    ProjectionRef(
                        release_id=release.id,
                        chunker_digest=chunker_digest(first.chunker_id, first.chunker_version),
                        embedding_profile_digest=embedding_profile_digest(profile)
                        if profile is not None
                        else "",
                        embedding_dimensions=vectors[0].dimensions,
                        shards=(chunk_shard, vector_shard),
                    )
                )

            if not release.projection_ready_profiles:
                # Chunks without vectors is a real state: a release can be
                # lexically servable before any embedding profile is built.
                refs.append(
                    ProjectionRef(
                        release_id=release.id,
                        chunker_digest=chunker_digest(first.chunker_id, first.chunker_version),
                        embedding_profile_digest="",
                        embedding_dimensions=0,
                        shards=(chunk_shard,),
                    )
                )
        return tuple(refs)

    async def _collect_chunks(self, index, workspace_id: UUID, release_id: UUID):
        rows: list[ProjectionChunkRecord] = []
        after: str | None = None
        while True:
            page = await index.list_chunks(
                workspace_id, release_id, limit=DEFAULT_PAGE_SIZE, after_key=after
            )
            if not page:
                break
            for row in page:
                rows.append(
                    ProjectionChunkRecord(
                        library_id=self._library_id,
                        record_id=projection_record_id(release_id, row["chunk_key"]),
                        release_id=release_id,
                        chunk_key=row["chunk_key"],
                        work_id=UUID(str(row["work_id"])),
                        rendition_id=UUID(str(row["rendition_id"])),
                        node_ids=tuple(row.get("node_ids") or ()),
                        content=row["content"],
                        content_sha256=row["content_sha256"],
                        chunk_type=row["chunk_type"],
                        section_label=row.get("section_label"),
                        page=row.get("page"),
                        token_count=int(row.get("token_count") or 0),
                        chunker_id=row.get("chunker_id") or "unknown",
                        chunker_version=row.get("chunker_version") or "unknown",
                        access_policy_snapshot_id=(
                            UUID(str(row["access_policy_snapshot_id"]))
                            if row.get("access_policy_snapshot_id")
                            else None
                        ),
                        metadata=dict(row.get("metadata") or row.get("metadata_json") or {}),
                    )
                )
            after = page[-1]["chunk_key"]
            if len(page) < DEFAULT_PAGE_SIZE:
                break
        return sorted(rows, key=lambda record: record.chunk_key)

    async def _collect_vectors(self, index, workspace_id: UUID, release_id: UUID, profile_id: str):
        rows: list[ProjectionVectorRecord] = []
        after: str | None = None
        while True:
            page = await index.list_embeddings(
                workspace_id,
                release_id,
                profile_id=profile_id,
                limit=DEFAULT_PAGE_SIZE,
                after_key=after,
            )
            if not page:
                break
            for row in page:
                values = [float(value) for value in row["vector"]]
                rows.append(
                    ProjectionVectorRecord(
                        library_id=self._library_id,
                        record_id=projection_record_id(release_id, row["chunk_key"], profile_id),
                        release_id=release_id,
                        chunk_key=row["chunk_key"],
                        profile_id=profile_id,
                        dimensions=len(values),
                        vector=encode_vector(values),
                    )
                )
            after = page[-1]["chunk_key"]
            if len(page) < DEFAULT_PAGE_SIZE:
                break
        return sorted(rows, key=lambda record: record.chunk_key)

    def _package_refs(self, closure: _Closure) -> tuple[PackageRef, ...]:
        """One package per work, digested over everything the work carries.

        The digest is what makes an incremental publication possible: a work
        whose package digest is unchanged since the parent snapshot need not be
        written again.
        """
        by_work: dict[UUID, list[str]] = {}
        for kind in APPLY_ORDER:
            for record in closure.rows[kind].values():
                work_id = getattr(record, "work_id", None)
                if kind is RecordKind.WORK:
                    work_id = record.record_id
                if work_id is None:
                    continue
                by_work.setdefault(work_id, []).append(record.digest())
        return tuple(
            PackageRef(
                work_id=work_id,
                package_revision=1,
                manifest_digest=sha256_bytes(canonical_json(sorted(digests))),
            )
            for work_id, digests in sorted(by_work.items(), key=lambda kv: str(kv[0]))
        )


# ── helpers ────────────────────────────────────────────────────────────────


def _write_shard(
    destination: Path, location: str, kind: str, schema_version: str, rows: list[bytes]
) -> CatalogShardRef:
    """Write rows and describe them. Digested over the bytes, as everywhere."""
    path = destination / location
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(rows))
    return CatalogShardRef(
        kind=kind,
        schema_version=schema_version,
        sha256=shard_digest(rows),
        byte_length=sum(len(row) for row in rows),
        row_count=len(rows),
        location=location,
    )


def _scope_record(library_id: UUID, scope) -> ScopeVersionRecord:
    return ScopeVersionRecord(
        library_id=library_id,
        record_id=scope.id,
        scope_id=scope.scope_id,
        version=scope.version,
        name=scope.name,
        scientific_objective=scope.scientific_objective,
        concepts=tuple(scope.concepts or ()),
        exclusion_terms=tuple(getattr(scope, "exclusion_terms", ()) or ()),
        controlled_terms=tuple(scope.controlled_terms or ()),
        connector_plan=tuple(
            entry if isinstance(entry, dict) else entry.model_dump(mode="python")
            for entry in (scope.connector_plan or ())
        ),
        approval_state=_value(scope.approval_state),
        frozen_at=scope.frozen_at,
    )


def _value(item: Any) -> Any:
    """The plain value of an enum, or the item itself.

    Records are data a reader parses without importing this package, so an
    enum's *value* travels and its Python identity does not.
    """
    if item is None:
        return None
    return item.value if hasattr(item, "value") else item


def _servable_renditions(closure: _Closure) -> frozenset[UUID]:
    """Renditions whose text travels: their payload is in the snapshot."""
    servable: set[UUID] = set()
    for record in closure.rows[RecordKind.RENDITION].values():
        artifact_id = getattr(record, "artifact_id", None)
        ref = closure.artifacts.get(artifact_id) if artifact_id is not None else None
        if ref is not None and ref.is_resolvable:
            servable.add(record.record_id)
    return frozenset(servable)


def _sorted_ids(values) -> tuple[UUID, ...]:
    """Deduplicated and sorted, so two builds of one closure write one byte string."""
    return tuple(sorted(set(values), key=str))


def _inventory_digest(artifacts) -> str:
    """A digest over every required artifact, so an importer can check the set
    it was given against the set the producer meant to send."""
    return sha256_bytes(canonical_json(sorted(ref.sha256 for ref in artifacts if ref.required)))


def _write_manifest(destination: Path, manifest: SnapshotManifest) -> None:
    payload = canonical_json(manifest.model_dump(mode="python"))
    (destination / MANIFEST_NAME).write_bytes(payload)
    (destination / "MANIFEST.sha256").write_text(
        f"{sha256_bytes(payload)}  {MANIFEST_NAME}\n", encoding="utf-8"
    )


def _app_version() -> str:
    from lab_assistant import __version__

    return __version__
