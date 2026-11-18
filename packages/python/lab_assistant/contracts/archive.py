"""Portable archive format, version 1 — **draft**.

Status: draft. Nothing publishes or imports this yet; phase 2 of the shared
archive plan does. It is here now because the fixture that exercises the format
and the format itself have to be designed against each other, and because a
schema committed early can be reviewed before an implementation makes it
expensive to change.

The question this format answers is narrow and worth stating exactly: **what
does a second, empty PostgreSQL need in order to serve this corpus without
asking the producer anything?** Not "what did we store" — the object store
already holds the bytes, and a bucket full of payloads tells a reader nothing
about which work they belong to, which revision superseded which, or who may see
them. The meaning is in the database, and this format is how the meaning travels.

Four rules shape it.

**Addressing is not identity.** A bucket name, an endpoint, a prefix and a
presigned URL are all addresses, and none of them belongs in a paper's identity.
Artifacts are named by digest and by logical id; where the bytes currently sit is
the importer's problem, not the archive's.

**Every reference resolves or is explicitly classified.** A dependency that is
missing is a different thing from one deliberately withheld under rights, which
is different again from one that expired. An importer that cannot tell them apart
either refuses a perfectly good archive or serves a hole. So `ArtifactRef` carries
a reason when the bytes are absent, and an unresolvable *required* reference is a
validation failure rather than a warning.

**A hash is not a signature.** The digest proves the bytes did not rot. It says
nothing about who approved them. Bucket write access is not evidence that a
steward reviewed a claim, so the manifest carries a signature over its own digest
and the identity fields, verified against keys pinned in configuration.

**Negative and superseded evidence travels too.** A package that carried only
current accepted values would lose the rejected proposal, the reviewer's
correction and the retraction — which is most of what makes the corpus evidence
rather than a table of numbers.

What this draft does **not** attempt: the publication protocol (prepared →
committed → published, the head compare-and-swap, writer epochs), the importer's
staging and generation activation, and the policy/revocation stream. Those are
phase 2 and phase 3, and each needs its own contract.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "ARCHIVE_FORMAT_NAME",
    "ARCHIVE_FORMAT_VERSION",
    "MINIMUM_READER_PROTOCOL",
    "ArtifactClass",
    "ArtifactRef",
    "CatalogShardRef",
    "PackageRef",
    "ProjectionRef",
    "SnapshotManifest",
    "StageOutcome",
    "StageResultEnvelope",
    "UnavailableReason",
]

ARCHIVE_FORMAT_NAME = "lab-assistant-library"
#: Major.minor. A reader refuses an unknown **major**; it may accept an unknown
#: minor and must preserve fields it does not understand, or a round trip through
#: an older reader silently drops what a newer producer added.
ARCHIVE_FORMAT_VERSION = "1.0"
#: The oldest reader protocol that can serve this format.
MINIMUM_READER_PROTOCOL = "1.0"


class ArtifactClass(StrEnum):
    """What an artifact is, which decides how an importer may use it.

    Deliberately not the object store's ``object_class``: that records what one
    writer called a payload, while this records the role the payload plays in
    this package. The same bytes can be both an acquired asset here and a cited
    attachment there, and a single label on the payload would erase one of them.
    """

    SOURCE_RESPONSE = "source_response"
    CANONICAL_RECORD = "canonical_record"
    ASSET = "asset"
    RENDITION = "rendition"
    EVIDENCE_CONTEXT = "evidence_context"
    MODEL_ENVELOPE = "model_envelope"
    RELEASE_MANIFEST = "release_manifest"
    VALIDATION_REPORT = "validation_report"
    PROJECTION_SHARD = "projection_shard"
    POLICY_CHECKPOINT = "policy_checkpoint"
    CATALOG_SHARD = "catalog_shard"


class UnavailableReason(StrEnum):
    """Why an artifact's bytes are not in this archive.

    ``WITHHELD_BY_RIGHTS`` is the one that matters most: it is not an error, and
    an importer that treated it as one would refuse a valid archive, while one
    that treated it as ``MISSING`` would report corruption to an operator who
    would then go looking for a bug.
    """

    WITHHELD_BY_RIGHTS = "withheld_by_rights"
    WITHHELD_BY_RETENTION = "withheld_by_retention"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    TOMBSTONED = "tombstoned"
    #: Required, and not here. A validation failure, not a classification.
    MISSING = "missing"


class ArtifactRef(BaseModel):
    """One immutable payload, named by content rather than by location."""

    model_config = {"frozen": True}

    artifact_id: UUID
    artifact_class: ArtifactClass
    sha256: str
    byte_length: int
    media_type: str
    #: The library this artifact belongs to. Deduplication is within a namespace:
    #: identical bytes in two libraries are two artifacts, because collapsing
    #: them would couple their permissions, retention and deletion.
    library_id: UUID
    #: ``False`` means an importer may activate the snapshot without these bytes.
    required: bool = True
    #: Set when the bytes are not in the archive. Required reading before an
    #: importer decides whether this is a failure.
    unavailable: UnavailableReason | None = None
    #: What this artifact needs in order to mean anything: a rendition's asset,
    #: an evidence span's rendition. Resolved within the snapshot closure.
    depends_on: tuple[UUID, ...] = ()
    #: The provider's version token, if the producer recorded one. Never a
    #: substitute for ``sha256``: an ETag is not the digest of a multipart upload.
    storage_version: str | None = None

    @property
    def is_resolvable(self) -> bool:
        return self.unavailable is None

    @property
    def blocks_import(self) -> bool:
        return self.required and not self.is_resolvable


class StageOutcome(StrEnum):
    """How an expensive stage ended.

    ``SUCCEEDED_EMPTY`` exists because it is the case every naive cache gets
    wrong. A paper the extractor read and found nothing in has been *done*; an
    archive that cannot say so makes every reader pay to do it again, forever.
    ``FAILED_RETRYABLE`` is its opposite: not a result, and never a cache hit.
    """

    SUCCEEDED = "succeeded"
    SUCCEEDED_EMPTY = "succeeded_empty"
    SUCCEEDED_TRUNCATED = "succeeded_truncated"
    REFUSED_BY_POLICY = "refused_by_policy"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_PERMANENT = "failed_permanent"
    #: The request left this installation and its answer did not come back. The
    #: provider may have charged for it. Honest, and not reusable.
    OUTCOME_UNKNOWN = "outcome_unknown"


class StageResultEnvelope(BaseModel):
    """A completed expensive stage, recorded so nobody pays for it twice.

    The reuse fingerprint is the whole point, and it has to cover everything that
    would change the answer: the input bytes, the recipe, the actual tool and
    model versions, and the policy scope under which it ran. A fingerprint over
    the input alone would reuse a result produced by a model that has since been
    replaced, which is how a corpus acquires numbers nobody can account for.
    """

    model_config = {"frozen": True}

    stage: str
    outcome: StageOutcome
    #: Why a failure outcome failed, from the shared taxonomy. Carried because
    #: a reused `FAILED_PERMANENT` must be surfaced as the failure it was — "we
    #: know this fails" and "this succeeded" are different answers, and a
    #: caller that could not tell them apart would report a permanent failure
    #: as a cache hit.
    error_class: str | None = None
    #: Everything that invalidates reuse, hashed. See ``provenance.fingerprints``.
    reuse_fingerprint: str
    input_artifact_digests: tuple[str, ...] = ()
    output_artifacts: tuple[UUID, ...] = ()
    #: Actual versions, not the configured defaults. "What we asked for" and
    #: "what ran" diverge, and only the second one explains an output.
    tool_versions: dict[str, str] = Field(default_factory=dict)
    policy_scope: dict[str, Any] = Field(default_factory=dict)
    #: The provider's own request id where one exists, so a disputed charge or a
    #: lost response can be reconciled against their records rather than ours.
    provider_request_id: str | None = None
    token_usage: dict[str, int] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    actor_id: UUID | None = None
    #: Conditions under which this result may be reused by an importer at all;
    #: empty means the snapshot's own policy governs.
    reuse_restrictions: tuple[str, ...] = ()

    @property
    def is_reusable(self) -> bool:
        return self.outcome in (
            StageOutcome.SUCCEEDED,
            StageOutcome.SUCCEEDED_EMPTY,
            StageOutcome.SUCCEEDED_TRUNCATED,
        )


class PackageRef(BaseModel):
    """One work's immutable package revision within a snapshot."""

    model_config = {"frozen": True}

    work_id: UUID
    #: Monotonic per work. A correction is a new revision, never an edit, so a
    #: citation minted against revision 3 keeps meaning revision 3.
    package_revision: int
    manifest_digest: str
    #: Carried so an incremental publication can skip a package whose digest is
    #: unchanged without opening it.
    unchanged_since_parent: bool = False


