"""Repository and infrastructure ports.

Every repository method takes ``workspace_id`` as a required argument, never an
optional filter. That signature is the irreversible half of tenancy-readiness:
adding the column later is a migration, but retrofitting the *call sites* is a
rewrite (scale §3.3).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from lab_assistant.contracts.events import AuditEventRecord, OutboxEvent
from lab_assistant.contracts.policy import GrantSpec
from lab_assistant.contracts.runtime import Clock, IdFactory
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.assets import (
    AcquisitionAttempt,
    AcquisitionPlan,
    Asset,
    Manifestation,
    RightsDecision,
)
from lab_assistant.domain.chat import ChatMessage, ChatSession, Feedback
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
    "AssetRepository",
    "AuditRepository",
    "ChatRepository",
    "ClaimRepository",
    "Clock",
    "ConfigRepository",
    "EvaluationRepository",
    "IdFactory",
    "ObjectRecord",
    "ObjectReference",
    "ObjectReferenceSource",
    "ObjectRegistryRepository",
    "ObjectStore",
    "ObservationRepository",
    "PolicyRepository",
    "ReleaseRepository",
    "RenditionRepository",
    "SearchIndex",
    "StoredObject",
    "TaskRepository",
    "UnitOfWork",
    "UserRepository",
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

    async def get(self, object_id: UUID) -> bytes: ...

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
    async def add_message(self, message: ChatMessage) -> None: ...
    async def list_messages(self, workspace_id: UUID, session_id: UUID) -> list[ChatMessage]: ...
    async def get_message(self, workspace_id: UUID, message_id: UUID) -> ChatMessage | None: ...
    async def add_feedback(self, feedback: Feedback) -> None: ...
    async def list_feedback(self, workspace_id: UUID, *, limit: int = 100) -> list[Feedback]: ...


@runtime_checkable
class EvaluationRepository(Protocol):
    async def add_run(self, run: dict[str, Any]) -> None: ...
    async def get_run(self, workspace_id: UUID, run_id: UUID) -> dict[str, Any] | None: ...
    async def list_runs(
        self, workspace_id: UUID, *, benchmark_id: str | None = None
    ) -> list[dict[str, Any]]: ...


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
