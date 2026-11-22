"""Repository and infrastructure ports.

Every repository method takes ``workspace_id`` as a required argument, never an
optional filter. That signature is the irreversible half of tenancy-readiness:
adding the column later is a migration, but retrofitting the *call sites* is a
rewrite (scale §3.3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from lab_assistant.contracts.archive import StageResultEnvelope
from lab_assistant.contracts.archive_import import ImportRecord
from lab_assistant.contracts.events import AuditEventRecord, OutboxEvent
from lab_assistant.contracts.policy import GrantSpec
from lab_assistant.contracts.publication import PublicationRecord
from lab_assistant.contracts.runtime import Clock, IdFactory
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.assets import (
    AcquisitionAttempt,
    AcquisitionPlan,
    Asset,
    Manifestation,
    RightsDecision,
)
from lab_assistant.domain.chat import ChatMessage, ChatSession, Feedback, UsageTotals
from lab_assistant.domain.claims import (
    Claim,
    ClaimRevision,
    Entity,
    Experiment,
    Review,
)
from lab_assistant.domain.identity import (
    IdentityDecision,
    Work,
    WorkIdentifier,
    WorkRelation,
)
from lab_assistant.domain.objects import ObjectRecord
from lab_assistant.domain.observation import (
    DiscoveryRun,
    SourceAssertion,
    SourceObservation,
    SourceRequest,
)
from lab_assistant.domain.release import CorpusRelease, ReleaseItem, SearchAlias
from lab_assistant.domain.renditions import EvidenceSpan, ParseRun, Rendition
from lab_assistant.domain.scope import ConfigVersion, ScopeVersion
from lab_assistant.domain.tasks import RateBudget, Task
from lab_assistant.domain.workspace import (
    Group,
    Invitation,
    Membership,
    User,
    Workspace,
)

__all__ = [
    "ArchiveControlStore",
    "ArchiveDeletionStore",
    "ArchiveImportRepository",
    "AssetRepository",
    "AuditRepository",
    "ChatRepository",
    "ClaimRepository",
    "Clock",
    "ConfigRepository",
    "DatabaseFingerprint",
    "DatabaseSnapshot",
    "DumpResult",
    "EvaluationRepository",
    "IdFactory",
    "ObjectRecord",
    "ObjectReference",
    "ObjectReferenceSource",
    "ObjectRegistryRepository",
    "ObjectStore",
    "ObservationRepository",
    "PolicyRepository",
    "PublicationRepository",
    "ReleaseRepository",
    "RenditionRepository",
    "SearchIndex",
    "StageResultRepository",
    "StoredObject",
    "SupportsInventory",
    "TaskRepository",
    "UnitOfWork",
    "UserRepository",
    "VersionedPayload",
    "WorkRepository",
    "WorkVisibilityRow",
    "WorkspaceRepository",
]


class StoredObject(Protocol):
    """Metadata of an immutable object in content-addressed storage.

    ``key`` is the backend's address for the bytes. It is part of the protocol
    because the database records it: a restore has to be able to say *where* a
    missing object was expected, and "somewhere in the store" is not an answer.
    """

    id: UUID
    workspace_id: UUID | None
    sha256: str
    byte_length: int
    media_type: str
    object_class: str
    created_at: datetime
    key: str


@runtime_checkable
class ObjectStore(Protocol):
    """Immutable, content-addressed storage.

    ``put`` is idempotent by digest: storing the same bytes twice yields the same
    key and never overwrites. There is no ``overwrite`` operation, because
    citation stability depends on bytes never changing under a key.
    """

    async def put(
        self,
        data: bytes,
        *,
        workspace_id: UUID | None,
        object_class: str,
        media_type: str = "application/octet-stream",
    ) -> StoredObject: ...

    async def put_stream(
        self,
        source: AsyncIterator[bytes],
        *,
        workspace_id: UUID | None,
        object_class: str,
        media_type: str = "application/octet-stream",
        max_bytes: int | None = None,
        created_at: datetime | None = None,
    ) -> StoredObject:
        """Store a payload without holding it in memory.

        The key is the digest and the digest is only known at the end, so the
        payload is spooled — in memory while it is small, on disk once it is
        not — and the limit is enforced while reading rather than after. Same
        idempotence as ``put``: the same bytes twice are one object.

        ``created_at`` exists for one caller: a migration copying an object
        between stores, which must not restamp evidence with the hour it was
        moved. Everything else leaves it unset and takes the store's clock.
        """
        ...

    async def get(self, object_id: UUID) -> bytes: ...

    def stream(self, object_id: UUID, *, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        """Yield the object's bytes, verifying the digest as they pass.

        The verification can only fail at the end, by which time the consumer
        holds most of the payload. So the contract is explicit: a stream that
        raises has delivered bytes that must be discarded, not cached and not
        served. A caller that cannot honour that — anything that treats a
        partial read as a result — uses ``get`` and pays the memory.
        """
        ...

    async def get_by_digest(self, sha256: str, *, workspace_id: UUID | None) -> bytes | None: ...

    async def head(self, object_id: UUID) -> StoredObject | None: ...

    async def verify(self, object_id: UUID) -> bool:
        """Recompute the digest of stored bytes and compare with the record."""
        ...

    async def signed_url(self, object_id: UUID, *, expires_seconds: int) -> str: ...

    async def tombstone(self, object_id: UUID, *, reason: str) -> None:
        """Mark for purge. Never an in-place overwrite."""
        ...


@dataclass(frozen=True, slots=True)
class DatabaseFingerprint:
    """What the running database says about itself.

    Read from the server rather than from configuration, because the whole point
    is to catch the case where the two disagree: a compose file declaring
    pgvector 0.8 against a server holding 0.5 restores cleanly and then fails at
    the first vector index — after the migrations, and usually after somebody
    has told the lab it is back.
    """

    server_version: str
    #: Extension name to installed version, e.g. ``{"vector": "0.8.2"}``.
    extensions: dict[str, str] = field(default_factory=dict)
    #: The Alembic revision the database is stamped at. ``None`` when there is no
    #: ``alembic_version`` row — a schema built by ``lab db create``, which is a
    #: finding rather than an error.
    schema_revision: str | None = None
    #: Application tables already present in the destination. A logical restore
    #: goes into an *empty* database: replaying a dump over existing tables fails
    #: on the first ``CREATE TABLE``, and it fails partway, after a restore has
    #: already copied the object payloads in.
    relation_count: int = 0


@dataclass(frozen=True, slots=True)
class DumpResult:
    """The outcome of writing a logical dump to a path."""

    path: str
    byte_length: int
    sha256: str
    #: The dump program's own version banner. A dump written by a newer
    #: ``pg_dump`` than the destination's ``pg_restore`` fails partway through a
    #: restore; recorded so a preflight can say so before anything is dropped.
    tool_version: str


@dataclass(frozen=True, slots=True)
class VersionedPayload:
    """Bytes plus the provider's opaque version token for them.

    The token is whatever the backend uses to say "this exact version": an S3
    ETag, a GCS generation, a content digest on a filesystem. It is never
    interpreted, only handed back on the next write, which is what makes a
    compare-and-swap a compare-and-swap rather than a hopeful overwrite.
    """

    data: bytes
    version: str | None = None


@runtime_checkable
class ArchiveControlStore(Protocol):
    """The library's mutable control records — today, just the head.

    Separate from `ObjectStore` on purpose. That port is immutable
    content-addressed storage where the key *is* the digest and overwriting is
    meaningless. A head is the opposite: one well-known key whose whole job is
    to change, exactly once per publication, with the previous version as the
    precondition. Putting both behind one interface would mean a store that
    offers overwrite — and the first thing to overwrite would be a tombstone.
    """

    async def read(self, key: str) -> VersionedPayload | None:
        """The current bytes and version, or ``None`` when absent.

        ``None`` means genuinely absent. A denial, a timeout or an unreachable
        endpoint raises: a publisher that read "no head" from a permissions
        error would conclude the library is empty and create one.
        """
        ...

    async def create(self, key: str, data: bytes) -> str | None:
        """Write only if nothing is there, returning the new version.

        Raises `CONFLICT` when it already exists. This is how two simultaneous
        first publications resolve to one winner instead of one silently
        overwriting the other.
        """
        ...

    async def compare_and_swap(self, key: str, data: bytes, *, expected_version: str) -> str | None:
        """Replace only if the current version matches, returning the new one.

        Raises `CONFLICT` when it does not. The caller must **not** respond by
        re-reading and retrying with whatever it finds: a failed swap can mean
        a handover fenced this writer, and adopting the epoch it just observed
        is how a fenced publisher takes the library back
        (`domain/publication.py`).
        """
        ...


@runtime_checkable
class ArchiveDeletionStore(Protocol):
    """Deleting archive keys: a separate authority from publishing.

    Its own port so that holding an `ArchiveControlStore` does not imply being
    able to delete. Only a reviewed retention plan calls it (ADR-0028).
    """

    async def delete(self, key: str) -> bool:
        """Remove a key; ``False`` when it was already absent."""
        ...


@runtime_checkable
class DatabaseSnapshot(Protocol):
    """Taking and replaying a portable logical dump of the control plane.

    A port rather than a service call because it shells out: ``pg_dump`` and
    ``pg_restore`` are external programs, which makes them an adapter concern,
    and the backup service has to be drivable by a test on a machine with no
    PostgreSQL on it.

    Logical, not physical. A ``PGDATA`` copy is faster and only restores inside a
    narrow platform and version envelope; this format exists to survive a move to
    a VM nobody has provisioned yet, so the portable method is the contract and
    the fast one is an operator's optional shortcut.
    """

    async def fingerprint(self) -> DatabaseFingerprint:
        """Server version, installed extensions, and the stamped revision."""
        ...

    async def dump(self, destination: Path) -> DumpResult:
        """Write a restorable logical dump, and digest what was written.

        A dump truncated by a full disk is a plausible-looking file. The digest
        recorded at write time is the only thing that distinguishes it from a
        good one.
        """
        ...

    async def restore(self, source: Path, *, clean: bool = False) -> None:
        """Replay a dump into the configured database.

        ``clean`` drops existing objects first, and defaults off: the
        predecessor's script removed the destination's files before it had
        established that the restore would work, so an operator who pointed it at
        the wrong volume had already lost the contents by the time they found
        out.
        """
        ...


@dataclass(frozen=True, slots=True)
class ObjectReferenceSource:
    """One column that points at ``objects.id``.

    ``required`` is the column's nullability: a null ``assets.object_id`` is not
    a thing that can exist, so a dangling one there is a broken asset, while a
    null ``parse_runs.log_object_id`` is an ordinary absent log. Reporting both
    the same way would bury the first in the second.
    """

    table: str
    column: str
    required: bool = False

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True, slots=True)
class ObjectReference:
    """One row's pointer at an object, with the row that holds it."""

    source: ObjectReferenceSource
    row_id: UUID
    object_id: UUID


