"""In-memory repositories and unit of work.

Not a toy: this is the adapter the deterministic test suite runs against, and it
enforces the same rules the PostgreSQL adapter must. In particular every method
requires ``workspace_id`` and filters on it, and ``_MemoryStore`` asserts that a
row being written carries the workspace it is being written under. A repository
that silently ignored the workspace argument would make the isolation tests
vacuous, so the guard lives here rather than only in the SQL layer.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, TypeVar
from uuid import UUID

from lab_assistant.application.ports import (
    ObjectReference,
    ObjectReferenceSource,
    WorkVisibilityRow,
)
from lab_assistant.contracts.archive import StageResultEnvelope
from lab_assistant.contracts.archive_import import ImportRecord, ImportState
from lab_assistant.contracts.errors import ConcurrencyConflict, ErrorClass, LabAssistantError
from lab_assistant.contracts.events import AuditEventRecord, OutboxEvent
from lab_assistant.contracts.policy import GrantSpec
from lab_assistant.contracts.publication import PublicationRecord
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.assets import (
    AcquisitionAttempt,
    AcquisitionPlan,
    Asset,
    Manifestation,
    RightsDecision,
)
from lab_assistant.domain.chat import ChatMessage, ChatSession, Feedback, MessageRole, UsageTotals
from lab_assistant.domain.claims import Claim, ClaimRevision, Entity, Experiment, Review
from lab_assistant.domain.identity import IdentityDecision, Work, WorkIdentifier, WorkRelation
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
from lab_assistant.domain.states import TaskState
from lab_assistant.domain.tasks import RateBudget, Task
from lab_assistant.domain.workspace import Group, Invitation, Membership, User, Workspace

__all__ = ["MemoryDatabase", "MemoryUnitOfWork", "memory_uow_factory"]

T = TypeVar("T")


def _check_workspace(workspace_id: UUID, row: Any) -> None:
    row_workspace = getattr(row, "workspace_id", None)
    if row_workspace is not None and row_workspace != workspace_id:
        raise AssertionError(
            f"row {getattr(row, 'id', row)!r} belongs to workspace {row_workspace}, "
            f"expected {workspace_id}"
        )


#: Sort key for a row written before `created_at` was set.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class MemoryDatabase:
    """The shared store behind every unit of work in one test or process."""

    users: dict[UUID, User] = field(default_factory=dict)
    workspaces: dict[UUID, Workspace] = field(default_factory=dict)
    memberships: dict[tuple[UUID, UUID], Membership] = field(default_factory=dict)
    groups: dict[UUID, Group] = field(default_factory=dict)
    invitations: dict[UUID, Invitation] = field(default_factory=dict)
    grants: dict[UUID, GrantSpec] = field(default_factory=dict)
    rights_decisions: dict[UUID, RightsDecision] = field(default_factory=dict)
    objects: dict[UUID, ObjectRecord] = field(default_factory=dict)
    scope_versions: dict[UUID, ScopeVersion] = field(default_factory=dict)
    config_versions: dict[UUID, ConfigVersion] = field(default_factory=dict)
    discovery_runs: dict[UUID, DiscoveryRun] = field(default_factory=dict)
    source_requests: dict[UUID, SourceRequest] = field(default_factory=dict)
    observations: dict[UUID, SourceObservation] = field(default_factory=dict)
    assertions: dict[UUID, SourceAssertion] = field(default_factory=dict)
    cursors: dict[tuple[UUID, str], tuple[str | None, bool]] = field(default_factory=dict)
    works: dict[UUID, Work] = field(default_factory=dict)
    identifiers: dict[UUID, WorkIdentifier] = field(default_factory=dict)
    relations: dict[UUID, WorkRelation] = field(default_factory=dict)
    identity_decisions: dict[UUID, IdentityDecision] = field(default_factory=dict)
    manifestations: dict[UUID, Manifestation] = field(default_factory=dict)
    plans: dict[UUID, AcquisitionPlan] = field(default_factory=dict)
    attempts: dict[UUID, AcquisitionAttempt] = field(default_factory=dict)
    assets: dict[UUID, Asset] = field(default_factory=dict)
    parse_runs: dict[UUID, ParseRun] = field(default_factory=dict)
    renditions: dict[UUID, Rendition] = field(default_factory=dict)
    spans: dict[UUID, EvidenceSpan] = field(default_factory=dict)
    entities: dict[UUID, Entity] = field(default_factory=dict)
    experiments: dict[UUID, Experiment] = field(default_factory=dict)
    claims: dict[UUID, Claim] = field(default_factory=dict)
    revisions: dict[UUID, ClaimRevision] = field(default_factory=dict)
    reviews: dict[UUID, Review] = field(default_factory=dict)
    releases: dict[UUID, CorpusRelease] = field(default_factory=dict)
    publications: dict[UUID, PublicationRecord] = field(default_factory=dict)
    stage_results: dict[tuple[UUID, str], StageResultEnvelope] = field(default_factory=dict)
    imports: dict[UUID, ImportRecord] = field(default_factory=dict)
    work_metadata: dict[tuple[UUID, UUID], dict[str, Any]] = field(default_factory=dict)
    release_items: dict[UUID, ReleaseItem] = field(default_factory=dict)
    aliases: dict[tuple[UUID, str], SearchAlias] = field(default_factory=dict)
    tasks: dict[UUID, Task] = field(default_factory=dict)
    rate_budgets: dict[str, RateBudget] = field(default_factory=dict)
    audit_events: list[AuditEventRecord] = field(default_factory=list)
    outbox: dict[UUID, OutboxEvent] = field(default_factory=dict)
    chat_sessions: dict[UUID, ChatSession] = field(default_factory=dict)
    chat_messages: dict[UUID, ChatMessage] = field(default_factory=dict)
    feedback: dict[UUID, Feedback] = field(default_factory=dict)
    evaluation_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    benchmarks: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    evaluation_reviews: dict[str, dict[str, Any]] = field(default_factory=dict)
    release_gates: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: Per-stage leases, tracked by task id rather than as a counter. Rows here
    #: are mutated in place, so "what state was this task in before?" is not a
    #: question this store can answer — a counter would leak on every update.
    leased_task_ids: dict[StageName, set[UUID]] = field(default_factory=lambda: defaultdict(set))


class _Users:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def get(self, user_id: UUID) -> User | None:
        return self._db.users.get(user_id)

    async def get_by_email(self, email_normalized: str) -> User | None:
        for user in self._db.users.values():
            if user.email_normalized == email_normalized:
                return user
        return None

    async def add(self, user: User) -> None:
        self._db.users[user.id] = user

    async def update(self, user: User) -> None:
        self._db.users[user.id] = user

    async def list_all(self, *, limit: int = 100, cursor: str | None = None) -> list[User]:
        users = sorted(self._db.users.values(), key=lambda u: u.email_normalized)
        if cursor:
            users = [u for u in users if u.email_normalized > cursor]
        return users[:limit]


class _Workspaces:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def get(self, workspace_id: UUID) -> Workspace | None:
        return self._db.workspaces.get(workspace_id)

    async def get_by_slug(self, slug: str) -> Workspace | None:
        for workspace in self._db.workspaces.values():
            if workspace.slug == slug:
                return workspace
        return None

    async def add(self, workspace: Workspace) -> None:
        self._db.workspaces[workspace.id] = workspace

    async def list_for_user(self, user_id: UUID) -> list[Workspace]:
        ids = {ws_id for (ws_id, uid) in self._db.memberships if uid == user_id}
        return [self._db.workspaces[i] for i in sorted(ids, key=str) if i in self._db.workspaces]

    async def list_workspaces(
        self, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[Workspace]:
        ordered = sorted(self._db.workspaces.values(), key=lambda ws: str(ws.id))
        if after_id is not None:
            ordered = [ws for ws in ordered if str(ws.id) > str(after_id)]
        return ordered[:limit]

    async def get_membership(self, workspace_id: UUID, user_id: UUID) -> Membership | None:
        return self._db.memberships.get((workspace_id, user_id))

    async def list_memberships(self, workspace_id: UUID) -> list[Membership]:
        return [m for (ws, _), m in self._db.memberships.items() if ws == workspace_id]

    async def add_membership(self, membership: Membership) -> None:
        self._db.memberships[(membership.workspace_id, membership.user_id)] = membership

    async def update_membership(self, membership: Membership) -> None:
        self._db.memberships[(membership.workspace_id, membership.user_id)] = membership

    async def list_groups(self, workspace_id: UUID, *, user_id: UUID | None = None) -> list[Group]:
        groups = [g for g in self._db.groups.values() if g.workspace_id == workspace_id]
        if user_id is not None:
            groups = [g for g in groups if g.contains(user_id)]
        return sorted(groups, key=lambda g: g.slug)

    async def add_group(self, group: Group) -> None:
        self._db.groups[group.id] = group

    async def update_group(self, group: Group) -> None:
        self._db.groups[group.id] = group

    async def add_invitation(self, invitation: Invitation) -> None:
        self._db.invitations[invitation.id] = invitation

    async def get_invitation_by_digest(self, token_digest: str) -> Invitation | None:
        for invitation in self._db.invitations.values():
            if invitation.token_digest == token_digest:
                return invitation
        return None

    async def update_invitation(self, invitation: Invitation) -> None:
        self._db.invitations[invitation.id] = invitation

    async def list_invitations(self, workspace_id: UUID) -> list[Invitation]:
        return [i for i in self._db.invitations.values() if i.workspace_id == workspace_id]


class _Policies:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def list_grants(
        self, workspace_id: UUID, *, subject_ids: Sequence[UUID]
    ) -> list[GrantSpec]:
        subjects = set(subject_ids)
        return [
            grant
            for grant in self._db.grants.values()
            if grant.workspace_id == workspace_id and grant.subject_id in subjects
        ]

    async def add_grant(self, grant: GrantSpec) -> None:
        self._db.grants[grant.id] = grant

    async def remove_grant(self, workspace_id: UUID, grant_id: UUID) -> None:
        grant = self._db.grants.get(grant_id)
        if grant is not None:
            _check_workspace(workspace_id, grant)
            del self._db.grants[grant_id]

    async def add_rights_decision(self, decision: RightsDecision) -> None:
        self._db.rights_decisions[decision.id] = decision

    async def get_rights_decision(
        self, workspace_id: UUID, decision_id: UUID
    ) -> RightsDecision | None:
        decision = self._db.rights_decisions.get(decision_id)
        if decision is None:
            return None
        _check_workspace(workspace_id, decision)
        return decision

    async def find_rights_decision(
        self, workspace_id: UUID, *, subject_id: UUID
    ) -> RightsDecision | None:
        matches = [
            decision
            for decision in self._db.rights_decisions.values()
            if decision.workspace_id == workspace_id and decision.subject_id == subject_id
        ]
        return sorted(matches, key=lambda d: d.decided_at or datetime.min)[-1] if matches else None

    async def list_rights_decisions(
        self, workspace_id: UUID, *, expiring_before: datetime | None = None
    ) -> list[RightsDecision]:
        decisions = [
            d for d in self._db.rights_decisions.values() if d.workspace_id == workspace_id
        ]
        if expiring_before is not None:
            decisions = [
                d
                for d in decisions
                if d.review_due_at is not None and d.review_due_at < expiring_before
            ]
        return decisions


class _Configs:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add_scope_version(self, scope: ScopeVersion) -> None:
        self._db.scope_versions[scope.id] = scope

    async def get_scope_version(
        self, workspace_id: UUID, scope_version_id: UUID
    ) -> ScopeVersion | None:
        scope = self._db.scope_versions.get(scope_version_id)
        if scope is None:
            return None
        _check_workspace(workspace_id, scope)
        return scope

    async def latest_scope_version(self, workspace_id: UUID, scope_id: UUID) -> ScopeVersion | None:
        versions = [
            s
            for s in self._db.scope_versions.values()
            if s.workspace_id == workspace_id and s.scope_id == scope_id
        ]
        return max(versions, key=lambda s: s.version) if versions else None

    async def list_scope_versions(self, workspace_id: UUID) -> list[ScopeVersion]:
        return sorted(
            (s for s in self._db.scope_versions.values() if s.workspace_id == workspace_id),
            key=lambda s: (str(s.scope_id), s.version),
        )

    async def delete_scope_version(self, workspace_id: UUID, scope_version_id: UUID) -> None:
        scope = self._db.scope_versions.get(scope_version_id)
        if scope is not None and scope.workspace_id == workspace_id:
            del self._db.scope_versions[scope_version_id]

    async def add_config_version(self, config: ConfigVersion) -> None:
        self._db.config_versions[config.id] = config

    async def get_config_version(self, config_version_id: UUID) -> ConfigVersion | None:
        return self._db.config_versions.get(config_version_id)

    async def find_config_version(
        self, workspace_id: UUID | None, *, kind: str, logical_id: str, semantic_version: str
    ) -> ConfigVersion | None:
        for config in self._db.config_versions.values():
            if (
                config.workspace_id == workspace_id
                and config.kind.value == kind
                and config.logical_id == logical_id
                and config.semantic_version == semantic_version
            ):
                return config
        return None

    async def list_config_versions(
        self, workspace_id: UUID | None, *, kind: str | None = None
    ) -> list[ConfigVersion]:
        configs = [
            config
            for config in self._db.config_versions.values()
            if config.workspace_id == workspace_id and (kind is None or config.kind.value == kind)
        ]
        return sorted(configs, key=lambda c: (c.logical_id, c.semantic_version))


#: ``(table, column) -> the MemoryDatabase attribute holding those rows``. Built
#: from the domain models themselves, so a new ``*_object_id`` field is covered
#: here the moment it exists rather than when somebody remembers this mapping.
_REFERENCE_MODELS: tuple[tuple[str, str, Any], ...] = (
    ("assets", "assets", Asset),
    ("renditions", "renditions", Rendition),
    ("parse_runs", "parse_runs", ParseRun),
    ("acquisition_attempts", "attempts", AcquisitionAttempt),
    ("source_requests", "source_requests", SourceRequest),
    ("source_observations", "observations", SourceObservation),
    ("evidence_spans", "spans", EvidenceSpan),
    ("corpus_releases", "releases", CorpusRelease),
)

_REFERENCE_STORES: dict[tuple[str, str], str] = {}


def _memory_reference_sources() -> tuple[ObjectReferenceSource, ...]:
    if not _REFERENCE_STORES:
        for table, store, model in _REFERENCE_MODELS:
            for name, field_info in model.model_fields.items():
                if name == "object_id" or name.endswith("_object_id"):
                    _REFERENCE_STORES[(table, name)] = store
                    _MEMORY_SOURCES.append(
                        ObjectReferenceSource(
                            table=table, column=name, required=field_info.is_required()
                        )
                    )
        _MEMORY_SOURCES.sort(key=lambda source: (source.table, source.column))
    return tuple(_MEMORY_SOURCES)


_MEMORY_SOURCES: list[ObjectReferenceSource] = []


class _Objects:
    """The in-memory object registry.

    ``register`` refuses to overwrite, exactly as the SQL adapter does, so a test
    cannot pass because this store quietly let a tombstone be cleared.
    """

    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def register(self, record: ObjectRecord) -> None:
        if record.id in self._db.objects:
            return
        self._db.objects[record.id] = record

    async def get(self, object_id: UUID) -> ObjectRecord | None:
        return self._db.objects.get(object_id)

    async def get_by_digest(self, workspace_id: UUID | None, sha256: str) -> ObjectRecord | None:
        for record in self._db.objects.values():
            if record.workspace_id == workspace_id and record.sha256 == sha256:
                return record
        return None

    async def tombstone(self, object_id: UUID, *, reason: str) -> None:
        record = self._db.objects.get(object_id)
        if record is None:
            return
        self._db.objects[object_id] = record.model_copy(
            update={"tombstoned": True, "tombstone_reason": reason}
        )

    async def list_objects(
        self,
        workspace_id: UUID,
        *,
        limit: int = 500,
        after_id: UUID | None = None,
        object_class: str | None = None,
    ) -> list[ObjectRecord]:
        rows = [
            record
            for record in self._db.objects.values()
            if record.workspace_id == workspace_id
            and (object_class is None or record.object_class == object_class)
        ]
        rows.sort(key=lambda record: record.id)
        if after_id is not None:
            rows = [record for record in rows if record.id > after_id]
        return rows[:limit]

    async def list_all_objects(
        self, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[ObjectRecord]:
        rows = sorted(self._db.objects.values(), key=lambda record: record.id)
        if after_id is not None:
            rows = [record for record in rows if record.id > after_id]
        return rows[:limit]

    async def count_objects(self, workspace_id: UUID) -> int:
        return sum(1 for record in self._db.objects.values() if record.workspace_id == workspace_id)

    def reference_sources(self) -> tuple[ObjectReferenceSource, ...]:
        """Only the sources this store can actually serve.

        Fewer than the SQL adapter's, because this store holds fewer kinds of
        row. Declaring the full schema list and answering the rest with an empty
        page would let a sweep report a clean graph while covering a fraction of
        it — the same shape as the bug this whole sweep exists to find.
        """
        return _memory_reference_sources()

    async def list_references(
        self,
        workspace_id: UUID,
        source: ObjectReferenceSource,
        *,
        limit: int = 500,
        after_id: UUID | None = None,
    ) -> list[ObjectReference]:
        store = _REFERENCE_STORES.get((source.table, source.column))
        if store is None:
            raise KeyError(
                f"the in-memory object registry does not model {source}; "
                "use the SQL adapter for a sweep that covers it"
            )
        rows = [
            row
            for row in getattr(self._db, store).values()
            if getattr(row, "workspace_id", None) == workspace_id
            and getattr(row, source.column, None) is not None
        ]
        rows.sort(key=lambda row: row.id)
        if after_id is not None:
            rows = [row for row in rows if row.id > after_id]
        return [
            ObjectReference(source=source, row_id=row.id, object_id=getattr(row, source.column))
            for row in rows[:limit]
        ]


class _Observations:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add_run(self, run: DiscoveryRun) -> None:
        self._db.discovery_runs[run.id] = run

    async def update_run(self, run: DiscoveryRun) -> None:
        self._db.discovery_runs[run.id] = run

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> DiscoveryRun | None:
        run = self._db.discovery_runs.get(run_id)
        if run is None:
            return None
        _check_workspace(workspace_id, run)
        return run

    async def list_runs(self, workspace_id: UUID, *, limit: int = 50) -> list[DiscoveryRun]:
        runs = [r for r in self._db.discovery_runs.values() if r.workspace_id == workspace_id]
        return sorted(runs, key=lambda r: r.started_at or datetime.min, reverse=True)[:limit]

    async def add_request(self, request: SourceRequest) -> None:
        self._db.source_requests[request.id] = request

    async def update_request(self, request: SourceRequest) -> None:
        self._db.source_requests[request.id] = request

    async def find_request_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> SourceRequest | None:
        for request in self._db.source_requests.values():
            if request.workspace_id == workspace_id and request.request_fingerprint == fingerprint:
                return request
        return None

    async def list_requests(self, workspace_id: UUID, run_id: UUID) -> list[SourceRequest]:
        return [
            request
            for request in self._db.source_requests.values()
            if request.workspace_id == workspace_id and request.discovery_run_id == run_id
        ]

    async def add_observation(self, observation: SourceObservation) -> None:
        self._db.observations[observation.id] = observation

    async def list_observations(
        self, workspace_id: UUID, *, request_id: UUID | None = None
    ) -> list[SourceObservation]:
        return [
            observation
            for observation in self._db.observations.values()
            if observation.workspace_id == workspace_id
            and (request_id is None or observation.source_request_id == request_id)
        ]

    async def add_assertions(self, assertions: Sequence[SourceAssertion]) -> None:
        for assertion in assertions:
            self._db.assertions[assertion.id] = assertion

    async def list_assertions(
        self,
        workspace_id: UUID,
        *,
        observation_id: UUID | None = None,
        subject_hint: str | None = None,
        work_id: UUID | None = None,
    ) -> list[SourceAssertion]:
        result = []
        for assertion in self._db.assertions.values():
            if assertion.workspace_id != workspace_id:
                continue
            if observation_id is not None and assertion.source_observation_id != observation_id:
                continue
            if subject_hint is not None and assertion.subject_hint != subject_hint:
                continue
            if work_id is not None and assertion.work_id != work_id:
                continue
            result.append(assertion)
        return result

    async def update_assertion(self, assertion: SourceAssertion) -> None:
        self._db.assertions[assertion.id] = assertion

    async def get_cursor(self, workspace_id: UUID, *, lineage_key: str) -> str | None:
        return self._db.cursors.get((workspace_id, lineage_key), (None, False))[0]

    async def set_cursor(
        self, workspace_id: UUID, *, lineage_key: str, position: str | None, exhausted: bool
    ) -> None:
        self._db.cursors[(workspace_id, lineage_key)] = (position, exhausted)


class _Works:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add(self, work: Work) -> None:
        self._db.works[work.id] = work

    async def update(self, work: Work) -> None:
        self._db.works[work.id] = work

    async def get(self, workspace_id: UUID, work_id: UUID) -> Work | None:
        work = self._db.works.get(work_id)
        if work is None:
            return None
        _check_workspace(workspace_id, work)
        return work

    async def list_works(
        self,
        workspace_id: UUID,
        *,
        relevance: str | None = None,
        limit: int = 100,
        after_id: UUID | None = None,
    ) -> list[Work]:
        works = [w for w in self._db.works.values() if w.workspace_id == workspace_id]
        if relevance is not None:
            works = [w for w in works if w.relevance.value == relevance]
        ordered = sorted(works, key=lambda w: str(w.id))
        if after_id is not None:
            cursor = str(after_id)
            ordered = [w for w in ordered if str(w.id) > cursor]
        return ordered[:limit]

    async def list_work_visibility(
        self,
        workspace_id: UUID,
        *,
        relevance: str | None = None,
        limit: int = 500,
        after_id: UUID | None = None,
    ) -> list[WorkVisibilityRow]:
        page = await self.list_works(
            workspace_id, relevance=relevance, limit=limit, after_id=after_id
        )
        rows: list[WorkVisibilityRow] = []
        for work in page:
            restricted: list[UUID] = []
            sensitivity = "internal"
            has_decision = False
            states: list[str] = []
            due: datetime | None = None
            for asset in self._db.assets.values():
                if asset.workspace_id != workspace_id or asset.work_id != work.id:
                    continue
                if asset.rights_decision_id is None:
                    continue
                decision = self._db.rights_decisions.get(asset.rights_decision_id)
                if decision is None:
                    continue
                has_decision = True
                restricted.extend(decision.restricted_to_group_ids)
                if decision.restrictions:
                    sensitivity = "restricted"
                states.append(decision.status.value)
                if decision.review_due_at is not None:
                    due = (
                        decision.review_due_at if due is None else min(due, decision.review_due_at)
                    )
            rows.append(
                WorkVisibilityRow(
                    work_id=work.id,
                    is_active=work.is_active,
                    relevance=work.relevance.value,
                    restricted_to_groups=tuple(dict.fromkeys(restricted)),
                    sensitivity=sensitivity,
                    has_rights_decision=has_decision,
                    rights_states=tuple(dict.fromkeys(states)),
                    earliest_review_due=due,
                )
            )
        return rows

    async def add_identifier(self, identifier: WorkIdentifier) -> None:
        self._db.identifiers[identifier.id] = identifier

    async def update_identifier(self, identifier: WorkIdentifier) -> None:
        self._db.identifiers[identifier.id] = identifier

    async def list_identifiers(self, workspace_id: UUID, work_id: UUID) -> list[WorkIdentifier]:
        return [
            identifier
            for identifier in self._db.identifiers.values()
            if identifier.workspace_id == workspace_id and identifier.work_id == work_id
        ]

    async def find_by_identifier(
        self, workspace_id: UUID, *, scheme: str, value_normalized: str
    ) -> Work | None:
        for identifier in self._db.identifiers.values():
            if (
                identifier.workspace_id == workspace_id
                and identifier.scheme.value == scheme
                and identifier.value_normalized == value_normalized
                and identifier.status == "active"
            ):
                work = self._db.works.get(identifier.work_id)
                if work is not None and work.is_active:
                    return work
        return None

    async def add_relation(self, relation: WorkRelation) -> None:
        self._db.relations[relation.id] = relation

    async def list_relations(self, workspace_id: UUID, work_id: UUID) -> list[WorkRelation]:
        return [
            relation
            for relation in self._db.relations.values()
            if relation.workspace_id == workspace_id
            and work_id in (relation.from_work_id, relation.to_work_id)
        ]

    async def add_decision(self, decision: IdentityDecision) -> None:
        self._db.identity_decisions[decision.id] = decision

    async def list_decisions(
        self, workspace_id: UUID, *, work_id: UUID | None = None
    ) -> list[IdentityDecision]:
        decisions = [
            decision
            for decision in self._db.identity_decisions.values()
            if decision.workspace_id == workspace_id
            and (work_id is None or work_id in decision.subject_work_ids)
        ]
        return sorted(decisions, key=lambda d: d.decided_at or datetime.min)


class _Assets:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add_manifestation(self, manifestation: Manifestation) -> None:
        self._db.manifestations[manifestation.id] = manifestation

    async def get_manifestation(
        self, workspace_id: UUID, manifestation_id: UUID
    ) -> Manifestation | None:
        manifestation = self._db.manifestations.get(manifestation_id)
        if manifestation is None:
            return None
        _check_workspace(workspace_id, manifestation)
        return manifestation

    async def list_manifestations(self, workspace_id: UUID, work_id: UUID) -> list[Manifestation]:
        return [
            manifestation
            for manifestation in self._db.manifestations.values()
            if manifestation.workspace_id == workspace_id and manifestation.work_id == work_id
        ]

    async def add_plan(self, plan: AcquisitionPlan) -> None:
        self._db.plans[plan.id] = plan

    async def update_plan(self, plan: AcquisitionPlan) -> None:
        self._db.plans[plan.id] = plan

    async def get_plan(self, workspace_id: UUID, plan_id: UUID) -> AcquisitionPlan | None:
        plan = self._db.plans.get(plan_id)
        if plan is None:
            return None
        _check_workspace(workspace_id, plan)
        return plan

    async def list_plans(
        self, workspace_id: UUID, *, status: str | None = None
    ) -> list[AcquisitionPlan]:
        plans = [p for p in self._db.plans.values() if p.workspace_id == workspace_id]
        if status is not None:
            plans = [p for p in plans if p.status == status]
        return sorted(plans, key=lambda p: str(p.id))

    async def add_attempt(self, attempt: AcquisitionAttempt) -> None:
        self._db.attempts[attempt.id] = attempt

    async def list_attempts(
        self, workspace_id: UUID, *, plan_id: UUID | None = None
    ) -> list[AcquisitionAttempt]:
        attempts = [
            attempt
            for attempt in self._db.attempts.values()
            if attempt.workspace_id == workspace_id
            and (plan_id is None or attempt.acquisition_plan_id == plan_id)
        ]
        return sorted(attempts, key=lambda a: a.attempt_number)

    async def add_asset(self, asset: Asset) -> None:
        self._db.assets[asset.id] = asset

    async def update_asset(self, asset: Asset) -> None:
        self._db.assets[asset.id] = asset

    async def get_asset(self, workspace_id: UUID, asset_id: UUID) -> Asset | None:
        asset = self._db.assets.get(asset_id)
        if asset is None:
            return None
        _check_workspace(workspace_id, asset)
        return asset

    async def find_asset_by_digest(
        self,
        workspace_id: UUID,
        sha256: str,
        *,
        work_id: UUID | None = None,
        manifestation_id: UUID | None = None,
    ) -> Asset | None:
        for asset in sorted(self._db.assets.values(), key=lambda a: str(a.id)):
            if asset.workspace_id != workspace_id or asset.sha256 != sha256:
                continue
            if work_id is not None and asset.work_id != work_id:
                continue
            if manifestation_id is not None and asset.manifestation_id != manifestation_id:
                continue
            return asset
        return None

    async def list_assets(self, workspace_id: UUID, *, work_id: UUID | None = None) -> list[Asset]:
        return [
            asset
            for asset in self._db.assets.values()
            if asset.workspace_id == workspace_id and (work_id is None or asset.work_id == work_id)
        ]


class _Renditions:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add_parse_run(self, run: ParseRun) -> None:
        self._db.parse_runs[run.id] = run

    async def update_parse_run(self, run: ParseRun) -> None:
        self._db.parse_runs[run.id] = run

    async def get_parse_run(self, workspace_id: UUID, run_id: UUID) -> ParseRun | None:
        run = self._db.parse_runs.get(run_id)
        if run is None:
            return None
        _check_workspace(workspace_id, run)
        return run

    async def find_parse_run_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> ParseRun | None:
        for run in self._db.parse_runs.values():
            if run.workspace_id == workspace_id and run.run_fingerprint == fingerprint:
                return run
        return None

    async def add_rendition(self, rendition: Rendition) -> None:
        self._db.renditions[rendition.id] = rendition

    async def update_rendition(self, rendition: Rendition) -> None:
        self._db.renditions[rendition.id] = rendition

    async def get_rendition(self, workspace_id: UUID, rendition_id: UUID) -> Rendition | None:
        rendition = self._db.renditions.get(rendition_id)
        if rendition is None:
            return None
        _check_workspace(workspace_id, rendition)
        return rendition

    async def list_renditions(
        self, workspace_id: UUID, *, asset_id: UUID | None = None, work_id: UUID | None = None
    ) -> list[Rendition]:
        return [
            rendition
            for rendition in self._db.renditions.values()
            if rendition.workspace_id == workspace_id
            and (asset_id is None or rendition.asset_id == asset_id)
            and (work_id is None or rendition.work_id == work_id)
        ]

    async def add_spans(self, spans: Sequence[EvidenceSpan]) -> None:
        for span in spans:
            self._db.spans[span.id] = span

    async def get_span(self, workspace_id: UUID, span_id: UUID) -> EvidenceSpan | None:
        span = self._db.spans.get(span_id)
        if span is None:
            return None
        _check_workspace(workspace_id, span)
        return span

    async def list_spans(self, workspace_id: UUID, rendition_id: UUID) -> list[EvidenceSpan]:
        return [
            span
            for span in self._db.spans.values()
            if span.workspace_id == workspace_id and span.rendition_id == rendition_id
        ]


class _Claims:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add_entity(self, entity: Entity) -> None:
        self._db.entities[entity.id] = entity

    async def find_entity(
        self, workspace_id: UUID, *, entity_type: str, normalised_value: str
    ) -> Entity | None:
        for entity in self._db.entities.values():
            if (
                entity.workspace_id == workspace_id
                and entity.entity_type.value == entity_type
                and entity.normalised_value == normalised_value
            ):
                return entity
        return None

    async def get_entity(self, workspace_id: UUID, entity_id: UUID) -> Entity | None:
        entity = self._db.entities.get(entity_id)
        return entity if entity is not None and entity.workspace_id == workspace_id else None

    async def add_experiment(self, experiment: Experiment) -> None:
        self._db.experiments[experiment.id] = experiment

    async def update_experiment(self, experiment: Experiment) -> None:
        self._db.experiments[experiment.id] = experiment

    async def list_experiments(self, workspace_id: UUID, work_id: UUID) -> list[Experiment]:
        return [
            experiment
            for experiment in self._db.experiments.values()
            if experiment.workspace_id == workspace_id and experiment.work_id == work_id
        ]

    async def add_claim(self, claim: Claim) -> None:
        self._db.claims[claim.id] = claim

    async def update_claim(self, claim: Claim) -> None:
        self._db.claims[claim.id] = claim

    async def get_claim(self, workspace_id: UUID, claim_id: UUID) -> Claim | None:
        claim = self._db.claims.get(claim_id)
        if claim is None:
            return None
        _check_workspace(workspace_id, claim)
        return claim

    async def list_claims(
        self,
        workspace_id: UUID,
        *,
        work_id: UUID | None = None,
        review_status: str | None = None,
        limit: int = 200,
        after_id: UUID | None = None,
    ) -> list[Claim]:
        claims = [
            claim
            for claim in self._db.claims.values()
            if claim.workspace_id == workspace_id and (work_id is None or claim.work_id == work_id)
        ]
        if review_status is not None:
            filtered = []
            for claim in claims:
                revision = (
                    self._db.revisions.get(claim.current_revision_id)
                    if claim.current_revision_id
                    else None
                )
                if revision is not None and revision.review_status.value == review_status:
                    filtered.append(claim)
            claims = filtered
        ordered = sorted(claims, key=lambda c: str(c.id))
        if after_id is not None:
            cursor = str(after_id)
            ordered = [claim for claim in ordered if str(claim.id) > cursor]
        return ordered[:limit]

    async def add_revision(self, revision: ClaimRevision) -> None:
        self._db.revisions[revision.id] = revision

    async def update_revision(self, revision: ClaimRevision) -> None:
        self._db.revisions[revision.id] = revision

    async def get_revision(self, workspace_id: UUID, revision_id: UUID) -> ClaimRevision | None:
        revision = self._db.revisions.get(revision_id)
        if revision is None:
            return None
        _check_workspace(workspace_id, revision)
        return revision

    async def list_revisions(self, workspace_id: UUID, claim_id: UUID) -> list[ClaimRevision]:
        revisions = [
            revision
            for revision in self._db.revisions.values()
            if revision.workspace_id == workspace_id and revision.claim_id == claim_id
        ]
        return sorted(revisions, key=lambda r: r.revision_number)

    async def find_revision_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> ClaimRevision | None:
        for revision in self._db.revisions.values():
            if (
                revision.workspace_id == workspace_id
                and revision.extractor_fingerprint == fingerprint
            ):
                return revision
        return None

    async def add_review(self, review: Review) -> None:
        self._db.reviews[review.id] = review

    async def list_reviews(
        self, workspace_id: UUID, *, subject_id: UUID | None = None
    ) -> list[Review]:
        return [
            review
            for review in self._db.reviews.values()
            if review.workspace_id == workspace_id
            and (subject_id is None or review.subject_id == subject_id)
        ]


class _Imports:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add(self, record: ImportRecord) -> None:
        self._db.imports[record.id] = record

    async def latest(self, library_id: UUID) -> ImportRecord | None:
        active = [
            record
            for record in self._db.imports.values()
            if record.library_id == library_id and record.state is ImportState.ACTIVE
        ]
        return max(active, key=lambda r: (r.commit_sequence, r.imported_at), default=None)

    async def put_work_metadata(
        self,
        workspace_id: UUID,
        work_id: UUID,
        *,
        library_id: UUID,
        title: str | None,
        publication_year: int | None,
        container_title: str | None,
    ) -> None:
        self._db.work_metadata[(workspace_id, work_id)] = {
            "library_id": library_id,
            "title": title,
            "publication_year": publication_year,
            "container_title": container_title,
        }

    async def get_work_metadata(self, workspace_id: UUID, work_id: UUID) -> dict[str, Any] | None:
        found = self._db.work_metadata.get((workspace_id, work_id))
        return dict(found) if found is not None else None


class _StageResults:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def record(self, result: StageResultEnvelope, *, workspace_id: UUID) -> None:
        self._db.stage_results[(workspace_id, result.reuse_fingerprint)] = result

    async def find(self, workspace_id: UUID, reuse_fingerprint: str) -> StageResultEnvelope | None:
        return self._db.stage_results.get((workspace_id, reuse_fingerprint))


class _Publications:
    """The publication outbox, in memory.

    Enforces the same uniqueness the database does — one attempt per library
    and snapshot. A fake that allowed two would let a duplicate-publication
    test pass while proving nothing about the constraint that actually
    prevents it.
    """

    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add(self, record: PublicationRecord) -> None:
        for existing in self._db.publications.values():
            if (
                existing.library_id == record.library_id
                and existing.snapshot_id == record.snapshot_id
                and existing.id != record.id
            ):
                raise LabAssistantError(
                    "an attempt to publish this snapshot already exists",
                    context={
                        "library_id": str(record.library_id),
                        "snapshot_id": str(record.snapshot_id),
                    },
                ).with_class(ErrorClass.CONFLICT)
        self._db.publications[record.id] = record

    async def update(self, record: PublicationRecord) -> None:
        self._db.publications[record.id] = record

    async def get(self, workspace_id: UUID, record_id: UUID) -> PublicationRecord | None:
        record = self._db.publications.get(record_id)
        if record is None or record.workspace_id != workspace_id:
            return None
        return record

    async def find_by_snapshot(
        self, library_id: UUID, snapshot_id: UUID
    ) -> PublicationRecord | None:
        for record in self._db.publications.values():
            if record.library_id == library_id and record.snapshot_id == snapshot_id:
                return record
        return None

    async def list_open(self, library_id: UUID) -> list[PublicationRecord]:
        return sorted(
            (
                record
                for record in self._db.publications.values()
                if record.library_id == library_id and not record.state.is_terminal
            ),
            key=lambda record: (record.created_at or _EPOCH, str(record.id)),
        )

    async def list_for_library(self, library_id: UUID) -> list[PublicationRecord]:
        return sorted(
            (
                record
                for record in self._db.publications.values()
                if record.library_id == library_id
            ),
            key=lambda record: (record.created_at or _EPOCH, str(record.id)),
        )


class _Releases:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add(self, release: CorpusRelease) -> None:
        self._db.releases[release.id] = release

    async def update(self, release: CorpusRelease) -> None:
        self._db.releases[release.id] = release

    async def get(self, workspace_id: UUID, release_id: UUID) -> CorpusRelease | None:
        release = self._db.releases.get(release_id)
        if release is None:
            return None
        _check_workspace(workspace_id, release)
        return release

    async def list_releases(self, workspace_id: UUID, *, limit: int = 50) -> list[CorpusRelease]:
        releases = [r for r in self._db.releases.values() if r.workspace_id == workspace_id]
        return sorted(releases, key=lambda r: r.created_at or datetime.min, reverse=True)[:limit]

    async def list_all_releases(
        self, workspace_id: UUID, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[CorpusRelease]:
        releases = sorted(
            (r for r in self._db.releases.values() if r.workspace_id == workspace_id),
            key=lambda r: r.id,
        )
        if after_id is not None:
            releases = [r for r in releases if r.id > after_id]
        return releases[:limit]

    async def add_items(self, items: Sequence[ReleaseItem]) -> None:
        for item in items:
            self._db.release_items[item.id] = item

    async def list_items(self, workspace_id: UUID, release_id: UUID) -> list[ReleaseItem]:
        items = [
            item
            for item in self._db.release_items.values()
            if item.workspace_id == workspace_id and item.release_id == release_id
        ]
        return sorted(items, key=lambda i: i.record.sort_key)

    async def get_alias(self, workspace_id: UUID, alias: str) -> SearchAlias | None:
        return self._db.aliases.get((workspace_id, alias))

    async def upsert_alias(self, alias: SearchAlias) -> None:
        self._db.aliases[(alias.workspace_id, alias.alias)] = alias

    async def list_aliases(self, workspace_id: UUID) -> list[SearchAlias]:
        return [a for (ws, _), a in self._db.aliases.items() if ws == workspace_id]


class _Tasks:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add(self, task: Task) -> None:
        self._db.tasks[task.id] = task

    async def update(self, task: Task) -> None:
        previous = self._db.tasks.get(task.id)
        if (
            previous is not None
            and previous.state is TaskState.LEASED
            and (task.state is not TaskState.LEASED)
        ):
            self._db.leased_by_stage[task.stage] = max(0, self._db.leased_by_stage[task.stage] - 1)
        self._db.tasks[task.id] = task

    async def get(self, workspace_id: UUID, task_id: UUID) -> Task | None:
        task = self._db.tasks.get(task_id)
        if task is None:
            return None
        _check_workspace(workspace_id, task)
        return task

    async def find_by_fingerprint(self, workspace_id: UUID, fingerprint: str) -> Task | None:
        for task in self._db.tasks.values():
            if task.workspace_id == workspace_id and task.input_fingerprint == fingerprint:
                return task
        return None

    async def claim_next(
        self,
        *,
        owner: str,
        worker_class: str,
        now: datetime,
        stages: Sequence[StageName] | None = None,
        lease_seconds: int = 300,
        stage_concurrency: dict[StageName, int] | None = None,
    ) -> Task | None:
        eligible = [
            task
            for task in self._db.tasks.values()
            if task.worker_class == worker_class
            and (stages is None or task.stage in stages)
            and task.is_claimable(now)
        ]
        # Priority, then creation order: deterministic and starvation-free.
        eligible.sort(key=lambda t: (t.priority, t.created_at or datetime.min, str(t.id)))
        caps = stage_concurrency or {}
        for task in eligible:
            in_flight = self._db.leased_task_ids[task.stage]
            # An expired lease is reclaimable: the previous owner is gone.
            in_flight = {
                task_id
                for task_id in in_flight
                if (held := self._db.tasks.get(task_id)) is not None
                and held.state is TaskState.LEASED
                and (held.lease_expires_at is None or held.lease_expires_at > now)
            }
            self._db.leased_task_ids[task.stage] = in_flight
            if len(in_flight) >= caps.get(task.stage, 1):
                continue
            task.claim(owner=owner, now=now, lease_seconds=lease_seconds)
            in_flight.add(task.id)
            self._db.tasks[task.id] = task
            return task
        return None

    async def list_tasks(
        self,
        workspace_id: UUID,
        *,
        state: str | None = None,
        stage: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        tasks = [
            task
            for task in self._db.tasks.values()
            if task.workspace_id == workspace_id
            and (state is None or task.state.value == state)
            and (stage is None or task.stage.value == stage)
        ]
        return sorted(tasks, key=lambda t: t.created_at or datetime.min)[:limit]

    async def count_by_state(self, workspace_id: UUID) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self._db.tasks.values():
            if task.workspace_id != workspace_id:
                continue
            counts[task.state.value] = counts.get(task.state.value, 0) + 1
        return dict(sorted(counts.items()))

    async def release_dependencies(self, workspace_id: UUID, completed_task_id: UUID) -> list[Task]:
        unblocked: list[Task] = []
        for task in self._db.tasks.values():
            if task.workspace_id != workspace_id or task.state is not TaskState.BLOCKED:
                continue
            if completed_task_id not in task.depends_on:
                continue
            remaining = [
                dependency
                for dependency in task.depends_on
                if dependency != completed_task_id
                and (self._db.tasks.get(dependency) or task).state is not TaskState.SUCCEEDED
            ]
            if not remaining:
                task.release_dependency_block()
                unblocked.append(task)
        return unblocked

    async def get_rate_budget(self, key: str, *, for_update: bool = False) -> RateBudget | None:
        # One process, one event loop: there is nothing to lock against. The
        # argument exists so the in-memory and SQL adapters stay interchangeable.
        return self._db.rate_budgets.get(key)

    async def upsert_rate_budget(self, budget: RateBudget) -> None:
        self._db.rate_budgets[budget.key] = budget


class _Audit:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def append(self, event: AuditEventRecord) -> None:
        self._db.audit_events.append(event)

    async def latest_hash(self, workspace_id: UUID | None) -> str | None:
        for event in reversed(self._db.audit_events):
            if event.workspace_id == workspace_id:
                return event.integrity_hash
        return None

    async def list_events(
        self,
        workspace_id: UUID | None,
        *,
        subject_id: UUID | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[AuditEventRecord]:
        events = [
            event
            for event in self._db.audit_events
            if event.workspace_id == workspace_id
            and (subject_id is None or event.subject_id == subject_id)
            and (action is None or event.action.value == action)
        ]
        return events[-limit:]

    async def add_outbox(self, event: OutboxEvent) -> None:
        self._db.outbox[event.id] = event

    async def list_unpublished_outbox(self, *, limit: int = 100) -> list[OutboxEvent]:
        return [e for e in self._db.outbox.values() if e.published_at is None][:limit]

    async def mark_outbox_published(self, event_id: UUID, *, at: datetime) -> None:
        event = self._db.outbox.get(event_id)
        if event is not None:
            self._db.outbox[event_id] = event.model_copy(update={"published_at": at})


class _Chat:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add_session(self, session: ChatSession) -> None:
        self._db.chat_sessions[session.id] = session

    async def update_session(self, session: ChatSession) -> None:
        self._db.chat_sessions[session.id] = session

    async def get_session(self, workspace_id: UUID, session_id: UUID) -> ChatSession | None:
        session = self._db.chat_sessions.get(session_id)
        if session is None:
            return None
        _check_workspace(workspace_id, session)
        return session

    async def list_sessions(
        self, workspace_id: UUID, user_id: UUID, *, limit: int = 50
    ) -> list[ChatSession]:
        sessions = [
            session
            for session in self._db.chat_sessions.values()
            if session.workspace_id == workspace_id and session.user_id == user_id
        ]
        return sorted(sessions, key=lambda s: s.updated_at or datetime.min, reverse=True)[:limit]

    async def list_workspace_sessions(
        self, workspace_id: UUID, *, limit: int = 1000
    ) -> list[ChatSession]:
        sessions = [
            session
            for session in self._db.chat_sessions.values()
            if session.workspace_id == workspace_id
        ]
        return sorted(sessions, key=lambda s: str(s.id))[:limit]

    async def add_message(self, message: ChatMessage) -> None:
        self._db.chat_messages[message.id] = message

    async def list_messages(self, workspace_id: UUID, session_id: UUID) -> list[ChatMessage]:
        messages = [
            message
            for message in self._db.chat_messages.values()
            if message.workspace_id == workspace_id and message.session_id == session_id
        ]
        return sorted(messages, key=lambda m: m.created_at or datetime.min)

    async def get_message(self, workspace_id: UUID, message_id: UUID) -> ChatMessage | None:
        message = self._db.chat_messages.get(message_id)
        if message is None:
            return None
        _check_workspace(workspace_id, message)
        return message

    async def add_feedback(self, feedback: Feedback) -> None:
        self._db.feedback[feedback.id] = feedback

    async def list_feedback(self, workspace_id: UUID, *, limit: int = 100) -> list[Feedback]:
        return [f for f in self._db.feedback.values() if f.workspace_id == workspace_id][:limit]

    async def usage_by_user(
        self, workspace_id: UUID, *, user_ids: Sequence[UUID] | None = None
    ) -> dict[UUID, UsageTotals]:
        wanted = set(user_ids) if user_ids is not None else None
        owner = {
            session.id: session.user_id
            for session in self._db.chat_sessions.values()
            if session.workspace_id == workspace_id
            and (wanted is None or session.user_id in wanted)
        }
        totals: dict[UUID, dict[str, Any]] = {}
        for user_id in owner.values():
            row = totals.setdefault(
                user_id,
                {
                    "sessions": 0,
                    "questions": 0,
                    "answers": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "last_active_at": None,
                    "latencies": [],
                },
            )
            row["sessions"] += 1
        for message in self._db.chat_messages.values():
            asker = owner.get(message.session_id)
            if asker is None:
                continue
            row = totals[asker]
            if message.role is MessageRole.USER:
                row["questions"] += 1
            else:
                row["answers"] += 1
                row["latencies"].append(message.latency_ms)
            row["input_tokens"] += message.input_tokens
            row["output_tokens"] += message.output_tokens
            if message.created_at and (
                row["last_active_at"] is None or message.created_at > row["last_active_at"]
            ):
                row["last_active_at"] = message.created_at
        return {
            user_id: UsageTotals(
                sessions=row["sessions"],
                questions=row["questions"],
                answers=row["answers"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                last_active_at=row["last_active_at"],
                avg_latency_ms=(
                    sum(row["latencies"]) / len(row["latencies"]) if row["latencies"] else None
                ),
            )
            for user_id, row in totals.items()
        }


class _Evaluations:
    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db

    async def add_run(self, run: dict[str, Any]) -> None:
        row = {"status": "succeeded", "created_at": None, **run}
        # Insertion order stands in for created_at, which the memory store does not
        # stamp; a counter keeps "newest first" deterministic under a fixed clock.
        row["_sequence"] = len(self._db.evaluation_runs)
        self._db.evaluation_runs[str(run["id"])] = row

    async def update_run(self, run: dict[str, Any]) -> None:
        current = self._db.evaluation_runs.get(str(run["id"]))
        if current is None:
            raise KeyError(f"evaluation run {run['id']} does not exist")
        current.update({key: value for key, value in run.items() if key != "_sequence"})

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> dict[str, Any] | None:
        run = self._db.evaluation_runs.get(str(run_id))
        if run is None or str(run.get("workspace_id")) != str(workspace_id):
            return None
        return run

    async def list_runs(
        self,
        workspace_id: UUID,
        *,
        benchmark_id: str | None = None,
        release_id: UUID | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        runs = [
            run
            for run in self._db.evaluation_runs.values()
            if str(run.get("workspace_id")) == str(workspace_id)
            and (benchmark_id is None or str(run.get("benchmark", "")).startswith(benchmark_id))
            and (release_id is None or str(run.get("release_id")) == str(release_id))
        ]
        runs.sort(key=lambda run: run.get("_sequence", 0), reverse=True)
        return runs[:limit]

    async def add_benchmark(self, benchmark: dict[str, Any]) -> None:
        key = (str(benchmark["workspace_id"]), benchmark["benchmark_id"], benchmark["version"])
        if key in self._db.benchmarks:
            raise ConcurrencyConflict(f"benchmark {key[1]}@{key[2]} already exists")
        self._db.benchmarks[key] = dict(benchmark)

    async def get_benchmark(
        self, workspace_id: UUID, benchmark_id: str, version: str
    ) -> dict[str, Any] | None:
        return self._db.benchmarks.get((str(workspace_id), benchmark_id, version))

    async def list_benchmarks(self, workspace_id: UUID) -> list[dict[str, Any]]:
        return sorted(
            (row for key, row in self._db.benchmarks.items() if key[0] == str(workspace_id)),
            key=lambda row: (row["benchmark_id"], row["version"]),
        )

    async def add_review(self, review: dict[str, Any]) -> None:
        for row in self._db.evaluation_reviews.values():
            if (str(row["run_id"]), row["case_id"], str(row["assigned_to"])) == (
                str(review["run_id"]),
                review["case_id"],
                str(review["assigned_to"]),
            ):
                raise ConcurrencyConflict("this case is already assigned to this reviewer")
        row = {"status": "assigned", "scores": {}, "flags": [], **review}
        row["_sequence"] = len(self._db.evaluation_reviews)
        self._db.evaluation_reviews[str(review["id"])] = row

    async def update_review(self, review: dict[str, Any]) -> None:
        current = self._db.evaluation_reviews.get(str(review["id"]))
        if current is None:
            raise KeyError(f"evaluation review {review['id']} does not exist")
        current.update({key: value for key, value in review.items() if key != "_sequence"})

    async def get_review(self, workspace_id: UUID, review_id: UUID) -> dict[str, Any] | None:
        row = self._db.evaluation_reviews.get(str(review_id))
        if row is None or str(row.get("workspace_id")) != str(workspace_id):
            return None
        return row

    async def list_reviews(
        self,
        workspace_id: UUID,
        *,
        run_id: UUID | None = None,
        assigned_to: UUID | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self._db.evaluation_reviews.values()
            if str(row.get("workspace_id")) == str(workspace_id)
            and (run_id is None or str(row["run_id"]) == str(run_id))
            and (assigned_to is None or str(row["assigned_to"]) == str(assigned_to))
        ]
        return sorted(rows, key=lambda row: row.get("_sequence", 0))

    async def list_release_gates(self, workspace_id: UUID) -> list[dict[str, Any]]:
        return sorted(
            (row for key, row in self._db.release_gates.items() if key[0] == str(workspace_id)),
            key=lambda row: row["benchmark_id"],
        )

    async def set_release_gate(self, gate: dict[str, Any]) -> None:
        self._db.release_gates[(str(gate["workspace_id"]), gate["benchmark_id"])] = dict(gate)

    async def clear_release_gate(self, workspace_id: UUID, benchmark_id: str) -> None:
        self._db.release_gates.pop((str(workspace_id), benchmark_id), None)


class MemoryUnitOfWork:
    """A unit of work over the in-memory store.

    ``commit`` is a no-op because writes land in the shared dictionaries
    immediately; ``rollback`` raises rather than pretending, so a test that
    depends on rollback semantics fails loudly instead of passing by accident.
    """

    def __init__(self, db: MemoryDatabase) -> None:
        self._db = db
        self.users = _Users(db)
        self.workspaces = _Workspaces(db)
        self.objects = _Objects(db)
        self.policies = _Policies(db)
        self.configs = _Configs(db)
        self.observations = _Observations(db)
        self.works = _Works(db)
        self.assets = _Assets(db)
        self.renditions = _Renditions(db)
        self.claims = _Claims(db)
        self.releases = _Releases(db)
        self.publications = _Publications(db)
        self.stage_results = _StageResults(db)
        self.imports = _Imports(db)
        self.tasks = _Tasks(db)
        self.audit = _Audit(db)
        self.chat = _Chat(db)
        self.evaluations = _Evaluations(db)
        self._committed = False

    async def __aenter__(self) -> MemoryUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self._committed = True

    async def rollback(self) -> None:
        raise NotImplementedError(
            "the in-memory unit of work cannot roll back; use the PostgreSQL adapter for "
            "tests that depend on transactional rollback"
        )


def memory_uow_factory(db: MemoryDatabase):
    """Return a callable that produces units of work over ``db``."""

    def factory() -> MemoryUnitOfWork:
        return MemoryUnitOfWork(db)

    return factory
