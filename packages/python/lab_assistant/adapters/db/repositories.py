"""SQL repositories and the unit of work.

Every method takes ``workspace_id`` and filters on it. The task claim uses
``FOR UPDATE SKIP LOCKED`` on PostgreSQL, which is what makes several workers safe
without a broker; SQLite falls back to a plain read, which is fine because the
offline suite runs one worker at a time.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from functools import lru_cache
from types import TracebackType
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from lab_assistant.adapters.db import tables as t
from lab_assistant.adapters.db.mapper import Mapper, uuid_list, uuid_or_none
from lab_assistant.application.ports import (
    ObjectReference,
    ObjectReferenceSource,
    WorkVisibilityRow,
)
from lab_assistant.contracts.events import AuditEventRecord, OutboxEvent
from lab_assistant.contracts.policy import GrantSpec
from lab_assistant.contracts.publication import PublicationRecord, PublicationState
from lab_assistant.contracts.release import ReleaseItemRecord
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.assets import (
    AcquisitionAttempt,
    AcquisitionPlan,
    Asset,
    Manifestation,
    RightsDecision,
)
from lab_assistant.domain.chat import ChatMessage, ChatSession, Feedback
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

__all__ = ["SqlUnitOfWork", "sql_uow_factory"]


def _capabilities(value: Any) -> frozenset:
    from lab_assistant.domain.workspace import Capability

    return frozenset(Capability(item) for item in (value or []))


def _actions(value: Any) -> frozenset:
    from lab_assistant.contracts.policy import PolicyAction

    return frozenset(PolicyAction(item) for item in (value or []))


def _routes(value: Any) -> tuple:
    from lab_assistant.domain.assets import AcquisitionRoute

    return tuple(AcquisitionRoute(item) for item in (value or []))


def _str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in (value or []))


# ── mappers ─────────────────────────────────────────────────────────────────
USERS = Mapper(t.users, User)
WORKSPACES = Mapper(t.workspaces, Workspace)
MEMBERSHIPS = Mapper(t.memberships, Membership, loaders={"extra_capabilities": _capabilities})
GROUPS = Mapper(t.groups, Group)
INVITATIONS = Mapper(t.invitations, Invitation)
SCOPES = Mapper(
    t.scope_versions,
    ScopeVersion,
    computed={
        "config_checksum": lambda scope: scope.config_checksum,
        "payload": lambda scope: scope.model_dump(mode="json"),
    },
)
CONFIGS = Mapper(t.config_versions, ConfigVersion, computed={"checksum": lambda c: c.checksum})
RUNS = Mapper(
    t.discovery_runs,
    DiscoveryRun,
    renames={"metrics": "metrics"},
    loaders={"connector_states": lambda rows: tuple(rows or ())},
)
OBJECTS = Mapper(t.objects, ObjectRecord)
REQUESTS = Mapper(t.source_requests, SourceRequest)
OBSERVATIONS = Mapper(t.source_observations, SourceObservation)
ASSERTIONS = Mapper(t.source_assertions, SourceAssertion)
WORKS = Mapper(t.works, Work)
IDENTIFIERS = Mapper(t.work_identifiers, WorkIdentifier)
RELATIONS = Mapper(t.work_relations, WorkRelation, loaders={"assertion_ids": uuid_list})
DECISIONS = Mapper(
    t.identity_decisions,
    IdentityDecision,
    renames={"before": "before_json", "after": "after_json"},
    loaders={"subject_work_ids": uuid_list, "supporting_assertion_ids": uuid_list},
)
MANIFESTATIONS = Mapper(
    t.manifestations, Manifestation, loaders={"relation_assertion_ids": uuid_list}
)
RIGHTS = Mapper(
    t.rights_decisions,
    RightsDecision,
    loaders={
        "allowed_actions": _actions,
        "restrictions": _str_tuple,
        "restricted_to_group_ids": uuid_list,
        "evidence_assertion_ids": uuid_list,
    },
)
PLANS = Mapper(
    t.acquisition_plans,
    AcquisitionPlan,
    loaders={
        "manifestation_ids": uuid_list,
        "route_order": _routes,
        "requested_actions": _actions,
        "credential_names": _str_tuple,
    },
)
ATTEMPTS = Mapper(t.acquisition_attempts, AcquisitionAttempt)
ASSETS = Mapper(t.assets, Asset, renames={"metadata": "metadata_json"})
PARSE_RUNS = Mapper(t.parse_runs, ParseRun)
RENDITIONS = Mapper(t.renditions, Rendition)
SPANS = Mapper(t.evidence_spans, EvidenceSpan, renames={"locator": "locator_json"})
ENTITIES = Mapper(t.entities, Entity)
EXPERIMENTS = Mapper(t.experiments, Experiment)
CLAIMS = Mapper(t.claims, Claim)
REVISIONS = Mapper(
    t.claim_revisions,
    ClaimRevision,
    renames={"value": "value_json"},
    loaders={"evidence_span_ids": uuid_list, "validation_issues": _str_tuple},
)
REVIEWS = Mapper(t.reviews, Review, renames={"before": "before_json", "after": "after_json"})
RELEASES = Mapper(
    t.corpus_releases,
    CorpusRelease,
    loaders={
        "scope_version_ids": uuid_list,
        "embedding_profile_ids": _str_tuple,
        "projection_ready_profiles": _str_tuple,
    },
)
ALIASES = Mapper(t.search_aliases, SearchAlias)
TASKS = Mapper(
    t.tasks,
    Task,
    loaders={"depends_on": uuid_list, "stage": StageName, "state": TaskState},
)
BUDGETS = Mapper(t.rate_budgets, RateBudget)
AUDIT = Mapper(t.audit_events, AuditEventRecord, renames={"metadata": "metadata_json"})
OUTBOX = Mapper(t.outbox_events, OutboxEvent)
SESSIONS = Mapper(t.chat_sessions, ChatSession)
MESSAGES = Mapper(
    t.chat_messages,
    ChatMessage,
    loaders={
        "rewritten_queries": _str_tuple,
        "retrieved_span_ids": uuid_list,
        "retrieved_claim_revision_ids": uuid_list,
        "unsupported_citation_handles": _str_tuple,
        "citations": lambda rows: tuple(rows or ()),
    },
)
FEEDBACK = Mapper(t.feedback, Feedback, loaders={"flagged_citation_handles": _str_tuple})
GRANTS = Mapper(t.resource_grants, GrantSpec)


class _Base:
    def __init__(self, connection: AsyncConnection) -> None:
        self._c = connection

    async def _one(self, mapper: Mapper, *conditions: Any) -> Any | None:
        result = await self._c.execute(select(mapper.table).where(*conditions))
        row = result.mappings().first()
        return mapper.from_row(row) if row else None

    async def _many(self, mapper: Mapper, *conditions: Any, order=None, limit: int | None = None):
        statement = select(mapper.table).where(*conditions)
        if order is not None:
            statement = statement.order_by(order)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self._c.execute(statement)
        return mapper.from_rows(result.mappings().all())

    async def _upsert(self, mapper: Mapper, instance: Any) -> None:
        row, keys = mapper.upsert_values(instance)
        conditions = [mapper.table.c[key] == row[key] for key in keys]
        existing = await self._c.execute(select(mapper.table.c[keys[0]]).where(*conditions))
        if existing.first() is None:
            await self._c.execute(insert(mapper.table).values(**row))
        else:
            await self._c.execute(update(mapper.table).where(*conditions).values(**row))

    @staticmethod
    def _scope(mapper: Mapper, workspace_id: UUID) -> Any:
        return mapper.table.c.workspace_id == workspace_id


class _Users(_Base):
    async def get(self, user_id: UUID) -> User | None:
        return await self._one(USERS, t.users.c.id == user_id)

    async def get_by_email(self, email_normalized: str) -> User | None:
        return await self._one(USERS, t.users.c.email_normalized == email_normalized)

    async def add(self, user: User) -> None:
        await self._upsert(USERS, user)

    async def update(self, user: User) -> None:
        await self._upsert(USERS, user)

    async def list_all(self, *, limit: int = 100, cursor: str | None = None) -> list[User]:
        conditions = [t.users.c.email_normalized > cursor] if cursor else []
        return await self._many(USERS, *conditions, order=t.users.c.email_normalized, limit=limit)


class _Workspaces(_Base):
    async def get(self, workspace_id: UUID) -> Workspace | None:
        return await self._one(WORKSPACES, t.workspaces.c.id == workspace_id)

    async def get_by_slug(self, slug: str) -> Workspace | None:
        return await self._one(WORKSPACES, t.workspaces.c.slug == slug)

    async def add(self, workspace: Workspace) -> None:
        await self._upsert(WORKSPACES, workspace)

    async def list_for_user(self, user_id: UUID) -> list[Workspace]:
        statement = (
            select(t.workspaces)
            .join(t.memberships, t.memberships.c.workspace_id == t.workspaces.c.id)
            .where(t.memberships.c.user_id == user_id)
            .order_by(t.workspaces.c.slug)
        )
        result = await self._c.execute(statement)
        return WORKSPACES.from_rows(result.mappings().all())

    async def list_workspaces(
        self, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[Workspace]:
        statement = select(t.workspaces).order_by(t.workspaces.c.id).limit(limit)
        if after_id is not None:
            statement = statement.where(t.workspaces.c.id > after_id)
        result = await self._c.execute(statement)
        return WORKSPACES.from_rows(result.mappings().all())

    async def get_membership(self, workspace_id: UUID, user_id: UUID) -> Membership | None:
        return await self._one(
            MEMBERSHIPS,
            t.memberships.c.workspace_id == workspace_id,
            t.memberships.c.user_id == user_id,
        )

    async def list_memberships(self, workspace_id: UUID) -> list[Membership]:
        return await self._many(MEMBERSHIPS, self._scope(MEMBERSHIPS, workspace_id))

    async def add_membership(self, membership: Membership) -> None:
        await self._upsert(MEMBERSHIPS, membership)

    async def update_membership(self, membership: Membership) -> None:
        await self._upsert(MEMBERSHIPS, membership)

    async def list_groups(self, workspace_id: UUID, *, user_id: UUID | None = None) -> list[Group]:
        if user_id is None:
            groups = await self._many(GROUPS, self._scope(GROUPS, workspace_id))
        else:
            statement = (
                select(t.groups)
                .join(t.group_memberships, t.group_memberships.c.group_id == t.groups.c.id)
                .where(
                    t.groups.c.workspace_id == workspace_id,
                    t.group_memberships.c.user_id == user_id,
                )
            )
            result = await self._c.execute(statement)
            groups = GROUPS.from_rows(result.mappings().all())
        # Group membership lives in its own table; load it for each group.
        loaded: list[Group] = []
        for group in groups:
            members = await self._c.execute(
                select(t.group_memberships.c.user_id).where(
                    t.group_memberships.c.group_id == group.id
                )
            )
            loaded.append(
                group.model_copy(
                    update={
                        "member_user_ids": frozenset(uuid_or_none(row[0]) for row in members.all())
                    }
                )
            )
        return loaded

    async def add_group(self, group: Group) -> None:
        await self._upsert(GROUPS, group)
        await self._c.execute(
            delete(t.group_memberships).where(t.group_memberships.c.group_id == group.id)
        )
        for user_id in sorted(group.member_user_ids, key=str):
            await self._c.execute(
                insert(t.group_memberships).values(group_id=group.id, user_id=user_id)
            )

    async def update_group(self, group: Group) -> None:
        await self.add_group(group)

    async def add_invitation(self, invitation: Invitation) -> None:
        await self._upsert(INVITATIONS, invitation)

    async def get_invitation_by_digest(self, token_digest: str) -> Invitation | None:
        return await self._one(INVITATIONS, t.invitations.c.token_digest == token_digest)

    async def update_invitation(self, invitation: Invitation) -> None:
        await self._upsert(INVITATIONS, invitation)

    async def list_invitations(self, workspace_id: UUID) -> list[Invitation]:
        return await self._many(INVITATIONS, self._scope(INVITATIONS, workspace_id))


class _Policies(_Base):
    async def list_grants(
        self, workspace_id: UUID, *, subject_ids: Sequence[UUID]
    ) -> list[GrantSpec]:
        return await self._many(
            GRANTS,
            self._scope(GRANTS, workspace_id),
            t.resource_grants.c.subject_id.in_(list(subject_ids)),
        )

    async def add_grant(self, grant: GrantSpec) -> None:
        await self._upsert(GRANTS, grant)

    async def remove_grant(self, workspace_id: UUID, grant_id: UUID) -> None:
        await self._c.execute(
            delete(t.resource_grants).where(
                t.resource_grants.c.workspace_id == workspace_id,
                t.resource_grants.c.id == grant_id,
            )
        )

    async def add_rights_decision(self, decision: RightsDecision) -> None:
        await self._upsert(RIGHTS, decision)

    async def get_rights_decision(
        self, workspace_id: UUID, decision_id: UUID
    ) -> RightsDecision | None:
        return await self._one(
            RIGHTS, self._scope(RIGHTS, workspace_id), t.rights_decisions.c.id == decision_id
        )

    async def find_rights_decision(
        self, workspace_id: UUID, *, subject_id: UUID
    ) -> RightsDecision | None:
        rows = await self._many(
            RIGHTS,
            self._scope(RIGHTS, workspace_id),
            t.rights_decisions.c.subject_id == subject_id,
            order=t.rights_decisions.c.decided_at,
        )
        return rows[-1] if rows else None

    async def list_rights_decisions(
        self, workspace_id: UUID, *, expiring_before: datetime | None = None
    ) -> list[RightsDecision]:
        conditions = [self._scope(RIGHTS, workspace_id)]
        if expiring_before is not None:
            conditions.append(t.rights_decisions.c.review_due_at < expiring_before)
        return await self._many(RIGHTS, *conditions)


class _Configs(_Base):
    async def add_scope_version(self, scope: ScopeVersion) -> None:
        await self._upsert(SCOPES, scope)

    async def get_scope_version(
        self, workspace_id: UUID, scope_version_id: UUID
    ) -> ScopeVersion | None:
        row = await self._c.execute(
            select(t.scope_versions.c.payload).where(
                t.scope_versions.c.workspace_id == workspace_id,
                t.scope_versions.c.id == scope_version_id,
            )
        )
        found = row.first()
        return ScopeVersion.model_validate(found[0]) if found else None

    async def latest_scope_version(self, workspace_id: UUID, scope_id: UUID) -> ScopeVersion | None:
        row = await self._c.execute(
            select(t.scope_versions.c.payload)
            .where(
                t.scope_versions.c.workspace_id == workspace_id,
                t.scope_versions.c.scope_id == scope_id,
            )
            .order_by(t.scope_versions.c.version.desc())
            .limit(1)
        )
        found = row.first()
        return ScopeVersion.model_validate(found[0]) if found else None

    async def list_scope_versions(self, workspace_id: UUID) -> list[ScopeVersion]:
        rows = await self._c.execute(
            select(t.scope_versions.c.payload)
            .where(t.scope_versions.c.workspace_id == workspace_id)
            .order_by(t.scope_versions.c.scope_id, t.scope_versions.c.version)
        )
        return [ScopeVersion.model_validate(row[0]) for row in rows.all()]

    async def add_config_version(self, config: ConfigVersion) -> None:
        await self._upsert(CONFIGS, config)

    async def get_config_version(self, config_version_id: UUID) -> ConfigVersion | None:
        return await self._one(CONFIGS, t.config_versions.c.id == config_version_id)

    async def find_config_version(
        self, workspace_id: UUID | None, *, kind: str, logical_id: str, semantic_version: str
    ) -> ConfigVersion | None:
        return await self._one(
            CONFIGS,
            t.config_versions.c.workspace_id == workspace_id,
            t.config_versions.c.kind == kind,
            t.config_versions.c.logical_id == logical_id,
            t.config_versions.c.semantic_version == semantic_version,
        )

    async def list_config_versions(
        self, workspace_id: UUID | None, *, kind: str | None = None
    ) -> list[ConfigVersion]:
        conditions = [t.config_versions.c.workspace_id == workspace_id]
        if kind is not None:
            conditions.append(t.config_versions.c.kind == kind)
        return await self._many(CONFIGS, *conditions, order=t.config_versions.c.logical_id)


@lru_cache(maxsize=1)
def _object_reference_sources() -> tuple[ObjectReferenceSource, ...]:
    """Columns pointing at ``objects.id``, read from the schema itself.

    A hand-written list would be correct until the next column, and a sweep that
    silently stops covering a column is worse than no sweep: it reports a clean
    bill of health over a shrinking fraction of the graph.
    """
    found: list[ObjectReferenceSource] = []
    for table in t.METADATA.tables.values():
        if "id" not in table.c:
            continue
        for column in table.columns:
            if any(key.column is t.objects.c.id for key in column.foreign_keys):
                found.append(
                    ObjectReferenceSource(
                        table=table.name,
                        column=column.name,
                        required=not column.nullable,
                    )
                )
    return tuple(sorted(found, key=lambda source: (source.table, source.column)))


class _Objects(_Base):
    """The ``objects`` registry: one row per stored payload.

    ``register`` inserts and never updates. Storing the same bytes twice is a
    no-op in the object store, and it has to be a no-op here too, or a repeat of
    an ordinary write would clear a tombstone — the resurrection the audit found
    in the S3 adapter's unconditional record write, reproduced in SQL.
    """

    async def register(self, record: ObjectRecord) -> None:
        existing = await self._c.execute(select(t.objects.c.id).where(t.objects.c.id == record.id))
        if existing.first() is not None:
            return
        row, _keys = OBJECTS.upsert_values(record)
        await self._c.execute(insert(t.objects).values(**row))

    async def get(self, object_id: UUID) -> ObjectRecord | None:
        return await self._one(OBJECTS, t.objects.c.id == object_id)

    async def get_by_digest(self, workspace_id: UUID | None, sha256: str) -> ObjectRecord | None:
        scope = (
            t.objects.c.workspace_id.is_(None)
            if workspace_id is None
            else t.objects.c.workspace_id == workspace_id
        )
        return await self._one(OBJECTS, scope, t.objects.c.sha256 == sha256)

    async def tombstone(self, object_id: UUID, *, reason: str) -> None:
        await self._c.execute(
            update(t.objects)
            .where(t.objects.c.id == object_id)
            .values(tombstoned=True, tombstone_reason=reason)
        )

    async def list_objects(
        self,
        workspace_id: UUID,
        *,
        limit: int = 500,
        after_id: UUID | None = None,
        object_class: str | None = None,
    ) -> list[ObjectRecord]:
        conditions = [self._scope(OBJECTS, workspace_id)]
        if after_id is not None:
            conditions.append(t.objects.c.id > after_id)
        if object_class is not None:
            conditions.append(t.objects.c.object_class == object_class)
        return await self._many(OBJECTS, *conditions, order=t.objects.c.id, limit=limit)

    async def list_all_objects(
        self, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[ObjectRecord]:
        conditions = []
        if after_id is not None:
            conditions.append(t.objects.c.id > after_id)
        return await self._many(OBJECTS, *conditions, order=t.objects.c.id, limit=limit)

    async def count_objects(self, workspace_id: UUID) -> int:
        result = await self._c.execute(
            select(func.count()).select_from(t.objects).where(self._scope(OBJECTS, workspace_id))
        )
        return int(result.scalar_one())

    def reference_sources(self) -> tuple[ObjectReferenceSource, ...]:
        return _object_reference_sources()

    async def list_references(
        self,
        workspace_id: UUID,
        source: ObjectReferenceSource,
        *,
        limit: int = 500,
        after_id: UUID | None = None,
    ) -> list[ObjectReference]:
        table = t.METADATA.tables[source.table]
        column = table.c[source.column]
        conditions = [column.is_not(None)]
        if "workspace_id" in table.c:
            conditions.append(table.c.workspace_id == workspace_id)
        if after_id is not None:
            conditions.append(table.c.id > after_id)
        rows = await self._c.execute(
            select(table.c.id, column).where(*conditions).order_by(table.c.id).limit(limit)
        )
        return [
            ObjectReference(
                source=source,
                row_id=uuid_or_none(row[0]),  # type: ignore[arg-type]
                object_id=uuid_or_none(row[1]),  # type: ignore[arg-type]
            )
            for row in rows.fetchall()
        ]


class _Observations(_Base):
    async def add_run(self, run: DiscoveryRun) -> None:
        await self._upsert(RUNS, run)

    async def update_run(self, run: DiscoveryRun) -> None:
        await self._upsert(RUNS, run)

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> DiscoveryRun | None:
        return await self._one(
            RUNS, self._scope(RUNS, workspace_id), t.discovery_runs.c.id == run_id
        )

    async def list_runs(self, workspace_id: UUID, *, limit: int = 50) -> list[DiscoveryRun]:
        return await self._many(
            RUNS,
            self._scope(RUNS, workspace_id),
            order=t.discovery_runs.c.started_at.desc(),
            limit=limit,
        )

    async def add_request(self, request: SourceRequest) -> None:
        await self._upsert(REQUESTS, request)

    async def update_request(self, request: SourceRequest) -> None:
        await self._upsert(REQUESTS, request)

    async def find_request_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> SourceRequest | None:
        return await self._one(
            REQUESTS,
            self._scope(REQUESTS, workspace_id),
            t.source_requests.c.request_fingerprint == fingerprint,
        )

    async def list_requests(self, workspace_id: UUID, run_id: UUID) -> list[SourceRequest]:
        return await self._many(
            REQUESTS,
            self._scope(REQUESTS, workspace_id),
            t.source_requests.c.discovery_run_id == run_id,
        )

    async def add_observation(self, observation: SourceObservation) -> None:
        await self._upsert(OBSERVATIONS, observation)

    async def list_observations(
        self, workspace_id: UUID, *, request_id: UUID | None = None
    ) -> list[SourceObservation]:
        conditions = [self._scope(OBSERVATIONS, workspace_id)]
        if request_id is not None:
            conditions.append(t.source_observations.c.source_request_id == request_id)
        return await self._many(OBSERVATIONS, *conditions)

    async def add_assertions(self, assertions: Sequence[SourceAssertion]) -> None:
        for assertion in assertions:
            await self._upsert(ASSERTIONS, assertion)

    async def list_assertions(
        self,
        workspace_id: UUID,
        *,
        observation_id: UUID | None = None,
        subject_hint: str | None = None,
        work_id: UUID | None = None,
    ) -> list[SourceAssertion]:
        conditions = [self._scope(ASSERTIONS, workspace_id)]
        if observation_id is not None:
            conditions.append(t.source_assertions.c.source_observation_id == observation_id)
        if subject_hint is not None:
            conditions.append(t.source_assertions.c.subject_hint == subject_hint)
        if work_id is not None:
            conditions.append(t.source_assertions.c.work_id == work_id)
        return await self._many(ASSERTIONS, *conditions)

    async def update_assertion(self, assertion: SourceAssertion) -> None:
        await self._upsert(ASSERTIONS, assertion)

    async def get_cursor(self, workspace_id: UUID, *, lineage_key: str) -> str | None:
        result = await self._c.execute(
            select(t.source_cursors.c.position).where(
                t.source_cursors.c.workspace_id == workspace_id,
                t.source_cursors.c.lineage_key == lineage_key,
            )
        )
        row = result.first()
        return row[0] if row else None

    async def set_cursor(
        self, workspace_id: UUID, *, lineage_key: str, position: str | None, exhausted: bool
    ) -> None:
        from datetime import UTC

        conditions = and_(
            t.source_cursors.c.workspace_id == workspace_id,
            t.source_cursors.c.lineage_key == lineage_key,
        )
        existing = await self._c.execute(select(t.source_cursors.c.position).where(conditions))
        values = {
            "position": position,
            "exhausted": exhausted,
            "updated_at": datetime.now(tz=UTC),
        }
        if existing.first() is None:
            await self._c.execute(
                insert(t.source_cursors).values(
                    workspace_id=workspace_id, lineage_key=lineage_key, **values
                )
            )
        else:
            await self._c.execute(update(t.source_cursors).where(conditions).values(**values))


class _Works(_Base):
    async def add(self, work: Work) -> None:
        await self._upsert(WORKS, work)

    async def update(self, work: Work) -> None:
        await self._upsert(WORKS, work)

    async def get(self, workspace_id: UUID, work_id: UUID) -> Work | None:
        return await self._one(WORKS, self._scope(WORKS, workspace_id), t.works.c.id == work_id)

    async def list_works(
        self,
        workspace_id: UUID,
        *,
        relevance: str | None = None,
        limit: int = 100,
        after_id: UUID | None = None,
    ) -> list[Work]:
        conditions = [self._scope(WORKS, workspace_id)]
        if relevance is not None:
            conditions.append(t.works.c.relevance == relevance)
        if after_id is not None:
            conditions.append(t.works.c.id > after_id)
        # Ordered by id, not by created_at: the cursor has to be total and stable,
        # and two works created in the same transaction share a timestamp.
        return await self._many(WORKS, *conditions, order=t.works.c.id, limit=limit)

    async def list_work_visibility(
        self,
        workspace_id: UUID,
        *,
        relevance: str | None = None,
        limit: int = 500,
        after_id: UUID | None = None,
    ) -> list[WorkVisibilityRow]:
        """One page of works with their rights restrictions, in one query.

        Two round trips per work is what made a corpus-wide visibility filter look
        expensive enough to move after ranking. It is a join.
        """
        conditions = [t.works.c.workspace_id == workspace_id]
        if relevance is not None:
            conditions.append(t.works.c.relevance == relevance)
        if after_id is not None:
            conditions.append(t.works.c.id > after_id)
        page = (
            select(
                t.works.c.id,
                t.works.c.relevance,
                t.works.c.merged_into_work_id,
                t.works.c.identity_status,
            )
            .where(*conditions)
            .order_by(t.works.c.id)
            .limit(limit)
            .subquery()
        )
        statement = (
            select(
                page.c.id,
                page.c.relevance,
                page.c.merged_into_work_id,
                page.c.identity_status,
                t.rights_decisions.c.restricted_to_group_ids,
                t.rights_decisions.c.restrictions,
            )
            .select_from(
                page.outerjoin(
                    t.assets,
                    (t.assets.c.work_id == page.c.id) & (t.assets.c.workspace_id == workspace_id),
                ).outerjoin(
                    t.rights_decisions,
                    (t.rights_decisions.c.id == t.assets.c.rights_decision_id)
                    & (t.rights_decisions.c.workspace_id == workspace_id),
                )
            )
            .order_by(page.c.id)
        )
        result = await self._c.execute(statement)
        collected: dict[UUID, dict[str, Any]] = {}
        for row in result.mappings().all():
            work_id = row["id"]
            entry = collected.setdefault(
                work_id,
                {
                    "relevance": row["relevance"],
                    "is_active": row["merged_into_work_id"] is None
                    and row["identity_status"] != "merged",
                    "groups": [],
                    "sensitivity": "internal",
                    "has_decision": False,
                },
            )
            groups = row["restricted_to_group_ids"]
            if groups is not None:
                entry["has_decision"] = True
                entry["groups"].extend(UUID(str(value)) for value in groups)
            if row["restrictions"]:
                entry["sensitivity"] = "restricted"
        return [
            WorkVisibilityRow(
                work_id=work_id,
                is_active=bool(entry["is_active"]),
                relevance=str(entry["relevance"]),
                restricted_to_groups=tuple(dict.fromkeys(entry["groups"])),
                sensitivity=str(entry["sensitivity"]),
                has_rights_decision=bool(entry["has_decision"]),
            )
            for work_id, entry in sorted(collected.items(), key=lambda pair: str(pair[0]))
        ]

    async def add_identifier(self, identifier: WorkIdentifier) -> None:
        await self._upsert(IDENTIFIERS, identifier)

    async def update_identifier(self, identifier: WorkIdentifier) -> None:
        await self._upsert(IDENTIFIERS, identifier)

    async def list_identifiers(self, workspace_id: UUID, work_id: UUID) -> list[WorkIdentifier]:
        return await self._many(
            IDENTIFIERS,
            self._scope(IDENTIFIERS, workspace_id),
            t.work_identifiers.c.work_id == work_id,
        )

    async def find_by_identifier(
        self, workspace_id: UUID, *, scheme: str, value_normalized: str
    ) -> Work | None:
        statement = (
            select(t.works)
            .join(t.work_identifiers, t.work_identifiers.c.work_id == t.works.c.id)
            .where(
                t.works.c.workspace_id == workspace_id,
                t.work_identifiers.c.scheme == scheme,
                t.work_identifiers.c.value_normalized == value_normalized,
                t.work_identifiers.c.status == "active",
                t.works.c.merged_into_work_id.is_(None),
            )
        )
        result = await self._c.execute(statement)
        row = result.mappings().first()
        return WORKS.from_row(row) if row else None

    async def add_relation(self, relation: WorkRelation) -> None:
        await self._upsert(RELATIONS, relation)

    async def list_relations(self, workspace_id: UUID, work_id: UUID) -> list[WorkRelation]:
        return await self._many(
            RELATIONS,
            self._scope(RELATIONS, workspace_id),
            (t.work_relations.c.from_work_id == work_id)
            | (t.work_relations.c.to_work_id == work_id),
        )

    async def add_decision(self, decision: IdentityDecision) -> None:
        await self._upsert(DECISIONS, decision)

    async def list_decisions(
        self, workspace_id: UUID, *, work_id: UUID | None = None
    ) -> list[IdentityDecision]:
        decisions = await self._many(
            DECISIONS,
            self._scope(DECISIONS, workspace_id),
            order=t.identity_decisions.c.decided_at,
        )
        if work_id is None:
            return decisions
        return [d for d in decisions if work_id in d.subject_work_ids]


class _Assets(_Base):
    async def add_manifestation(self, manifestation: Manifestation) -> None:
        await self._upsert(MANIFESTATIONS, manifestation)

    async def get_manifestation(
        self, workspace_id: UUID, manifestation_id: UUID
    ) -> Manifestation | None:
        return await self._one(
            MANIFESTATIONS,
            self._scope(MANIFESTATIONS, workspace_id),
            t.manifestations.c.id == manifestation_id,
        )

    async def list_manifestations(self, workspace_id: UUID, work_id: UUID) -> list[Manifestation]:
        return await self._many(
            MANIFESTATIONS,
            self._scope(MANIFESTATIONS, workspace_id),
            t.manifestations.c.work_id == work_id,
        )

    async def add_plan(self, plan: AcquisitionPlan) -> None:
        await self._upsert(PLANS, plan)

    async def update_plan(self, plan: AcquisitionPlan) -> None:
        await self._upsert(PLANS, plan)

    async def get_plan(self, workspace_id: UUID, plan_id: UUID) -> AcquisitionPlan | None:
        return await self._one(
            PLANS, self._scope(PLANS, workspace_id), t.acquisition_plans.c.id == plan_id
        )

    async def list_plans(
        self, workspace_id: UUID, *, status: str | None = None
    ) -> list[AcquisitionPlan]:
        conditions = [self._scope(PLANS, workspace_id)]
        if status is not None:
            conditions.append(t.acquisition_plans.c.status == status)
        return await self._many(PLANS, *conditions, order=t.acquisition_plans.c.created_at)

    async def add_attempt(self, attempt: AcquisitionAttempt) -> None:
        await self._upsert(ATTEMPTS, attempt)

    async def list_attempts(
        self, workspace_id: UUID, *, plan_id: UUID | None = None
    ) -> list[AcquisitionAttempt]:
        conditions = [self._scope(ATTEMPTS, workspace_id)]
        if plan_id is not None:
            conditions.append(t.acquisition_attempts.c.acquisition_plan_id == plan_id)
        return await self._many(
            ATTEMPTS, *conditions, order=t.acquisition_attempts.c.attempt_number
        )

    async def add_asset(self, asset: Asset) -> None:
        await self._upsert(ASSETS, asset)

    async def update_asset(self, asset: Asset) -> None:
        await self._upsert(ASSETS, asset)

    async def get_asset(self, workspace_id: UUID, asset_id: UUID) -> Asset | None:
        return await self._one(ASSETS, self._scope(ASSETS, workspace_id), t.assets.c.id == asset_id)

    async def find_asset_by_digest(
        self,
        workspace_id: UUID,
        sha256: str,
        *,
        work_id: UUID | None = None,
        manifestation_id: UUID | None = None,
    ) -> Asset | None:
        conditions = [self._scope(ASSETS, workspace_id), t.assets.c.sha256 == sha256]
        if work_id is not None:
            conditions.append(t.assets.c.work_id == work_id)
        if manifestation_id is not None:
            conditions.append(t.assets.c.manifestation_id == manifestation_id)
        return await self._one(ASSETS, *conditions)

    async def list_assets(self, workspace_id: UUID, *, work_id: UUID | None = None) -> list[Asset]:
        conditions = [self._scope(ASSETS, workspace_id)]
        if work_id is not None:
            conditions.append(t.assets.c.work_id == work_id)
        return await self._many(ASSETS, *conditions)


class _Renditions(_Base):
    async def add_parse_run(self, run: ParseRun) -> None:
        await self._upsert(PARSE_RUNS, run)

    async def update_parse_run(self, run: ParseRun) -> None:
        await self._upsert(PARSE_RUNS, run)

    async def get_parse_run(self, workspace_id: UUID, run_id: UUID) -> ParseRun | None:
        return await self._one(
            PARSE_RUNS, self._scope(PARSE_RUNS, workspace_id), t.parse_runs.c.id == run_id
        )

    async def find_parse_run_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> ParseRun | None:
        return await self._one(
            PARSE_RUNS,
            self._scope(PARSE_RUNS, workspace_id),
            t.parse_runs.c.run_fingerprint == fingerprint,
        )

    async def add_rendition(self, rendition: Rendition) -> None:
        await self._upsert(RENDITIONS, rendition)

    async def update_rendition(self, rendition: Rendition) -> None:
        await self._upsert(RENDITIONS, rendition)

    async def get_rendition(self, workspace_id: UUID, rendition_id: UUID) -> Rendition | None:
        return await self._one(
            RENDITIONS, self._scope(RENDITIONS, workspace_id), t.renditions.c.id == rendition_id
        )

    async def list_renditions(
        self, workspace_id: UUID, *, asset_id: UUID | None = None, work_id: UUID | None = None
    ) -> list[Rendition]:
        conditions = [self._scope(RENDITIONS, workspace_id)]
        if asset_id is not None:
            conditions.append(t.renditions.c.asset_id == asset_id)
        if work_id is not None:
            conditions.append(t.renditions.c.work_id == work_id)
        return await self._many(RENDITIONS, *conditions)

    async def add_spans(self, spans: Sequence[EvidenceSpan]) -> None:
        for span in spans:
            await self._upsert(SPANS, span)

    async def get_span(self, workspace_id: UUID, span_id: UUID) -> EvidenceSpan | None:
        return await self._one(
            SPANS, self._scope(SPANS, workspace_id), t.evidence_spans.c.id == span_id
        )

    async def list_spans(self, workspace_id: UUID, rendition_id: UUID) -> list[EvidenceSpan]:
        return await self._many(
            SPANS,
            self._scope(SPANS, workspace_id),
            t.evidence_spans.c.rendition_id == rendition_id,
        )


class _Claims(_Base):
    async def add_entity(self, entity: Entity) -> None:
        await self._upsert(ENTITIES, entity)

    async def find_entity(
        self, workspace_id: UUID, *, entity_type: str, normalised_value: str
    ) -> Entity | None:
        return await self._one(
            ENTITIES,
            self._scope(ENTITIES, workspace_id),
            t.entities.c.entity_type == entity_type,
            t.entities.c.normalised_value == normalised_value,
        )

    async def add_experiment(self, experiment: Experiment) -> None:
        await self._upsert(EXPERIMENTS, experiment)

    async def update_experiment(self, experiment: Experiment) -> None:
        await self._upsert(EXPERIMENTS, experiment)

    async def list_experiments(self, workspace_id: UUID, work_id: UUID) -> list[Experiment]:
        return await self._many(
            EXPERIMENTS,
            self._scope(EXPERIMENTS, workspace_id),
            t.experiments.c.work_id == work_id,
        )

    async def add_claim(self, claim: Claim) -> None:
        await self._upsert(CLAIMS, claim)

    async def update_claim(self, claim: Claim) -> None:
        await self._upsert(CLAIMS, claim)

    async def get_claim(self, workspace_id: UUID, claim_id: UUID) -> Claim | None:
        return await self._one(CLAIMS, self._scope(CLAIMS, workspace_id), t.claims.c.id == claim_id)

    async def list_claims(
        self,
        workspace_id: UUID,
        *,
        work_id: UUID | None = None,
        review_status: str | None = None,
        limit: int = 200,
        after_id: UUID | None = None,
    ) -> list[Claim]:
        conditions = [self._scope(CLAIMS, workspace_id)]
        if work_id is not None:
            conditions.append(t.claims.c.work_id == work_id)
        if after_id is not None:
            conditions.append(t.claims.c.id > after_id)
        if review_status is not None:
            statement = (
                select(t.claims)
                .join(
                    t.claim_revisions,
                    t.claim_revisions.c.id == t.claims.c.current_revision_id,
                )
                .where(*conditions, t.claim_revisions.c.review_status == review_status)
                .order_by(t.claims.c.id)
                .limit(limit)
            )
            result = await self._c.execute(statement)
            return CLAIMS.from_rows(result.mappings().all())
        # Ordered by id so the keyset cursor is total; created_at is not unique.
        return await self._many(CLAIMS, *conditions, order=t.claims.c.id, limit=limit)

    async def add_revision(self, revision: ClaimRevision) -> None:
        await self._upsert(REVISIONS, revision)

    async def update_revision(self, revision: ClaimRevision) -> None:
        await self._upsert(REVISIONS, revision)

    async def get_revision(self, workspace_id: UUID, revision_id: UUID) -> ClaimRevision | None:
        return await self._one(
            REVISIONS, self._scope(REVISIONS, workspace_id), t.claim_revisions.c.id == revision_id
        )

    async def list_revisions(self, workspace_id: UUID, claim_id: UUID) -> list[ClaimRevision]:
        return await self._many(
            REVISIONS,
            self._scope(REVISIONS, workspace_id),
            t.claim_revisions.c.claim_id == claim_id,
            order=t.claim_revisions.c.revision_number,
        )

    async def find_revision_by_fingerprint(
        self, workspace_id: UUID, fingerprint: str
    ) -> ClaimRevision | None:
        return await self._one(
            REVISIONS,
            self._scope(REVISIONS, workspace_id),
            t.claim_revisions.c.extractor_fingerprint == fingerprint,
        )

    async def add_review(self, review: Review) -> None:
        await self._upsert(REVIEWS, review)

    async def list_reviews(
        self, workspace_id: UUID, *, subject_id: UUID | None = None
    ) -> list[Review]:
        conditions = [self._scope(REVIEWS, workspace_id)]
        if subject_id is not None:
            conditions.append(t.reviews.c.subject_id == subject_id)
        return await self._many(REVIEWS, *conditions, order=t.reviews.c.reviewed_at)


class _Releases(_Base):
    async def add(self, release: CorpusRelease) -> None:
        await self._upsert(RELEASES, release)

    async def update(self, release: CorpusRelease) -> None:
        await self._upsert(RELEASES, release)

    async def get(self, workspace_id: UUID, release_id: UUID) -> CorpusRelease | None:
        return await self._one(
            RELEASES, self._scope(RELEASES, workspace_id), t.corpus_releases.c.id == release_id
        )

    async def list_releases(self, workspace_id: UUID, *, limit: int = 50) -> list[CorpusRelease]:
        return await self._many(
            RELEASES,
            self._scope(RELEASES, workspace_id),
            order=t.corpus_releases.c.created_at.desc(),
            limit=limit,
        )

    async def list_all_releases(
        self, workspace_id: UUID, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[CorpusRelease]:
        conditions = [self._scope(RELEASES, workspace_id)]
        if after_id is not None:
            conditions.append(t.corpus_releases.c.id > after_id)
        return await self._many(RELEASES, *conditions, order=t.corpus_releases.c.id, limit=limit)

    async def add_items(self, items: Sequence[ReleaseItem]) -> None:
        for item in items:
            record = item.record
            values = {
                "id": item.id,
                "workspace_id": item.workspace_id,
                "release_id": item.release_id,
                "work_id": record.work_id,
                "manifestation_id": record.manifestation_id,
                "asset_id": record.asset_id,
                "rendition_id": record.rendition_id,
                "rendition_digest": record.rendition_digest,
                "claim_revision_ids": [str(i) for i in record.claim_revision_ids],
                "access_policy_snapshot_id": record.access_policy_snapshot_id,
                "inclusion_reason": record.inclusion_reason,
                "source_discovery_run_ids": [str(i) for i in record.source_discovery_run_ids],
                "content_digest": record.content_digest(),
            }
            await self._c.execute(insert(t.release_items).values(**values))

    async def list_items(self, workspace_id: UUID, release_id: UUID) -> list[ReleaseItem]:
        result = await self._c.execute(
            select(t.release_items)
            .where(
                t.release_items.c.workspace_id == workspace_id,
                t.release_items.c.release_id == release_id,
            )
            .order_by(t.release_items.c.work_id)
        )
        items: list[ReleaseItem] = []
        for row in result.mappings().all():
            items.append(
                ReleaseItem(
                    id=row["id"],
                    workspace_id=row["workspace_id"],
                    release_id=row["release_id"],
                    record=ReleaseItemRecord(
                        work_id=row["work_id"],
                        manifestation_id=row["manifestation_id"],
                        asset_id=row["asset_id"],
                        rendition_id=row["rendition_id"],
                        rendition_digest=row["rendition_digest"],
                        claim_revision_ids=uuid_list(row["claim_revision_ids"]),
                        access_policy_snapshot_id=row["access_policy_snapshot_id"],
                        inclusion_reason=row["inclusion_reason"],
                        source_discovery_run_ids=uuid_list(row["source_discovery_run_ids"]),
                    ),
                )
            )
        return items

    async def get_alias(self, workspace_id: UUID, alias: str) -> SearchAlias | None:
        return await self._one(
            ALIASES,
            t.search_aliases.c.workspace_id == workspace_id,
            t.search_aliases.c.alias == alias,
        )

    async def upsert_alias(self, alias: SearchAlias) -> None:
        conditions = and_(
            t.search_aliases.c.workspace_id == alias.workspace_id,
            t.search_aliases.c.alias == alias.alias,
        )
        row = ALIASES.to_row(alias)
        existing = await self._c.execute(select(t.search_aliases.c.alias).where(conditions))
        if existing.first() is None:
            await self._c.execute(insert(t.search_aliases).values(**row))
        else:
            await self._c.execute(update(t.search_aliases).where(conditions).values(**row))

    async def list_aliases(self, workspace_id: UUID) -> list[SearchAlias]:
        return await self._many(ALIASES, t.search_aliases.c.workspace_id == workspace_id)


class _Tasks(_Base):
    def __init__(self, connection: AsyncConnection, *, dialect: str) -> None:
        super().__init__(connection)
        self._dialect = dialect

    async def add(self, task: Task) -> None:
        await self._upsert(TASKS, task)

    async def update(self, task: Task) -> None:
        await self._upsert(TASKS, task)

    async def get(self, workspace_id: UUID, task_id: UUID) -> Task | None:
        return await self._one(TASKS, self._scope(TASKS, workspace_id), t.tasks.c.id == task_id)

    async def find_by_fingerprint(self, workspace_id: UUID, fingerprint: str) -> Task | None:
        return await self._one(
            TASKS, self._scope(TASKS, workspace_id), t.tasks.c.input_fingerprint == fingerprint
        )

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
        eligible_stages = [
            stage.value
            for stage in (stages or list(StageName))
            if stage.worker_class == worker_class
        ]
        if not eligible_stages:
            return None
        caps = stage_concurrency or {}
        for stage_value in eligible_stages:
            stage = StageName(stage_value)
            cap = caps.get(stage, 1)
            in_flight = await self._c.execute(
                select(func.count())
                .select_from(t.tasks)
                .where(
                    t.tasks.c.stage == stage_value,
                    t.tasks.c.state == TaskState.LEASED.value,
                    t.tasks.c.lease_expires_at > now,
                )
            )
            if (in_flight.scalar() or 0) >= cap:
                continue
            statement = (
                select(t.tasks)
                .where(
                    t.tasks.c.stage == stage_value,
                    (
                        (t.tasks.c.state == TaskState.READY.value)
                        & (t.tasks.c.not_before.is_(None) | (t.tasks.c.not_before <= now))
                    )
                    | (
                        (t.tasks.c.state == TaskState.LEASED.value)
                        & (t.tasks.c.lease_expires_at <= now)
                    ),
                )
                .order_by(t.tasks.c.priority, t.tasks.c.created_at)
                .limit(1)
            )
            if self._dialect == "postgresql":
                # Several workers claim concurrently without a broker.
                statement = statement.with_for_update(skip_locked=True)
            result = await self._c.execute(statement)
            row = result.mappings().first()
            if row is None:
                continue
            task = TASKS.from_row(row)
            task.claim(owner=owner, now=now, lease_seconds=lease_seconds)
            await self._upsert(TASKS, task)
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
        conditions = [self._scope(TASKS, workspace_id)]
        if state is not None:
            conditions.append(t.tasks.c.state == state)
        if stage is not None:
            conditions.append(t.tasks.c.stage == stage)
        return await self._many(TASKS, *conditions, order=t.tasks.c.created_at, limit=limit)

    async def count_by_state(self, workspace_id: UUID) -> dict[str, int]:
        result = await self._c.execute(
            select(t.tasks.c.state, func.count())
            .where(t.tasks.c.workspace_id == workspace_id)
            .group_by(t.tasks.c.state)
        )
        return {row[0]: row[1] for row in sorted(result.all())}

    async def release_dependencies(self, workspace_id: UUID, completed_task_id: UUID) -> list[Task]:
        blocked = await self._many(
            TASKS, self._scope(TASKS, workspace_id), t.tasks.c.state == TaskState.BLOCKED.value
        )
        unblocked: list[Task] = []
        for task in blocked:
            if completed_task_id not in task.depends_on:
                continue
            remaining = []
            for dependency in task.depends_on:
                if dependency == completed_task_id:
                    continue
                other = await self.get(workspace_id, dependency)
                if other is None or other.state is not TaskState.SUCCEEDED:
                    remaining.append(dependency)
            if not remaining:
                task.release_dependency_block()
                unblocked.append(task)
        return unblocked

    async def get_rate_budget(self, key: str, *, for_update: bool = False) -> RateBudget | None:
        """Read a budget, optionally locking the row.

        Without the lock two workers read the same token count, both decide they
        may proceed, and the source sees double the configured rate — which is how
        a shared budget becomes a suggestion.
        """
        statement = select(t.rate_budgets).where(t.rate_budgets.c.key == key)
        if for_update and self._c.dialect.name == "postgresql":
            statement = statement.with_for_update()
        result = await self._c.execute(statement)
        row = result.mappings().first()
        return BUDGETS.from_row(row) if row else None

    async def upsert_rate_budget(self, budget: RateBudget) -> None:
        await self._upsert(BUDGETS, budget)


class _Audit(_Base):
    async def append(self, event: AuditEventRecord) -> None:
        await self._c.execute(insert(t.audit_events).values(**AUDIT.to_row(event)))

    async def latest_hash(self, workspace_id: UUID | None) -> str | None:
        condition = (
            t.audit_events.c.workspace_id.is_(None)
            if workspace_id is None
            else t.audit_events.c.workspace_id == workspace_id
        )
        result = await self._c.execute(
            select(t.audit_events.c.integrity_hash)
            .where(condition)
            .order_by(t.audit_events.c.occurred_at.desc(), t.audit_events.c.id.desc())
            .limit(1)
        )
        row = result.first()
        return row[0] if row else None

    async def list_events(
        self,
        workspace_id: UUID | None,
        *,
        subject_id: UUID | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[AuditEventRecord]:
        conditions = [
            t.audit_events.c.workspace_id.is_(None)
            if workspace_id is None
            else t.audit_events.c.workspace_id == workspace_id
        ]
        if subject_id is not None:
            conditions.append(t.audit_events.c.subject_id == subject_id)
        if action is not None:
            conditions.append(t.audit_events.c.action == action)
        result = await self._c.execute(
            select(t.audit_events)
            .where(*conditions)
            .order_by(t.audit_events.c.occurred_at, t.audit_events.c.id)
            .limit(limit)
        )
        return AUDIT.from_rows(result.mappings().all())

    async def add_outbox(self, event: OutboxEvent) -> None:
        await self._c.execute(insert(t.outbox_events).values(**OUTBOX.to_row(event)))

    async def list_unpublished_outbox(self, *, limit: int = 100) -> list[OutboxEvent]:
        return await self._many(OUTBOX, t.outbox_events.c.published_at.is_(None), limit=limit)

    async def mark_outbox_published(self, event_id: UUID, *, at: datetime) -> None:
        await self._c.execute(
            update(t.outbox_events).where(t.outbox_events.c.id == event_id).values(published_at=at)
        )


class _Chat(_Base):
    async def add_session(self, session: ChatSession) -> None:
        await self._upsert(SESSIONS, session)

    async def update_session(self, session: ChatSession) -> None:
        await self._upsert(SESSIONS, session)

    async def get_session(self, workspace_id: UUID, session_id: UUID) -> ChatSession | None:
        return await self._one(
            SESSIONS, self._scope(SESSIONS, workspace_id), t.chat_sessions.c.id == session_id
        )

    async def list_sessions(
        self, workspace_id: UUID, user_id: UUID, *, limit: int = 50
    ) -> list[ChatSession]:
        return await self._many(
            SESSIONS,
            self._scope(SESSIONS, workspace_id),
            t.chat_sessions.c.user_id == user_id,
            order=t.chat_sessions.c.updated_at.desc(),
            limit=limit,
        )

    async def list_workspace_sessions(
        self, workspace_id: UUID, *, limit: int = 1000
    ) -> list[ChatSession]:
        return await self._many(SESSIONS, self._scope(SESSIONS, workspace_id), limit=limit)

    async def add_message(self, message: ChatMessage) -> None:
        await self._upsert(MESSAGES, message)

    async def list_messages(self, workspace_id: UUID, session_id: UUID) -> list[ChatMessage]:
        return await self._many(
            MESSAGES,
            self._scope(MESSAGES, workspace_id),
            t.chat_messages.c.session_id == session_id,
            order=t.chat_messages.c.created_at,
        )

    async def get_message(self, workspace_id: UUID, message_id: UUID) -> ChatMessage | None:
        return await self._one(
            MESSAGES, self._scope(MESSAGES, workspace_id), t.chat_messages.c.id == message_id
        )

    async def add_feedback(self, feedback_row: Feedback) -> None:
        await self._upsert(FEEDBACK, feedback_row)

    async def list_feedback(self, workspace_id: UUID, *, limit: int = 100) -> list[Feedback]:
        return await self._many(FEEDBACK, self._scope(FEEDBACK, workspace_id), limit=limit)


class _Evaluations(_Base):
    async def add_run(self, run: dict[str, Any]) -> None:
        await self._c.execute(
            insert(t.evaluation_runs).values(
                id=uuid_or_none(run["id"]),
                workspace_id=uuid_or_none(run["workspace_id"]),
                benchmark=run["benchmark"],
                benchmark_checksum=run["benchmark_checksum"],
                release_id=uuid_or_none(run.get("release_id")),
                metrics=run.get("metrics", {}),
                gates=run.get("gates", {}),
                failures=[str(item) for item in run.get("failures", [])],
                per_case=run.get("per_case", []),
                created_at=datetime.now(tz=_utc()),
            )
        )

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> dict[str, Any] | None:
        result = await self._c.execute(
            select(t.evaluation_runs).where(
                t.evaluation_runs.c.workspace_id == workspace_id,
                t.evaluation_runs.c.id == run_id,
            )
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_runs(
        self, workspace_id: UUID, *, benchmark_id: str | None = None
    ) -> list[dict[str, Any]]:
        conditions = [t.evaluation_runs.c.workspace_id == workspace_id]
        if benchmark_id is not None:
            conditions.append(t.evaluation_runs.c.benchmark.like(f"{benchmark_id}%"))
        result = await self._c.execute(select(t.evaluation_runs).where(*conditions))
        return [dict(row) for row in result.mappings().all()]


def _utc():
    from datetime import UTC

    return UTC


class SqlUnitOfWork:
    """One transaction. Committing is explicit; leaving without it rolls back."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: AsyncConnection | None = None
        self._transaction: Any = None

    async def __aenter__(self) -> SqlUnitOfWork:
        self._connection = await self._engine.connect()
        self._transaction = await self._connection.begin()
        dialect = self._engine.dialect.name
        connection = self._connection
        self.users = _Users(connection)
        self.workspaces = _Workspaces(connection)
        self.objects = _Objects(connection)
        self.policies = _Policies(connection)
        self.configs = _Configs(connection)
        self.observations = _Observations(connection)
        self.works = _Works(connection)
        self.assets = _Assets(connection)
        self.renditions = _Renditions(connection)
        self.claims = _Claims(connection)
        self.releases = _Releases(connection)
        self.publications = _SqlPublications(connection)
        self.tasks = _Tasks(connection, dialect=dialect)
        self.audit = _Audit(connection)
        self.chat = _Chat(connection)
        self.evaluations = _Evaluations(connection)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._connection is not None
        try:
            if exc_type is not None and self._transaction.is_active:
                await self._transaction.rollback()
            elif self._transaction.is_active:
                # Leaving without committing is a rollback, not a silent write.
                await self._transaction.rollback()
        finally:
            await self._connection.close()
            self._connection = None

    async def commit(self) -> None:
        if self._transaction is not None and self._transaction.is_active:
            await self._transaction.commit()

    async def rollback(self) -> None:
        if self._transaction is not None and self._transaction.is_active:
            await self._transaction.rollback()