@runtime_checkable
class SupportsInventory(Protocol):
    """A store that can enumerate what it holds.

    Optional on purpose. A local store keeps an index and can answer cheaply; a
    bucket answers with LIST, which a deliberately narrow reader credential may
    not hold at all (audit F15). A migration that assumed every store could
    enumerate itself would refuse to run under exactly the credentials a careful
    deployment issues.
    """

    def inventory(self) -> AsyncIterator[StoredObject]: ...


@runtime_checkable
class ObjectRegistryRepository(Protocol):
    """The database's record of what the object store holds.

    Separate from ``ObjectStore`` on purpose: the store writes bytes, this writes
    a row, and the row belongs to the caller's transaction beside the asset or
    rendition that points at it. An adapter that registered on the caller's
    behalf would have to open its own transaction, and a dependent row would then
    commit against a registration that could still roll back.
    """

    async def register(self, record: ObjectRecord) -> None:
        """Insert if absent. Never an update, so a tombstone cannot be cleared
        by storing the same bytes again."""
        ...

    async def get(self, object_id: UUID) -> ObjectRecord | None: ...

    async def get_by_digest(
        self, workspace_id: UUID | None, sha256: str
    ) -> ObjectRecord | None: ...

    async def tombstone(self, object_id: UUID, *, reason: str) -> None: ...

    async def list_objects(
        self,
        workspace_id: UUID,
        *,
        limit: int = 500,
        after_id: UUID | None = None,
        object_class: str | None = None,
    ) -> list[ObjectRecord]:
        """One keyset page of the registry, for inventory and restore checks."""
        ...

    async def list_all_objects(
        self, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[ObjectRecord]:
        """One keyset page of every object, across every workspace.

        Migration is an installation-wide operation: a sweep scoped to one
        workspace would leave another workspace's evidence behind, and shared
        objects — the ones with no workspace at all — behind every time.
        """
        ...

    async def count_objects(self, workspace_id: UUID) -> int: ...

    def reference_sources(self) -> tuple[ObjectReferenceSource, ...]:
        """Every column in the schema that points at ``objects.id``.

        Read from the schema rather than from a hand-written list, so a column
        added next year is checked by every sweep that walks these without
        anybody remembering to add it here.
        """
        ...

    async def list_references(
        self,
        workspace_id: UUID,
        source: ObjectReferenceSource,
        *,
        limit: int = 500,
        after_id: UUID | None = None,
    ) -> list[ObjectReference]:
        """One keyset page of non-null references from ``source``."""
        ...


@runtime_checkable
class UserRepository(Protocol):
    async def get(self, user_id: UUID) -> User | None: ...
    async def get_by_email(self, email_normalized: str) -> User | None: ...
    async def add(self, user: User) -> None: ...
    async def update(self, user: User) -> None: ...
    async def list_all(self, *, limit: int = 100, cursor: str | None = None) -> list[User]: ...


@runtime_checkable
class WorkspaceRepository(Protocol):
    async def get(self, workspace_id: UUID) -> Workspace | None: ...
    async def get_by_slug(self, slug: str) -> Workspace | None: ...
    async def add(self, workspace: Workspace) -> None: ...
    async def list_for_user(self, user_id: UUID) -> list[Workspace]: ...

    async def list_workspaces(
        self, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[Workspace]:
        """One keyset page of every workspace in the installation.

        Distinct from ``list_for_user``, which answers a membership question. An
        installation-wide operation — a backup, a recovery inventory — needs the
        workspaces nobody is a member of too: one whose last member left still
        holds evidence, and a backup that skipped it would restore an
        installation missing a corpus nobody can name.
        """
        ...

    async def get_membership(self, workspace_id: UUID, user_id: UUID) -> Membership | None: ...
    async def list_memberships(self, workspace_id: UUID) -> list[Membership]: ...
    async def add_membership(self, membership: Membership) -> None: ...
    async def update_membership(self, membership: Membership) -> None: ...
    async def list_groups(
        self, workspace_id: UUID, *, user_id: UUID | None = None
    ) -> list[Group]: ...
    async def add_group(self, group: Group) -> None: ...
    async def update_group(self, group: Group) -> None: ...
    async def add_invitation(self, invitation: Invitation) -> None: ...
    async def get_invitation_by_digest(self, token_digest: str) -> Invitation | None: ...
    async def update_invitation(self, invitation: Invitation) -> None: ...
    async def list_invitations(self, workspace_id: UUID) -> list[Invitation]: ...


@runtime_checkable
class PolicyRepository(Protocol):
    async def list_grants(
        self, workspace_id: UUID, *, subject_ids: Sequence[UUID]
    ) -> list[GrantSpec]: ...
    async def add_grant(self, grant: GrantSpec) -> None: ...
    async def remove_grant(self, workspace_id: UUID, grant_id: UUID) -> None: ...
    async def add_rights_decision(self, decision: RightsDecision) -> None: ...
    async def get_rights_decision(
        self, workspace_id: UUID, decision_id: UUID
    ) -> RightsDecision | None: ...
    async def find_rights_decision(
        self, workspace_id: UUID, *, subject_id: UUID
    ) -> RightsDecision | None: ...
    async def list_rights_decisions(
        self, workspace_id: UUID, *, expiring_before: datetime | None = None
    ) -> list[RightsDecision]: ...


@runtime_checkable
class ConfigRepository(Protocol):
    async def add_scope_version(self, scope: ScopeVersion) -> None: ...
    async def get_scope_version(
        self, workspace_id: UUID, scope_version_id: UUID
    ) -> ScopeVersion | None: ...
    async def latest_scope_version(
        self, workspace_id: UUID, scope_id: UUID
    ) -> ScopeVersion | None: ...
    async def list_scope_versions(self, workspace_id: UUID) -> list[ScopeVersion]: ...
    async def delete_scope_version(self, workspace_id: UUID, scope_version_id: UUID) -> None: ...
    async def add_config_version(self, config: ConfigVersion) -> None: ...
    async def get_config_version(self, config_version_id: UUID) -> ConfigVersion | None: ...
    async def find_config_version(
        self, workspace_id: UUID | None, *, kind: str, logical_id: str, semantic_version: str
    ) -> ConfigVersion | None: ...
    async def list_config_versions(
        self, workspace_id: UUID | None, *, kind: str | None = None
    ) -> list[ConfigVersion]: ...


@runtime_checkable
class ObservationRepository(Protocol):
    async def add_run(self, run: DiscoveryRun) -> None: ...
    async def update_run(self, run: DiscoveryRun) -> None: ...
    async def get_run(self, workspace_id: UUID, run_id: UUID) -> DiscoveryRun | None: ...
    async def list_runs(self, workspace_id: UUID, *, limit: int = 50) -> list[DiscoveryRun]: ...
    async def add_request(self, request: SourceRequest) -> None: ...
    async def update_request(self, request: SourceRequest) -> None: ...
    async def find_request_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> SourceRequest | None: ...
    async def list_requests(self, workspace_id: UUID, run_id: UUID) -> list[SourceRequest]: ...
    async def add_observation(self, observation: SourceObservation) -> None: ...
    async def list_observations(
        self, workspace_id: UUID, *, request_id: UUID | None = None
    ) -> list[SourceObservation]: ...
    async def add_assertions(self, assertions: Sequence[SourceAssertion]) -> None: ...
    async def list_assertions(
        self,
        workspace_id: UUID,
        *,
        observation_id: UUID | None = None,
        subject_hint: str | None = None,
        work_id: UUID | None = None,
    ) -> list[SourceAssertion]: ...
    async def update_assertion(self, assertion: SourceAssertion) -> None: ...
    async def get_cursor(self, workspace_id: UUID, *, lineage_key: str) -> str | None: ...
    async def set_cursor(
        self, workspace_id: UUID, *, lineage_key: str, position: str | None, exhausted: bool
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkVisibilityRow:
    """One work, with everything the policy engine needs to judge it.

    Computed in a single query per page instead of two queries per work. Deciding
    visibility for a 13,000-work corpus was 26,000 round trips; it is now one page
    of rows, which is the difference between a filter that runs before scoring and
    a filter someone is tempted to move afterwards.
    """

    work_id: UUID
    is_active: bool
    relevance: str
    restricted_to_groups: tuple[UUID, ...] = ()
    sensitivity: str = "internal"
    #: True when at least one asset carries a rights decision. A work with none is
    #: not "unrestricted"; it is undecided, and content actions default to deny.
    has_rights_decision: bool = False
    #: The status of every rights decision on the work's assets, and the earliest
    #: review date among them. A revoked or lapsed decision withholds the work
    #: however its groups read — which retrieval did not check before, so a
    #: revocation stopped nothing until a successor release (ADR-0029).
    rights_states: tuple[str, ...] = ()
    earliest_review_due: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> UUID:
        """The keyset cursor, so ``iter_pages`` can page these like any row."""
        return self.work_id


@runtime_checkable
class WorkRepository(Protocol):
    async def add(self, work: Work) -> None: ...
    async def update(self, work: Work) -> None: ...
    async def get(self, workspace_id: UUID, work_id: UUID) -> Work | None: ...
    async def list_works(
        self,
        workspace_id: UUID,
        *,
        relevance: str | None = None,
        limit: int = 100,
        #: Keyset cursor. ``limit`` is a page size, never a corpus-completeness
        #: control: a lifecycle path that must see every work pages with this until
        #: a short page comes back. See ``application/pagination.py``.
        after_id: UUID | None = None,
    ) -> list[Work]: ...
    async def list_work_visibility(
        self,
        workspace_id: UUID,
        *,
        relevance: str | None = None,
        limit: int = 500,
        after_id: UUID | None = None,
    ) -> list[WorkVisibilityRow]: ...
    async def add_identifier(self, identifier: WorkIdentifier) -> None: ...
    async def update_identifier(self, identifier: WorkIdentifier) -> None: ...
    async def list_identifiers(self, workspace_id: UUID, work_id: UUID) -> list[WorkIdentifier]: ...
    async def find_by_identifier(
        self, workspace_id: UUID, *, scheme: str, value_normalized: str
    ) -> Work | None: ...
    async def add_relation(self, relation: WorkRelation) -> None: ...
    async def list_relations(self, workspace_id: UUID, work_id: UUID) -> list[WorkRelation]: ...
    async def add_decision(self, decision: IdentityDecision) -> None: ...
    async def list_decisions(
        self, workspace_id: UUID, *, work_id: UUID | None = None
    ) -> list[IdentityDecision]: ...


@runtime_checkable
class AssetRepository(Protocol):
    async def add_manifestation(self, manifestation: Manifestation) -> None: ...
    async def get_manifestation(
        self, workspace_id: UUID, manifestation_id: UUID
    ) -> Manifestation | None: ...
    async def list_manifestations(
        self, workspace_id: UUID, work_id: UUID
    ) -> list[Manifestation]: ...
    async def add_plan(self, plan: AcquisitionPlan) -> None: ...
    async def update_plan(self, plan: AcquisitionPlan) -> None: ...
    async def get_plan(self, workspace_id: UUID, plan_id: UUID) -> AcquisitionPlan | None: ...
    async def list_plans(
        self, workspace_id: UUID, *, status: str | None = None
    ) -> list[AcquisitionPlan]: ...
    async def add_attempt(self, attempt: AcquisitionAttempt) -> None: ...
    async def list_attempts(
        self, workspace_id: UUID, *, plan_id: UUID | None = None
    ) -> list[AcquisitionAttempt]: ...
    async def add_asset(self, asset: Asset) -> None: ...
    async def update_asset(self, asset: Asset) -> None: ...
    async def get_asset(self, workspace_id: UUID, asset_id: UUID) -> Asset | None: ...
    async def find_asset_by_digest(
        self,
        workspace_id: UUID,
        sha256: str,
        *,
        #: Byte storage deduplicates; *provenance* does not. The same PDF acquired
        #: for two works is one object and two assets, so a caller that means "is
        #: this already registered for this work" must say which work.
        work_id: UUID | None = None,
        manifestation_id: UUID | None = None,
    ) -> Asset | None: ...
    async def list_assets(
        self, workspace_id: UUID, *, work_id: UUID | None = None
    ) -> list[Asset]: ...


@runtime_checkable
class RenditionRepository(Protocol):
    async def add_parse_run(self, run: ParseRun) -> None: ...
    async def update_parse_run(self, run: ParseRun) -> None: ...
    async def get_parse_run(self, workspace_id: UUID, run_id: UUID) -> ParseRun | None: ...
    async def find_parse_run_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> ParseRun | None: ...
    async def add_rendition(self, rendition: Rendition) -> None: ...
    async def update_rendition(self, rendition: Rendition) -> None: ...
    async def get_rendition(self, workspace_id: UUID, rendition_id: UUID) -> Rendition | None: ...
    async def list_renditions(
        self, workspace_id: UUID, *, asset_id: UUID | None = None, work_id: UUID | None = None
    ) -> list[Rendition]: ...
    async def add_spans(self, spans: Sequence[EvidenceSpan]) -> None: ...
    async def get_span(self, workspace_id: UUID, span_id: UUID) -> EvidenceSpan | None: ...
    async def list_spans(self, workspace_id: UUID, rendition_id: UUID) -> list[EvidenceSpan]: ...


@runtime_checkable
class ClaimRepository(Protocol):
    async def add_entity(self, entity: Entity) -> None: ...
    async def find_entity(
        self, workspace_id: UUID, *, entity_type: str, normalised_value: str
    ) -> Entity | None: ...
    async def get_entity(self, workspace_id: UUID, entity_id: UUID) -> Entity | None:
        """By id, for the archive: a claim names its subject and unit by id."""
        ...

    async def add_experiment(self, experiment: Experiment) -> None: ...
    async def update_experiment(self, experiment: Experiment) -> None: ...
    async def list_experiments(self, workspace_id: UUID, work_id: UUID) -> list[Experiment]: ...
    async def add_claim(self, claim: Claim) -> None: ...
    async def update_claim(self, claim: Claim) -> None: ...
    async def get_claim(self, workspace_id: UUID, claim_id: UUID) -> Claim | None: ...
    async def list_claims(
        self,
        workspace_id: UUID,
        *,
        work_id: UUID | None = None,
        review_status: str | None = None,
        limit: int = 200,
        after_id: UUID | None = None,
    ) -> list[Claim]: ...
    async def add_revision(self, revision: ClaimRevision) -> None: ...
    async def update_revision(self, revision: ClaimRevision) -> None: ...
    async def get_revision(self, workspace_id: UUID, revision_id: UUID) -> ClaimRevision | None: ...
    async def list_revisions(self, workspace_id: UUID, claim_id: UUID) -> list[ClaimRevision]: ...
    async def find_revision_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> ClaimRevision | None: ...
    async def add_review(self, review: Review) -> None: ...
    async def list_reviews(
        self, workspace_id: UUID, *, subject_id: UUID | None = None
    ) -> list[Review]: ...


@runtime_checkable
class ReleaseRepository(Protocol):
    async def add(self, release: CorpusRelease) -> None: ...
    async def update(self, release: CorpusRelease) -> None: ...
    async def get(self, workspace_id: UUID, release_id: UUID) -> CorpusRelease | None: ...
    async def list_releases(
        self, workspace_id: UUID, *, limit: int = 50
    ) -> list[CorpusRelease]: ...

    async def list_all_releases(
        self, workspace_id: UUID, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[CorpusRelease]:
        """One keyset page, ordered by id, for paths that must see every release.

        Separate from ``list_releases`` because the two want different orders and
        conflating them is how a lifecycle sweep ends up reading the newest fifty
        rows and reporting on the corpus.
        """
        ...

    async def add_items(self, items: Sequence[ReleaseItem]) -> None: ...
    async def list_items(self, workspace_id: UUID, release_id: UUID) -> list[ReleaseItem]: ...
    async def get_alias(self, workspace_id: UUID, alias: str) -> SearchAlias | None: ...
    async def upsert_alias(self, alias: SearchAlias) -> None: ...
    async def list_aliases(self, workspace_id: UUID) -> list[SearchAlias]: ...


@runtime_checkable
class SearchIndex(Protocol):
    """Release-scoped serving projections."""

    async def write_chunks(
        self, workspace_id: UUID, release_id: UUID, rows: Sequence[dict[str, Any]]
    ) -> int: ...
    async def copy_chunks(
        self,
        workspace_id: UUID,
        *,
        from_release_id: UUID,
        to_release_id: UUID,
        work_ids: Sequence[UUID],
    ) -> int: ...
    async def write_embeddings(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        profile_id: str,
        rows: Sequence[dict[str, Any]],
    ) -> int: ...
    async def copy_embeddings(
        self,
        workspace_id: UUID,
        *,
        profile_id: str,
        from_release_id: UUID,
        to_release_id: UUID,
        work_ids: Sequence[UUID],
    ) -> int: ...
    async def write_claim_projection(
        self, workspace_id: UUID, release_id: UUID, rows: Sequence[dict[str, Any]]
    ) -> int: ...
    async def lexical_search(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        query: str,
        top_k: int,
        work_ids: Sequence[UUID] | None = None,
    ) -> list[dict[str, Any]]: ...
    async def vector_search(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        profile_id: str,
        vector: Sequence[float],
        top_k: int,
        work_ids: Sequence[UUID] | None = None,
    ) -> list[dict[str, Any]]: ...
    async def claim_search(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        predicate: str | None = None,
        subject: str | None = None,
        top_k: int = 20,
        #: The policy-filtered works this caller may see. ``None`` means "no filter"
        #: and is for stewardship paths that have already decided; retrieval always
        #: passes a list, so restricted claims cannot occupy the top-k window and
        #: cannot influence what a caller sees or how much of it there is.
        work_ids: Sequence[UUID] | None = None,
    ) -> list[dict[str, Any]]: ...
    async def count_chunks(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        work_ids: Sequence[UUID] | None = None,
    ) -> int: ...
    async def list_chunks(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        limit: int = 500,
        after_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """One keyset page of a release's chunks, ordered by chunk key.

        Reading a projection back is not something serving needs — which is why
        it was not here until the archive needed it. Publishing a release means
        carrying its chunks to an installation that cannot run the chunker, so
        the projection has to be enumerable, and keyset-paged because a corpus
        is larger than a page.
        """
        ...

    async def list_embeddings(
        self,
        workspace_id: UUID,
        release_id: UUID,
        *,
        profile_id: str,
        limit: int = 500,
        after_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """One keyset page of a release's vectors under one profile."""
        ...

    async def delete_release_projections(self, workspace_id: UUID, release_id: UUID) -> int: ...


@runtime_checkable
class TaskRepository(Protocol):
    async def add(self, task: Task) -> None: ...
    async def update(self, task: Task) -> None: ...
    async def get(self, workspace_id: UUID, task_id: UUID) -> Task | None: ...
    async def find_by_fingerprint(self, workspace_id: UUID, fingerprint: str) -> Task | None: ...
    async def claim_next(
        self,
        *,
        owner: str,
        worker_class: str,
        now: datetime,
        stages: Sequence[StageName] | None = None,
        lease_seconds: int = 300,
        stage_concurrency: dict[StageName, int] | None = None,
    ) -> Task | None: ...
    async def list_tasks(
        self,
        workspace_id: UUID,
        *,
        state: str | None = None,
        stage: str | None = None,
        limit: int = 100,
    ) -> list[Task]: ...
    async def count_by_state(self, workspace_id: UUID) -> dict[str, int]: ...
    async def release_dependencies(
        self, workspace_id: UUID, completed_task_id: UUID
    ) -> list[Task]: ...
    async def get_rate_budget(self, key: str, *, for_update: bool = False) -> RateBudget | None: ...
    async def upsert_rate_budget(self, budget: RateBudget) -> None: ...


@runtime_checkable
class AuditRepository(Protocol):
    async def append(self, event: AuditEventRecord) -> None: ...
    async def latest_hash(self, workspace_id: UUID | None) -> str | None: ...
    async def list_events(
        self,
        workspace_id: UUID | None,
        *,
        subject_id: UUID | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[AuditEventRecord]: ...
    async def add_outbox(self, event: OutboxEvent) -> None: ...
    async def list_unpublished_outbox(self, *, limit: int = 100) -> list[OutboxEvent]: ...
    async def mark_outbox_published(self, event_id: UUID, *, at: datetime) -> None: ...


@runtime_checkable
class ChatRepository(Protocol):
    async def add_session(self, session: ChatSession) -> None: ...
    async def update_session(self, session: ChatSession) -> None: ...
    async def get_session(self, workspace_id: UUID, session_id: UUID) -> ChatSession | None: ...
    async def list_sessions(
        self, workspace_id: UUID, user_id: UUID, *, limit: int = 50
    ) -> list[ChatSession]: ...
    async def list_workspace_sessions(self, workspace_id: UUID) -> list[ChatSession]:
        """Every member's sessions: what a retraction or a retention plan must see."""
        ...

    async def add_message(self, message: ChatMessage) -> None: ...
    async def list_messages(self, workspace_id: UUID, session_id: UUID) -> list[ChatMessage]: ...
    async def get_message(self, workspace_id: UUID, message_id: UUID) -> ChatMessage | None: ...
    async def add_feedback(self, feedback: Feedback) -> None: ...
    async def list_feedback(self, workspace_id: UUID, *, limit: int = 100) -> list[Feedback]: ...
    async def usage_by_user(
        self, workspace_id: UUID, *, user_ids: Sequence[UUID] | None = None
    ) -> dict[UUID, UsageTotals]:
        """Chat activity per member, deleted sessions included. Absent means none."""
        ...


@runtime_checkable
class EvaluationRepository(Protocol):
    """Evaluation runs and the benchmark versions they measured against.

    Rows are plain dictionaries, as the run record has always been: ids may come
    back as ``UUID`` or ``str`` and times as ``datetime`` or absent, so callers
    normalise on the way out.
    """

    async def add_run(self, run: dict[str, Any]) -> None: ...
    async def update_run(self, run: dict[str, Any]) -> None:
        """Replace the mutable fields of an existing run: status, results, times."""
        ...

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> dict[str, Any] | None: ...
    async def list_runs(
        self,
        workspace_id: UUID,
        *,
        benchmark_id: str | None = None,
        release_id: UUID | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Newest first."""
        ...

    async def add_benchmark(self, benchmark: dict[str, Any]) -> None: ...
    async def get_benchmark(
        self, workspace_id: UUID, benchmark_id: str, version: str
    ) -> dict[str, Any] | None: ...
    async def list_benchmarks(self, workspace_id: UUID) -> list[dict[str, Any]]: ...

    async def add_review(self, review: dict[str, Any]) -> None: ...
    async def update_review(self, review: dict[str, Any]) -> None:
        """Replace a review's grades, status and submission time."""
        ...

    async def get_review(self, workspace_id: UUID, review_id: UUID) -> dict[str, Any] | None: ...
    async def list_reviews(
        self,
        workspace_id: UUID,
        *,
        run_id: UUID | None = None,
        assigned_to: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Oldest first, so a reviewer's queue reads in the order it was assigned."""
        ...

    async def list_release_gates(self, workspace_id: UUID) -> list[dict[str, Any]]: ...
    async def set_release_gate(self, gate: dict[str, Any]) -> None:
        """Require a benchmark version, replacing any earlier version of it."""
        ...

    async def clear_release_gate(self, workspace_id: UUID, benchmark_id: str) -> None: ...


@runtime_checkable
class StageResultRepository(Protocol):
    """Completed expensive work, by the fingerprint of its input.

    The index that stops a model being paid twice for one paper. It records an
    outcome whether or not the stage produced rows, because the case that costs
    the most is the one that produces none (audit F10).
    """

    async def record(self, result: StageResultEnvelope, *, workspace_id: UUID) -> None:
        """Write the outcome, replacing any earlier one for this fingerprint.

        Replacing rather than refusing: a retryable failure recorded last week
        must be overwritten by this week's success, or the retry is pointless.
        """
        ...

    async def find(self, workspace_id: UUID, reuse_fingerprint: str) -> StageResultEnvelope | None:
        """The recorded outcome for this input, if there is one."""
        ...


@runtime_checkable
class ArchiveImportRepository(Protocol):
    """A reader's record of what it imported, and the metadata it keeps."""

    async def add(self, record: ImportRecord) -> None: ...

    async def latest(self, library_id: UUID) -> ImportRecord | None:
        """The newest active import: the reader's durable position."""
        ...

    async def put_work_metadata(
        self,
        workspace_id: UUID,
        work_id: UUID,
        *,
        library_id: UUID,
        title: str | None,
        publication_year: int | None,
        container_title: str | None,
    ) -> None: ...

    async def get_work_metadata(
        self, workspace_id: UUID, work_id: UUID
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class PublicationRepository(Protocol):
    """The archive publication outbox.

    Separate from the archive itself: this is the producer's record of what it
    *intended*, and the head is what actually happened. When they disagree the
    head wins, always — a local row claiming a commit the head does not carry
    did not commit, however confident it looks.
    """

    async def add(self, record: PublicationRecord) -> None: ...

    async def update(self, record: PublicationRecord) -> None: ...

    async def get(self, workspace_id: UUID, record_id: UUID) -> PublicationRecord | None: ...

    async def find_by_snapshot(
        self, library_id: UUID, snapshot_id: UUID
    ) -> PublicationRecord | None:
        """The attempt for this snapshot, if one was ever started.

        Keyed by library and snapshot rather than by workspace, because that is
        the pair the database holds unique: publishing one snapshot twice is
        the duplicate the constraint exists to refuse.
        """
        ...

    async def list_open(self, library_id: UUID) -> list[PublicationRecord]:
        """Attempts that have not reached a terminal state, oldest first.

        What a recovering publisher reads before it does anything else.
        """
        ...

    async def list_for_library(self, library_id: UUID) -> list[PublicationRecord]:
        """Every attempt for a library, oldest first, whatever its state.

        How a retention plan enumerates snapshots without listing the bucket:
        the producer recorded every one it ever uploaded.
        """
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Transaction boundary plus the repository set.

    Application services never touch a session or a connection. They open a unit
    of work, use repositories, and commit. Side effects that must happen with the
    write go through the outbox in the same transaction.
    """

    users: UserRepository
    workspaces: WorkspaceRepository
    objects: ObjectRegistryRepository
    policies: PolicyRepository
    configs: ConfigRepository
    observations: ObservationRepository
    works: WorkRepository
    assets: AssetRepository
    renditions: RenditionRepository
    claims: ClaimRepository
    releases: ReleaseRepository
    publications: PublicationRepository
    imports: ArchiveImportRepository
    stage_results: StageResultRepository
    tasks: TaskRepository
    audit: AuditRepository
    chat: ChatRepository
    evaluations: EvaluationRepository

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


@runtime_checkable
class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


def require_same_workspace(workspace_id: UUID, rows: Iterable[Any]) -> None:
    """Guard used by repository implementations and release validation."""
    for row in rows:
        row_workspace = getattr(row, "workspace_id", None)
        if row_workspace is not None and row_workspace != workspace_id:
            raise AssertionError(
                f"row {getattr(row, 'id', row)!r} belongs to workspace {row_workspace}, "
                f"expected {workspace_id}"
            )
