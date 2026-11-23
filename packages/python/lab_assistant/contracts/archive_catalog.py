"""The typed rows a catalog shard holds (ADR pending, phase 2).

`archive.py` describes the *shape* of a snapshot: a manifest, shard references,
artifact references. This is what is inside a shard — the records that carry the
meaning, because the bytes in the object store do not.

The question the whole file answers: **what does a second, empty PostgreSQL need
in order to serve this corpus without asking the producer anything?** A bucket
full of payloads tells a reader nothing about which work they belong to, which
revision superseded which, who reviewed what, or who may see it. That lives in
the producer's database today, and these records are how it travels.

Four rules, carried over from `archive.py` and made concrete here.

**Records, never statements.** A shard is JSON Lines: one canonical JSON object
per row, each validated against a schema before anything acts on it. No SQL, no
pickles, no code. An archive is data from an authority the reader has verified
only partly, and the cheapest way to keep that true is to have nothing in the
format that *could* be executed.

**Identity travels; addresses do not.** A record names other records by id, and
payloads by digest. Where the bytes currently sit — bucket, prefix, endpoint —
is the importer's problem and appears nowhere in a record.

**Superseded and negative evidence travel too.** A catalog that carried only
current accepted values would lose the rejected proposal, the reviewer's
correction, and the retraction — which is most of what makes this a corpus of
evidence rather than a table of numbers. So revisions are rows, reviews are
rows, and an abstention is a row.

**Every row says what it is.** `kind` and `schema_version` are on the record,
not only on the shard that holds it, so a row pulled out of context is still
self-describing and a reader that does not understand a kind can skip it
without guessing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "AssetRecord",
    "CatalogRecord",
    "ClaimRecord",
    "ClaimRevisionRecord",
    "EntityRecord",
    "ExperimentRecord",
    "IdentifierEntry",
    "ManifestationRecord",
    "ParseRunRecord",
    "RecordKind",
    "ReleaseEntryRecord",
    "ReleaseRecord",
    "RenditionRecord",
    "ReviewRecord",
    "RightsDecisionRecord",
    "ScopeVersionRecord",
    "SpanRecord",
    "WorkRecord",
    "record_for",
    "shard_digest",
]

#: Major.minor of the record schemas. A reader refuses an unknown major and may
#: accept an unknown minor, preserving fields it does not understand — or a
#: round trip through an older reader silently drops what a newer producer
#: added.
CATALOG_SCHEMA_VERSION = "1.0"


class RecordKind(StrEnum):
    """One shard per kind, so a reader can import what it understands.

    Ordered the way a reader must apply them: a rendition means nothing before
    its asset exists, and a claim revision means nothing before its claim.
    """

    WORK = "work"
    MANIFESTATION = "manifestation"
    ASSET = "asset"
    RIGHTS_DECISION = "rights_decision"
    PARSE_RUN = "parse_run"
    RENDITION = "rendition"
    SPAN = "span"
    ENTITY = "entity"
    EXPERIMENT = "experiment"
    CLAIM = "claim"
    CLAIM_REVISION = "claim_revision"
    REVIEW = "review"
    SCOPE_VERSION = "scope_version"
    RELEASE = "release"
    RELEASE_ITEM = "release_item"


#: The order an importer applies shards in. A reader that applied these in
#: filesystem order would reference rows it has not written yet, and the first
#: symptom would be a foreign key failure halfway through an import.
#:
#: Rights decisions come before assets: an asset names its decision, and a
#: decision names its subject only by id. An earlier version had the two the
#: other way round, which no import had yet run against a database that
#: enforces the key.
APPLY_ORDER: tuple[RecordKind, ...] = (
    RecordKind.SCOPE_VERSION,
    RecordKind.WORK,
    RecordKind.MANIFESTATION,
    RecordKind.RIGHTS_DECISION,
    RecordKind.ASSET,
    RecordKind.PARSE_RUN,
    RecordKind.RENDITION,
    RecordKind.SPAN,
    RecordKind.ENTITY,
    RecordKind.EXPERIMENT,
    RecordKind.CLAIM,
    RecordKind.CLAIM_REVISION,
    RecordKind.REVIEW,
    RecordKind.RELEASE,
    RecordKind.RELEASE_ITEM,
)


class CatalogRecord(BaseModel):
    """What every catalog row carries, whatever kind it is."""

    model_config = {"frozen": True}

    kind: RecordKind
    schema_version: str = CATALOG_SCHEMA_VERSION
    #: The library this row belongs to. References are resolved within a
    #: library namespace: the same work id in two libraries is two works, and
    #: collapsing them would couple their rights and their retention.
    library_id: UUID
    #: The row's own identity inside that namespace.
    record_id: UUID

    def digest(self) -> str:
        """A stable digest of this row, for shard-level change detection."""
        return sha256_bytes(canonical_json(self.model_dump(mode="python")))


class IdentifierEntry(BaseModel):
    """One identifier of a work, as the producer normalised it."""

    model_config = {"frozen": True}

    scheme: str
    value_display: str
    value_normalized: str
    normalisation_version: str
    status: str = "active"


class WorkRecord(CatalogRecord):
    """A scholarly work: the thing a citation names."""

    kind: RecordKind = RecordKind.WORK
    work_kind: str
    #: Reconciled from source assertions rather than stored on the work: the
    #: producer has no single authoritative title, it has what several sources
    #: said. What travels is the reconciliation, because a reader cannot redo it
    #: without the observations — which are the producer's, not the library's.
    title: str | None = None
    publication_year: int | None = None
    container_title: str | None = None
    identity_status: str | None = None
    #: A retraction is part of what a reader must show. Losing it would have a
    #: reader serving a retracted paper as current evidence.
    retraction_status: str | None = None
    correction_status: str | None = None
    #: The strong and weak identifiers that let an importer recognise a work it
    #: already has — and the only basis on which it may merge one
    #: (`domain/identity.py`). Normalised values travel with the display ones:
    #: a reader that re-normalised would be applying its own version of a
    #: contract the producer already applied.
    identifiers: tuple[IdentifierEntry, ...] = ()
    #: Typed edges to other works: a version of, a preprint of, a retraction of.
    #: Lose these and a reader cannot tell a correction from a new paper.
    relations: tuple[dict[str, Any], ...] = ()


class ManifestationRecord(CatalogRecord):
    """One concrete form of a work: a version of record, a preprint, an accepted
    manuscript. The level at which rights actually differ."""

    kind: RecordKind = RecordKind.MANIFESTATION
    work_id: UUID
    manifestation_kind: str
    version_label: str | None = None
    publication_state: str | None = None
    declared_mime: str | None = None
    language: str | None = None
    status: str | None = None


class AssetRecord(CatalogRecord):
    """Acquired bytes, named by digest rather than by where they were fetched."""

    kind: RecordKind = RecordKind.ASSET
    work_id: UUID | None = None
    manifestation_id: UUID | None = None
    sha256: str
    byte_length: int
    media_type: str | None = None
    #: The rights decision that permitted this asset to be processed. The
    #: decision itself travels as its own record; this is the edge.
    rights_decision_id: UUID | None = None
    malware_scan_status: str | None = None
    #: The artifact in the snapshot's inventory holding these bytes, when they
    #: travel. Absent means the record is present and the payload is not — a
    #: different thing from the asset not existing, and the manifest's
    #: `missing_optional_artifacts` says which.
    artifact_id: UUID | None = None
    created_at: datetime | None = None


class RightsDecisionRecord(CatalogRecord):
    """Why this material may be processed and who may see it.

    Travels with the asset and is applied by the importer. An archive that
    carried evidence without its rights decision would have a reader serving
    material the publisher restricted (scenario S39), and "it was in the
    archive" is not a licence.
    """

    kind: RecordKind = RecordKind.RIGHTS_DECISION
    #: What the decision is about: a manifestation or an asset.
    subject_type: str
    subject_id: UUID
    access_basis: str
    licence_expression: str | None = None
    #: The actions this decision permits, by name. An importer applies these
    #: rather than re-deriving them: the producer's licence analysis is part of
    #: the record, and a reader that guessed would guess generously.
    allowed_actions: tuple[str, ...] = ()
    restrictions: tuple[str, ...] = ()
    #: Groups the material is restricted to. Empty means the workspace default
    #: applies, not that it is unrestricted.
    restricted_to_group_ids: tuple[UUID, ...] = ()
    status: str | None = None
    decided_at: datetime | None = None
    #: When the permission lapses. An importer must treat an expired decision as
    #: withheld rather than as absent — a distinction `UnavailableReason` keeps.
    review_due_at: datetime | None = None


class ParseRunRecord(CatalogRecord):
    """The parse that produced a rendition: which parser, on which bytes.

    Travels because a rendition is not evidence on its own say-so. "This text
    came from these bytes through this parser version" is what lets a reader
    tell a rendition it may reuse from one a parser fix has made stale — and
    what it would otherwise have to invent, which is the one thing provenance
    must never be.
    """

    kind: RecordKind = RecordKind.PARSE_RUN
    asset_id: UUID
    parser_id: str
    parser_version: str
    input_sha256: str
    run_fingerprint: str
    status: str
    quality_metrics: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class RenditionRecord(CatalogRecord):
    """The normalised text an evidence span points into.

    `normalisation_version` is load-bearing: a span is an offset into *this*
    normalisation, and resolving it under a different one silently moves the
    quote (ADR-0010).
    """

    kind: RecordKind = RecordKind.RENDITION
    asset_id: UUID
    #: The parse that produced it. Required: a rendition without one is text of
    #: unknown origin.
    parse_run_id: UUID | None = None
    work_id: UUID | None = None
    sha256: str
    #: The rendition document format and its schema version, which is what a
    #: reader needs to parse the payload at all.
    format: str
    schema_version: str
    quality_status: str | None = None
    #: A degraded parse that a steward accepted. The acceptance is part of the
    #: record: a reader showing evidence from it should be able to say so.
    accepted_degraded_reason: str | None = None
    supersedes_rendition_id: UUID | None = None
    artifact_id: UUID | None = None
    language: str | None = None
    page_count: int = 0
    section_count: int = 0
    table_count: int = 0
    figure_count: int = 0
    created_at: datetime | None = None


class SpanRecord(CatalogRecord):
    """Where in a rendition a claim's evidence is, and what it said.

    The quote is carried as well as the offsets. Offsets alone cannot be
    verified by a reader that failed to resolve the rendition, and a citation
    that cannot be checked is the thing this system exists to prevent.
    """

    kind: RecordKind = RecordKind.SPAN
    rendition_id: UUID
    node_id: str
    start_offset: int
    end_offset: int
    #: The quote's digest and a short preview, which is what the producer
    #: stores. The full text is *not* carried: a span is verified by resolving
    #: it against the rendition under `normalisation_version`, and shipping the
    #: text would invite a reader to trust it instead of checking.
    quote_sha256: str
    quote_preview: str | None = None
    normalisation_version: str
    #: Structured, not a string: a page and a node id resolve differently from a
    #: character offset, and flattening them would make a locator unparseable
    #: by the reader that has to follow it.
    locator: dict[str, Any] | None = None
    is_focal: bool = True


class EntityRecord(CatalogRecord):
    """A thing claims are about or measured in: a strain, a compound, a unit."""

    kind: RecordKind = RecordKind.ENTITY
    entity_type: str
    preferred_label: str
    normalised_value: str
    ontology_namespace: str | None = None
    ontology_id: str | None = None
    resolution_status: str = "unresolved"


class ExperimentRecord(CatalogRecord):
    """The experiment a claim was observed in.

    A per-experiment template groups its claims by experiment, so a claim that
    arrived without the experiment it names would be a measurement detached
    from its conditions.
    """

    kind: RecordKind = RecordKind.EXPERIMENT
    work_id: UUID
    rendition_id: UUID | None = None
    local_label: str
    experiment_type: str | None = None
    review_status: str | None = None


class ClaimRecord(CatalogRecord):
    """A statement the corpus makes, independent of its current value."""

    kind: RecordKind = RecordKind.CLAIM
    work_id: UUID
    experiment_id: UUID | None = None
    predicate: str
    object_kind: str | None = None
    subject_entity_id: UUID | None = None
    subject_label: str | None = None
    unit_entity_id: UUID | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    polarity: str | None = None
    epistemic_status: str | None = None
    template_id: str | None = None
    template_version: str | None = None
    target_id: str | None = None
    lifecycle_status: str | None = None


class ClaimRevisionRecord(CatalogRecord):
    """One version of a claim's value, including the ones no longer current.

    Superseded revisions travel. A corpus that shipped only current values
    would lose the reason a number changed, which is most of what makes a
    correction auditable.
    """

    kind: RecordKind = RecordKind.CLAIM_REVISION
    claim_id: UUID
    revision_number: int
    #: Absent for an abstention, which is a positive statement that the source
    #: does not report the value rather than a missing row.
    value: dict[str, Any] | None = None
    #: Why no value. `NOT_REPORTED_AFTER_COMPLETE_SEARCH` is a result, not a
    #: gap: a reader that cannot tell it from "not yet extracted" makes every
    #: importer pay to re-run the extraction (scenario S12).
    abstention_reason: str | None = None
    epistemic_status: str | None = None
    evidence_span_ids: tuple[UUID, ...] = ()
    proposal_source: str | None = None
    extractor_fingerprint: str | None = None
    validation_status: str | None = None
    review_status: str | None = None
    #: The revision this one replaced. Superseded revisions travel, so the
    #: reason a number changed survives the journey.
    supersedes_revision_id: UUID | None = None
    created_at: datetime | None = None


class ReviewRecord(CatalogRecord):
    """A reviewer's decision on a revision, and the reason given.

    Attribution, not an account: a reviewer's historical identity belongs to
    the record, and an importer must not turn it into a login on the reader.
    """

    kind: RecordKind = RecordKind.REVIEW
    revision_id: UUID | None = None
    subject_type: str | None = None
    subject_id: UUID | None = None
    decision: str
    reason_code: str | None = None
    comment: str | None = None
    #: What changed, as the reviewer saw it. The correction and the proposal it
    #: replaced are both part of the record.
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    #: The origin installation's reviewer id, carried as attribution. It is not
    #: a user on the importing installation and must never be mapped to one
    #: automatically — an account is not provenance.
    origin_reviewer_id: UUID | None = None
    reviewed_at: datetime | None = None


class ScopeVersionRecord(CatalogRecord):
    """The frozen question a release answers.

    Without it a reader can show what was found and not what was looked for,
    and "nothing was found" is uninterpretable.
    """

    kind: RecordKind = RecordKind.SCOPE_VERSION
    scope_id: UUID | None = None
    version: int | None = None
    name: str
    scientific_objective: str | None = None
    inclusion_criteria: tuple[str, ...] = ()
    exclusion_criteria: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()
    exclusion_terms: tuple[str, ...] = ()
    controlled_terms: tuple[str, ...] = ()
    connector_plan: tuple[dict[str, Any], ...] = ()
    approval_state: str | None = None
    frozen_at: datetime | None = None


class ReleaseRecord(CatalogRecord):
    """A published corpus release: immutable, and cited by id."""

    kind: RecordKind = RecordKind.RELEASE
    name: str
    semantic_version: str
    status: str
    manifest_sha256: str | None = None
    scope_version_ids: tuple[UUID, ...] = ()
    #: The release this one succeeded. Optional within a snapshot: a snapshot of
    #: one release still names the release it replaced.
    parent_release_id: UUID | None = None
    embedding_profile_ids: tuple[str, ...] = ()
    projection_ready_profiles: tuple[str, ...] = ()
    published_at: datetime | None = None


class ReleaseEntryRecord(CatalogRecord):
    """One work as a release serves it, pinned to the revisions of that moment.

    Pinned deliberately: a citation into last quarter's release must resolve to
    what it said then, so an entry names revisions rather than claims.

    Named `ReleaseEntryRecord` rather than `ReleaseItemRecord` because
    `contracts/release.py` already has the latter and they are not the same
    thing: that one is the producer's manifest line, this one is the portable
    row. Two types with one name is how somebody later maps the wrong fields.
    """

    kind: RecordKind = RecordKind.RELEASE_ITEM
    release_id: UUID
    work_id: UUID
    manifestation_id: UUID | None = None
    asset_id: UUID | None = None
    rendition_id: UUID | None = None
    rendition_digest: str | None = None
    claim_revision_ids: tuple[UUID, ...] = ()
    #: The rights decision in force for this entry's asset when the release was
    #: drafted. Part of the producer's entry digest, so it travels too.
    access_policy_snapshot_id: UUID | None = None
    inclusion_reason: str = "steward_selected"
    #: The producer's own digest of the entry, carried rather than recomputed:
    #: it is what the producer's incremental builds compared, and a reader that
    #: derived its own would disagree with it the first time a field the digest
    #: covers failed to travel.
    content_digest: str | None = None


_BY_KIND: dict[RecordKind, type[CatalogRecord]] = {
    RecordKind.WORK: WorkRecord,
    RecordKind.MANIFESTATION: ManifestationRecord,
    RecordKind.ASSET: AssetRecord,
    RecordKind.RIGHTS_DECISION: RightsDecisionRecord,
    RecordKind.PARSE_RUN: ParseRunRecord,
    RecordKind.RENDITION: RenditionRecord,
    RecordKind.SPAN: SpanRecord,
    RecordKind.ENTITY: EntityRecord,
    RecordKind.EXPERIMENT: ExperimentRecord,
    RecordKind.CLAIM: ClaimRecord,
    RecordKind.CLAIM_REVISION: ClaimRevisionRecord,
    RecordKind.REVIEW: ReviewRecord,
    RecordKind.SCOPE_VERSION: ScopeVersionRecord,
    RecordKind.RELEASE: ReleaseRecord,
    RecordKind.RELEASE_ITEM: ReleaseEntryRecord,
}


def record_for(kind: RecordKind | str) -> type[CatalogRecord]:
    """The model a shard of this kind holds.

    Raises `KeyError` for an unknown kind, which the validator turns into a
    finding: a reader that silently skipped a kind it did not recognise would
    import a corpus missing something nobody can name.
    """
    return _BY_KIND[RecordKind(kind)]


def shard_digest(rows: list[bytes]) -> str:
    """The digest of a shard's serialised rows, in the order written.

    Over the bytes rather than over the parsed records, so a reader checks
    exactly what it received: a digest computed after parsing would pass on a
    file whose formatting changed under it, which is precisely when something
    has gone wrong.
    """
    return sha256_bytes(b"".join(rows))