class _SqlPublications:
    """The publication outbox over SQL.

    The unique constraint on (library_id, snapshot_id) does the work that
    matters: publishing one snapshot twice is refused by the database rather
    than by the code that already got it wrong once.
    """

    def __init__(self, connection) -> None:
        self._c = connection

    async def add(self, record: PublicationRecord) -> None:
        await self._c.execute(t.archive_publications.insert().values(**_publication_row(record)))

    async def update(self, record: PublicationRecord) -> None:
        await self._c.execute(
            t.archive_publications.update()
            .where(t.archive_publications.c.id == record.id)
            .values(**_publication_row(record))
        )

    async def get(self, workspace_id: UUID, record_id: UUID) -> PublicationRecord | None:
        result = await self._c.execute(
            select(t.archive_publications).where(
                t.archive_publications.c.id == record_id,
                t.archive_publications.c.workspace_id == workspace_id,
            )
        )
        row = result.mappings().first()
        return _publication_from_row(row) if row else None

    async def find_by_snapshot(
        self, library_id: UUID, snapshot_id: UUID
    ) -> PublicationRecord | None:
        result = await self._c.execute(
            select(t.archive_publications).where(
                t.archive_publications.c.library_id == library_id,
                t.archive_publications.c.snapshot_id == snapshot_id,
            )
        )
        row = result.mappings().first()
        return _publication_from_row(row) if row else None

    async def list_open(self, library_id: UUID) -> list[PublicationRecord]:
        terminal = [PublicationState.ACKNOWLEDGED.value, PublicationState.FAILED.value]
        result = await self._c.execute(
            select(t.archive_publications)
            .where(
                t.archive_publications.c.library_id == library_id,
                t.archive_publications.c.state.notin_(terminal),
            )
            .order_by(t.archive_publications.c.created_at, t.archive_publications.c.id)
        )
        return [_publication_from_row(row) for row in result.mappings().all()]