class CatalogShardRef(BaseModel):
    """A shard of the snapshot catalog: typed records, never SQL or code.

    An archive is data from an authority the reader has only partly verified, so
    nothing in it is executed. No SQL statements, no pickles, no parser plugins —
    a shard is rows, validated against a schema, with its size declared before
    anything is allocated to hold it.
    """

    model_config = {"frozen": True}

    kind: str
    schema_version: str
    sha256: str
    byte_length: int
    row_count: int
    location: str


class ProjectionRef(BaseModel):
    """Reusable chunk and embedding shards for one release.

    ``embedding_profile_digest`` is the compatibility gate and dimension is not:
    two models can produce 768-dimension vectors that mean entirely different
    things, and an importer that matched on dimension would mix them into one
    index and return confident nonsense.
    """

    model_config = {"frozen": True}

    release_id: UUID
    chunker_digest: str
    embedding_profile_digest: str
    embedding_dimensions: int
    shards: tuple[CatalogShardRef, ...] = ()


class SnapshotManifest(BaseModel):
    """A consistent, committed catalog of one library at one publication.

    This is the single object whose digest the publication head commits to, so
    everything an importer must agree about before it writes a row is here:
    format and protocol, identity and sequence, the policy checkpoint in force,
    and the digests of every shard and package in the closure.
    """

    model_config = {"frozen": True}

    format_name: str = ARCHIVE_FORMAT_NAME
    format_version: str = ARCHIVE_FORMAT_VERSION
    minimum_reader_protocol: str = MINIMUM_READER_PROTOCOL
    #: Features a reader must implement to activate this snapshot. A reader that
    #: does not recognise one refuses rather than importing a partial corpus.
    required_features: tuple[str, ...] = ()

    library_id: UUID
    source_workspace_id: UUID
    snapshot_id: UUID
    #: The installation that produced it, and the epoch of its writer authority.
    #: A clone of a backup is a new installation; copying a database does not
    #: copy the right to publish.
    origin_installation_id: UUID
    writer_epoch: int
    #: Monotonic per library. A reader that sees a sequence lower than the one it
    #: already activated is looking at a replay and must not roll back to it.
    publication_sequence: int
    parent_snapshot_id: UUID | None = None
    created_at: datetime | None = None
    producer_app_version: str | None = None

    scientific_release_ids: tuple[UUID, ...] = ()
    selected_alias_targets: dict[str, UUID] = Field(default_factory=dict)

    #: Rights can become stricter after a release is published, so policy travels
    #: on its own sequence and a reader checks its freshness independently of the
    #: catalog. A scientific rollback must not restore revoked permissions.
    policy_sequence: int = 0
    policy_artifact_digest: str | None = None

    catalog_shards: tuple[CatalogShardRef, ...] = ()
    package_refs: tuple[PackageRef, ...] = ()
    projection_refs: tuple[ProjectionRef, ...] = ()

    #: Digest over the sorted required artifact inventory, so an importer can
    #: detect a truncated upload before it begins.
    required_object_inventory_digest: str | None = None
    missing_optional_artifacts: tuple[UUID, ...] = ()

    provenance_checkpoint: str | None = None
    #: Over the manifest digest and the identity fields. Hashes detect rot;
    #: signatures establish who approved the science.
    signature: str | None = None
    key_identifier: str | None = None

    def signing_digest(self) -> str:
        """The digest a signature covers: everything *except* the signature.

        Excluded because a signature inside the thing it signs changes that
        thing. It is also the digest the head records, so a snapshot's identity
        does not shift when somebody signs it — an unsigned snapshot and the
        same snapshot signed are the same snapshot.
        """
        from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

        payload = self.model_dump(mode="python")
        payload.pop("signature", None)
        payload.pop("key_identifier", None)
        return sha256_bytes(canonical_json(payload))

    @property
    def is_signed(self) -> bool:
        return bool(self.signature and self.key_identifier)