def _publication_row(record: PublicationRecord) -> dict:
    return {
        "id": record.id,
        "workspace_id": record.workspace_id,
        "library_id": record.library_id,
        "snapshot_id": record.snapshot_id,
        "snapshot_manifest_digest": record.snapshot_manifest_digest,
        "state": record.state.value,
        "writer_epoch": record.writer_epoch,
        "commit_sequence": record.commit_sequence,
        "expected_head_version": record.expected_head_version,
        "committed_head_version": record.committed_head_version,
        "detail": record.detail,
        "updated_at": record.updated_at,
    }


def _publication_from_row(row) -> PublicationRecord:
    return PublicationRecord(
        id=row["id"],
        workspace_id=row["workspace_id"],
        library_id=row["library_id"],
        snapshot_id=row["snapshot_id"],
        snapshot_manifest_digest=row["snapshot_manifest_digest"],
        state=PublicationState(row["state"]),
        writer_epoch=row["writer_epoch"],
        commit_sequence=row["commit_sequence"],
        expected_head_version=row["expected_head_version"],
        committed_head_version=row["committed_head_version"],
        detail=row["detail"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def sql_uow_factory(engine: AsyncEngine):
    def factory() -> SqlUnitOfWork:
        return SqlUnitOfWork(engine)

    return factory


def _lease_window(now: datetime, seconds: int) -> datetime:
    return now + timedelta(seconds=seconds)
