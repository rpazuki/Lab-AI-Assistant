"""SQL repositories and the unit of work.

Every method takes ``workspace_id`` and filters on it. The task claim uses
``FOR UPDATE SKIP LOCKED`` on PostgreSQL, which is what makes several workers safe
without a broker; SQLite falls back to a plain read, which is fine because the
offline suite runs one worker at a time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import String, and_, cast, delete, func, insert, literal_column, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

from lab_assistant.adapters.db import tables as t
from lab_assistant.adapters.db.mapper import Mapper, uuid_list, uuid_or_none
from lab_assistant.application.ports import (
    ObjectReference,
    ObjectReferenceSource,
    WorkVisibilityRow,
)
from lab_assistant.contracts.archive import StageResultEnvelope
from lab_assistant.contracts.archive_import import ImportRecord, ImportState
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
from lab_assistant.domain.chat import ChatMessage, ChatSession, Feedback, MessageRole, UsageTotals
from lab_assistant.domain.claims import Claim, ClaimRevision, Entity, Experiment, Review
from lab_assistant.domain.identity import (
    IdentityDecision,
    IdentityDecisionType,
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
from lab_assistant.domain.review import (
    CandidateFilter,
    DuplicateCandidate,
    ReviewStep,
    WorkSummary,
)
from lab_assistant.domain.scope import ConfigVersion, ScopeVersion
from lab_assistant.domain.states import ReviewState, TaskState
from lab_assistant.domain.tasks import RateBudget, Task
from lab_assistant.domain.workspace import Group, Invitation, Membership, User, Workspace

#: Where a row without a timestamp sorts in a newest-first page: last.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class utc_text(FunctionElement):
    """A timestamp as ``YYYY-MM-DD HH:MM:SS`` in UTC: how the pages show and search a time."""

    type = String()
    inherit_cache = True


@compiles(utc_text, "postgresql")
def _utc_text_postgres(element: Any, compiler: Any, **kw: Any) -> str:
    column = compiler.process(element.clauses, **kw)
    return f"to_char(({column}) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"


@compiles(utc_text)
def _utc_text_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    column = compiler.process(element.clauses, **kw)
    return f"strftime('%Y-%m-%d %H:%M:%S', {column})"


def _all_words(parts: Sequence[Any], words: Sequence[str]) -> list[Any]:
    """Conditions that every word appears, case-insensitively, in the joined parts."""
    joined = func.coalesce(parts[0], "")
    for part in parts[1:]:
        joined = joined + " " + func.coalesce(part, "")
    text = func.lower(joined)
    return [text.contains(word.lower(), autoescape=True) for word in words]


#: A claim whose current revision is in one of these has left the review queue.
APPROVED_REVIEW_STATES = tuple(state.value for state in ReviewState if state.is_approved)

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
SUMMARIES = Mapper(t.work_summaries, WorkSummary)
DUPLICATES = Mapper(t.duplicate_candidates, DuplicateCandidate)
STEPS = Mapper(
    t.review_steps,
    ReviewStep,
    renames={"rule": "rule_json", "counts": "counts_json"},
    loaders={"work_ids": uuid_list},
)
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


def _chunks(items: list[Any], size: int = 500) -> list[list[Any]]:
    """IN lists a bounded size: SQLite caps bound parameters per statement."""
    return [items[start : start + size] for start in range(0, len(items), size)] or [[]]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains(text: str) -> str:
    """A LIKE pattern matching ``text`` anywhere, with its own % and _ taken literally."""
    return f"%{_escape(text)}%"


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

    async def _newest_page(
        self,
        mapper: Mapper,
        at: Any,
        *conditions: Any,
        limit: int,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[Any]:
        """One page newest first, by ``(at, id)``; ``before`` is the last row seen.

        ``at`` is coalesced to the epoch, so a row without a time still has a
        place in the order and is reached by the cursor like any other.
        """
        key = func.coalesce(at, EPOCH)
        ident = mapper.table.c.id
        if before is not None:
            moment, last = before
            conditions = (*conditions, (key < moment) | ((key == moment) & (ident < last)))
        result = await self._c.execute(
            select(mapper.table).where(*conditions).order_by(key.desc(), ident.desc()).limit(limit)
        )
        return mapper.from_rows(result.mappings().all())

    async def _count(self, table: Any, *conditions: Any) -> int:
        result = await self._c.execute(select(func.count()).select_from(table).where(*conditions))
        return int(result.scalar() or 0)

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

    async def delete_invitation(self, workspace_id: UUID, invitation_id: UUID) -> None:
        await self._c.execute(
            delete(t.invitations).where(
                t.invitations.c.workspace_id == workspace_id,
                t.invitations.c.id == invitation_id,
            )
        )


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

    async def delete_scope_version(self, workspace_id: UUID, scope_version_id: UUID) -> None:
        await self._c.execute(
            delete(t.scope_versions).where(
                t.scope_versions.c.workspace_id == workspace_id,
                t.scope_versions.c.id == scope_version_id,
            )
        )

    async def add_config_version(self, config: ConfigVersion) -> None:
        await self._upsert(CONFIGS, config)

    async def delete_config_version(self, workspace_id: UUID, config_version_id: UUID) -> None:
        await self._c.execute(
            delete(t.config_versions).where(
                t.config_versions.c.workspace_id == workspace_id,
                t.config_versions.c.id == config_version_id,
            )
        )

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

    async def list_runs(
        self,
        workspace_id: UUID,
        *,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[DiscoveryRun]:
        return await self._newest_page(
            RUNS,
            t.discovery_runs.c.started_at,
            self._scope(RUNS, workspace_id),
            limit=limit,
            before=before,
        )

    async def count_runs(self, workspace_id: UUID) -> int:
        return await self._count(t.discovery_runs, self._scope(RUNS, workspace_id))

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

    async def assign_assertions(
        self, workspace_id: UUID, assertion_ids: Sequence[UUID], work_id: UUID
    ) -> None:
        if not assertion_ids:
            return
        await self._c.execute(
            update(t.source_assertions)
            .where(
                self._scope(ASSERTIONS, workspace_id),
                t.source_assertions.c.id.in_(list(assertion_ids)),
            )
            .values(work_id=work_id)
        )

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

    async def delete_run(self, workspace_id: UUID, run_id: UUID) -> None:
        # Child rows explicitly, deepest first, rather than leaning on ON DELETE
        # CASCADE: SQLite enforces cascades only with a per-connection pragma.
        requests = select(t.source_requests.c.id).where(
            t.source_requests.c.workspace_id == workspace_id,
            t.source_requests.c.discovery_run_id == run_id,
        )
        observations = select(t.source_observations.c.id).where(
            t.source_observations.c.workspace_id == workspace_id,
            t.source_observations.c.source_request_id.in_(requests),
        )
        await self._c.execute(
            delete(t.source_assertions).where(
                t.source_assertions.c.workspace_id == workspace_id,
                t.source_assertions.c.source_observation_id.in_(observations),
            )
        )
        await self._c.execute(
            delete(t.source_observations).where(
                t.source_observations.c.workspace_id == workspace_id,
                t.source_observations.c.source_request_id.in_(requests),
            )
        )
        await self._c.execute(
            delete(t.source_requests).where(
                t.source_requests.c.workspace_id == workspace_id,
                t.source_requests.c.discovery_run_id == run_id,
            )
        )
        await self._c.execute(
            delete(t.discovery_runs).where(
                t.discovery_runs.c.workspace_id == workspace_id,
                t.discovery_runs.c.id == run_id,
            )
        )


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
        identity_status: str | None = None,
    ) -> list[Work]:
        conditions = self._work_filters(workspace_id, relevance, identity_status)
        if after_id is not None:
            conditions.append(t.works.c.id > after_id)
        # Ordered by id, not by created_at: the cursor has to be total and stable,
        # and two works created in the same transaction share a timestamp.
        return await self._many(WORKS, *conditions, order=t.works.c.id, limit=limit)

    def _work_filters(
        self, workspace_id: UUID, relevance: str | None, identity_status: str | None
    ) -> list[Any]:
        conditions = [self._scope(WORKS, workspace_id)]
        if relevance is not None:
            conditions.append(t.works.c.relevance == relevance)
        if identity_status is not None:
            conditions.append(t.works.c.identity_status == identity_status)
        return conditions

    async def count_works(
        self,
        workspace_id: UUID,
        *,
        relevance: str | None = None,
        identity_status: str | None = None,
    ) -> int:
        result = await self._c.execute(
            select(func.count())
            .select_from(t.works)
            .where(*self._work_filters(workspace_id, relevance, identity_status))
        )
        return int(result.scalar() or 0)

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
                t.rights_decisions.c.status.label("rights_status"),
                t.rights_decisions.c.review_due_at,
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
                    "states": [],
                    "due": None,
                },
            )
            if row["rights_status"] is not None:
                entry["states"].append(row["rights_status"])
            if row["review_due_at"] is not None:
                entry["due"] = (
                    row["review_due_at"]
                    if entry["due"] is None
                    else min(entry["due"], row["review_due_at"])
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
                rights_states=tuple(dict.fromkeys(entry["states"])),
                earliest_review_due=entry["due"],
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

    async def list_all_identifiers(
        self, workspace_id: UUID, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[WorkIdentifier]:
        conditions = [self._scope(IDENTIFIERS, workspace_id)]
        if after_id is not None:
            conditions.append(t.work_identifiers.c.id > after_id)
        return await self._many(
            IDENTIFIERS, *conditions, order=t.work_identifiers.c.id, limit=limit
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
                # A constant, not a bound parameter: the unique index on active
                # identifiers is partial (``WHERE status = 'active'``), and a
                # prepared statement's generic plan cannot prove a parameter
                # matches that predicate, so it read the whole table instead.
                t.work_identifiers.c.status == literal_column("'active'"),
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

    # ── review by rule ──
    async def upsert_summaries(self, summaries: Sequence[WorkSummary]) -> None:
        if not summaries:
            return
        ids = [summary.work_id for summary in summaries]
        await self._c.execute(delete(t.work_summaries).where(t.work_summaries.c.work_id.in_(ids)))
        await self._c.execute(
            insert(t.work_summaries),
            [SUMMARIES.upsert_values(summary)[0] for summary in summaries],
        )

    async def get_summaries(
        self, workspace_id: UUID, work_ids: Sequence[UUID]
    ) -> dict[UUID, WorkSummary]:
        out: dict[UUID, WorkSummary] = {}
        for chunk in _chunks(list(work_ids)):
            for summary in await self._many(
                SUMMARIES,
                self._scope(SUMMARIES, workspace_id),
                t.work_summaries.c.work_id.in_(chunk),
            ):
                out[summary.work_id] = summary
        return out

    async def summary_inputs(
        self, workspace_id: UUID, work_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[list[SourceAssertion], set[str]]]:
        out: dict[UUID, tuple[list[SourceAssertion], set[str]]] = {
            work_id: ([], set()) for work_id in work_ids
        }
        a, o, r = t.source_assertions, t.source_observations, t.source_requests
        for chunk in _chunks(list(work_ids)):
            result = await self._c.execute(
                select(a, r.c.connector_id.label("_connector_id"))
                .select_from(
                    a.join(o, o.c.id == a.c.source_observation_id).join(
                        r, r.c.id == o.c.source_request_id
                    )
                )
                .where(a.c.workspace_id == workspace_id, a.c.work_id.in_(chunk))
            )
            for row in result.mappings().all():
                values = dict(row)
                connector = values.pop("_connector_id")
                assertion = ASSERTIONS.from_row(values)
                entry = out.setdefault(assertion.work_id, ([], set()))
                entry[0].append(assertion)
                entry[1].add(connector)
        return out

    async def works_without_summary(self, workspace_id: UUID, *, limit: int) -> list[UUID]:
        result = await self._c.execute(
            select(t.works.c.id)
            .select_from(
                t.works.outerjoin(t.work_summaries, t.work_summaries.c.work_id == t.works.c.id)
            )
            .where(
                t.works.c.workspace_id == workspace_id,
                t.work_summaries.c.work_id.is_(None) | t.work_summaries.c.relevance_text.is_(None),
            )
            .order_by(t.works.c.id)
            .limit(limit)
        )
        return [row[0] for row in result.all()]

    async def human_relevance_work_ids(self, workspace_id: UUID) -> set[UUID]:
        d = t.identity_decisions
        result = await self._c.execute(
            select(d.c.subject_work_ids).where(
                d.c.workspace_id == workspace_id,
                d.c.decision_type == IdentityDecisionType.SET_RELEVANCE.value,
                d.c.decided_by_type == "human",
            )
        )
        return {work_id for row in result.all() for work_id in uuid_list(row[0])}

    async def add_review_step(self, step: ReviewStep) -> None:
        await self._upsert(STEPS, step)

    async def update_review_step(self, step: ReviewStep) -> None:
        await self._upsert(STEPS, step)

    async def get_review_step(self, workspace_id: UUID, step_id: UUID) -> ReviewStep | None:
        return await self._one(
            STEPS, self._scope(STEPS, workspace_id), t.review_steps.c.id == step_id
        )

    @staticmethod
    def _step_conditions(
        workspace_id: UUID, scope_version_id: UUID | None, kinds: Sequence[str] | None
    ) -> list[Any]:
        r = t.review_steps
        conditions: list[Any] = [r.c.workspace_id == workspace_id]
        if scope_version_id is not None:
            conditions.append(r.c.scope_version_id == scope_version_id)
        if kinds:
            conditions.append(r.c.kind.in_(list(kinds)))
        return conditions

    async def list_review_steps(
        self,
        workspace_id: UUID,
        *,
        scope_version_id: UUID | None = None,
        kinds: Sequence[str] | None = None,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[ReviewStep]:
        r = t.review_steps
        conditions = self._step_conditions(workspace_id, scope_version_id, kinds)
        if before is not None:
            at, last = before
            conditions.append((r.c.started_at < at) | ((r.c.started_at == at) & (r.c.id < last)))
        result = await self._c.execute(
            select(r).where(*conditions).order_by(r.c.started_at.desc(), r.c.id.desc()).limit(limit)
        )
        return STEPS.from_rows(result.mappings().all())

    async def count_review_steps(
        self,
        workspace_id: UUID,
        *,
        scope_version_id: UUID | None = None,
        kinds: Sequence[str] | None = None,
    ) -> int:
        result = await self._c.execute(
            select(func.count())
            .select_from(t.review_steps)
            .where(*self._step_conditions(workspace_id, scope_version_id, kinds))
        )
        return int(result.scalar() or 0)

    def _filter_statement(self, workspace_id: UUID, rule: CandidateFilter, columns: Any) -> Any:
        w, s = t.works, t.work_summaries
        conditions: list[Any] = [w.c.workspace_id == workspace_id]
        if rule.identity_status is None:
            conditions.append(w.c.merged_into_work_id.is_(None))
        else:
            conditions.append(w.c.identity_status == rule.identity_status.value)
        if rule.relevance is not None:
            conditions.append(w.c.relevance == rule.relevance.value)
        if rule.relevance_not is not None:
            conditions.append(w.c.relevance != rule.relevance_not.value)
        source = w
        if rule.needs_summary:
            source = w.join(s, s.c.work_id == w.c.id)
            for word in rule.title_words:
                conditions.append(s.c.title_search.like(_contains(word), escape="\\"))
            for word in rule.exclude_words:
                conditions.append(~s.c.title_search.like(_contains(word), escape="\\"))
            if rule.subject_terms:
                conditions.append(
                    or_(
                        *(
                            s.c.subject_terms.like(_contains(term), escape="\\")
                            for term in rule.subject_terms
                        )
                    )
                )
            if rule.year_from is not None:
                conditions.append(s.c.year >= rule.year_from)
            if rule.year_to is not None:
                conditions.append(s.c.year <= rule.year_to)
            if rule.resource_kinds:
                conditions.append(
                    func.lower(func.coalesce(s.c.resource_kind, "")).in_(rule.resource_kinds)
                )
            if rule.journal:
                conditions.append(
                    func.lower(func.coalesce(s.c.journal, "")).like(
                        _contains(rule.journal), escape="\\"
                    )
                )
            if rule.source:
                conditions.append(s.c.sources.like(f"% {_escape(rule.source)} %", escape="\\"))
            if rule.has_abstract is not None:
                conditions.append(s.c.has_abstract.is_(rule.has_abstract))
        return select(columns).select_from(source).where(*conditions)

    async def filter_works(
        self,
        workspace_id: UUID,
        rule: CandidateFilter,
        *,
        limit: int = 100,
        after_id: UUID | None = None,
    ) -> list[Work]:
        statement = self._filter_statement(workspace_id, rule, t.works)
        if after_id is not None:
            statement = statement.where(t.works.c.id > after_id)
        result = await self._c.execute(statement.order_by(t.works.c.id).limit(limit))
        return WORKS.from_rows(result.mappings().all())

    async def count_filtered(self, workspace_id: UUID, rule: CandidateFilter) -> int:
        result = await self._c.execute(self._filter_statement(workspace_id, rule, func.count()))
        return int(result.scalar() or 0)

    async def summary_facets(self, workspace_id: UUID) -> dict[str, Any]:
        s = t.work_summaries
        kinds = await self._c.execute(
            select(s.c.resource_kind, func.count())
            .where(s.c.workspace_id == workspace_id)
            .group_by(s.c.resource_kind)
        )
        sources: dict[str, int] = defaultdict(int)
        for (value,) in (
            await self._c.execute(select(s.c.sources).where(s.c.workspace_id == workspace_id))
        ).all():
            for source in (value or "").split():
                sources[source] += 1
        span = (
            await self._c.execute(
                select(func.min(s.c.year), func.max(s.c.year), func.count()).where(
                    s.c.workspace_id == workspace_id
                )
            )
        ).one()
        works = (
            await self._c.execute(
                select(func.count())
                .select_from(t.works)
                .where(t.works.c.workspace_id == workspace_id)
            )
        ).scalar()
        return {
            "kinds": dict(
                sorted(((k or "", int(n)) for k, n in kinds.all()), key=lambda kv: -kv[1])
            ),
            "sources": dict(sorted(sources.items(), key=lambda kv: -kv[1])),
            "year_min": span[0],
            "year_max": span[1],
            "summarised": int(span[2] or 0),
            "works": int(works or 0),
        }

    async def add_duplicate_candidates(self, rows: Sequence[DuplicateCandidate]) -> None:
        if rows:
            await self._c.execute(
                insert(t.duplicate_candidates),
                [DUPLICATES.upsert_values(row)[0] for row in rows],
            )

    async def update_duplicate_candidates(self, rows: Sequence[DuplicateCandidate]) -> None:
        for row in rows:
            await self._upsert(DUPLICATES, row)

    async def get_duplicate_candidate(
        self, workspace_id: UUID, candidate_id: UUID
    ) -> DuplicateCandidate | None:
        return await self._one(
            DUPLICATES,
            self._scope(DUPLICATES, workspace_id),
            t.duplicate_candidates.c.id == candidate_id,
        )

    def _candidate_conditions(
        self,
        workspace_id: UUID,
        status: str | None,
        min_similarity: float | None,
        work_id: UUID | None = None,
    ) -> list[Any]:
        d = t.duplicate_candidates
        conditions: list[Any] = [d.c.workspace_id == workspace_id]
        if status is not None:
            conditions.append(d.c.status == status)
        if min_similarity is not None:
            conditions.append(d.c.similarity >= min_similarity)
        if work_id is not None:
            conditions.append((d.c.left_work_id == work_id) | (d.c.right_work_id == work_id))
        return conditions

    async def list_duplicate_candidates(
        self,
        workspace_id: UUID,
        *,
        status: str | None = None,
        min_similarity: float | None = None,
        work_id: UUID | None = None,
        limit: int | None = None,
        after: tuple[float, UUID] | None = None,
    ) -> list[DuplicateCandidate]:
        d = t.duplicate_candidates
        conditions = self._candidate_conditions(workspace_id, status, min_similarity, work_id)
        if after is not None:
            similarity, last = after
            conditions.append(
                (d.c.similarity < similarity) | ((d.c.similarity == similarity) & (d.c.id > last))
            )
        statement = select(d).where(*conditions).order_by(d.c.similarity.desc(), d.c.id)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self._c.execute(statement)
        return DUPLICATES.from_rows(result.mappings().all())

    async def count_duplicate_candidates(
        self, workspace_id: UUID, *, status: str | None = None, min_similarity: float | None = None
    ) -> int:
        result = await self._c.execute(
            select(func.count())
            .select_from(t.duplicate_candidates)
            .where(*self._candidate_conditions(workspace_id, status, min_similarity))
        )
        return int(result.scalar() or 0)

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

    async def list_decisions_page(
        self,
        workspace_id: UUID,
        *,
        reason_code: str,
        limit: int = 100,
        after_id: UUID | None = None,
    ) -> list[IdentityDecision]:
        conditions = [
            self._scope(DECISIONS, workspace_id),
            t.identity_decisions.c.reason_code == reason_code,
        ]
        if after_id is not None:
            conditions.append(t.identity_decisions.c.id > after_id)
        return await self._many(
            DECISIONS, *conditions, order=t.identity_decisions.c.id, limit=limit
        )

    async def count_decisions(self, workspace_id: UUID, *, reason_code: str) -> int:
        result = await self._c.execute(
            select(func.count())
            .select_from(t.identity_decisions)
            .where(
                self._scope(DECISIONS, workspace_id),
                t.identity_decisions.c.reason_code == reason_code,
            )
        )
        return int(result.scalar() or 0)


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
        self,
        workspace_id: UUID,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[AcquisitionPlan]:
        conditions = [self._scope(PLANS, workspace_id)]
        if status is not None:
            conditions.append(t.acquisition_plans.c.status == status)
        if limit is None and after_id is None:
            return await self._many(PLANS, *conditions, order=t.acquisition_plans.c.created_at)
        if after_id is not None:
            conditions.append(t.acquisition_plans.c.id > after_id)
        return await self._many(PLANS, *conditions, order=t.acquisition_plans.c.id, limit=limit)

    async def count_plans_by_status(self, workspace_id: UUID) -> dict[str, int]:
        result = await self._c.execute(
            select(t.acquisition_plans.c.status, func.count())
            .where(self._scope(PLANS, workspace_id))
            .group_by(t.acquisition_plans.c.status)
        )
        return {str(status): int(count) for status, count in result.all()}

    async def latest_outcomes(self, workspace_id: UUID, *, status: str) -> dict[str, int]:
        """Plans in ``status`` by the outcome of their latest fetch attempt.

        ``"none"`` counts plans no attempt was made for (an upload-only route).
        """
        a, p = t.acquisition_attempts, t.acquisition_plans
        ranked = (
            select(
                a.c.acquisition_plan_id.label("plan_id"),
                a.c.outcome.label("outcome"),
                func.row_number()
                .over(
                    partition_by=a.c.acquisition_plan_id,
                    order_by=(a.c.requested_at.desc(), a.c.attempt_number.desc()),
                )
                .label("rank"),
            )
            .where(a.c.workspace_id == workspace_id)
            .subquery()
        )
        latest = select(ranked.c.plan_id, ranked.c.outcome).where(ranked.c.rank == 1).subquery()
        # Grouped by the bare column: Postgres binds each "none" literal as its
        # own parameter, so a COALESCE in both SELECT and GROUP BY never matches.
        result = await self._c.execute(
            select(latest.c.outcome, func.count())
            .select_from(p.outerjoin(latest, latest.c.plan_id == p.c.id))
            .where(p.c.workspace_id == workspace_id, p.c.status == status)
            .group_by(latest.c.outcome)
        )
        counts: dict[str, int] = {}
        for outcome, count in result.all():
            key = str(outcome) if outcome is not None else "none"
            counts[key] = counts.get(key, 0) + int(count)
        return counts

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

    async def get_entity(self, workspace_id: UUID, entity_id: UUID) -> Entity | None:
        return await self._one(
            ENTITIES, self._scope(ENTITIES, workspace_id), t.entities.c.id == entity_id
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

    def _queue_conditions(
        self, workspace_id: UUID, work_id: UUID | None, include_invalid: bool
    ) -> list[Any]:
        c, r = t.claims, t.claim_revisions
        conditions: list[Any] = [
            c.c.workspace_id == workspace_id,
            r.c.review_status.notin_(APPROVED_REVIEW_STATES),
        ]
        if work_id is not None:
            conditions.append(c.c.work_id == work_id)
        if not include_invalid:
            conditions.append(r.c.validation_status == "valid")
        return conditions

    async def list_review_queue(
        self,
        workspace_id: UUID,
        *,
        work_id: UUID | None = None,
        include_invalid: bool = False,
        limit: int = 50,
        after_id: UUID | None = None,
    ) -> list[Claim]:
        c, r = t.claims, t.claim_revisions
        conditions = self._queue_conditions(workspace_id, work_id, include_invalid)
        if after_id is not None:
            conditions.append(c.c.id > after_id)
        result = await self._c.execute(
            select(c)
            .join(r, r.c.id == c.c.current_revision_id)
            .where(*conditions)
            .order_by(c.c.id)
            .limit(limit)
        )
        return CLAIMS.from_rows(result.mappings().all())

    async def count_review_queue(
        self, workspace_id: UUID, *, work_id: UUID | None = None, include_invalid: bool = False
    ) -> int:
        c, r = t.claims, t.claim_revisions
        return await self._count(
            c.join(r, r.c.id == c.c.current_revision_id),
            *self._queue_conditions(workspace_id, work_id, include_invalid),
        )

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

    async def list_releases(
        self,
        workspace_id: UUID,
        *,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[CorpusRelease]:
        return await self._newest_page(
            RELEASES,
            t.corpus_releases.c.created_at,
            self._scope(RELEASES, workspace_id),
            limit=limit,
            before=before,
        )

    async def count_releases(self, workspace_id: UUID) -> int:
        return await self._count(t.corpus_releases, self._scope(RELEASES, workspace_id))

    async def list_all_releases(
        self, workspace_id: UUID, *, limit: int = 500, after_id: UUID | None = None
    ) -> list[CorpusRelease]:
        conditions = [self._scope(RELEASES, workspace_id)]
        if after_id is not None:
            conditions.append(t.corpus_releases.c.id > after_id)
        return await self._many(RELEASES, *conditions, order=t.corpus_releases.c.id, limit=limit)

    async def delete(self, workspace_id: UUID, release_id: UUID) -> None:
        await self._c.execute(
            delete(t.release_items).where(
                t.release_items.c.workspace_id == workspace_id,
                t.release_items.c.release_id == release_id,
            )
        )
        await self._c.execute(
            delete(t.corpus_releases).where(
                t.corpus_releases.c.workspace_id == workspace_id,
                t.corpus_releases.c.id == release_id,
            )
        )

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

    async def renew_lease(
        self, workspace_id: UUID, task_id: UUID, *, owner: str, now: datetime, lease_seconds: int
    ) -> bool:
        result = await self._c.execute(
            update(t.tasks)
            .where(
                self._scope(TASKS, workspace_id),
                t.tasks.c.id == task_id,
                t.tasks.c.state == TaskState.LEASED.value,
                t.tasks.c.lease_owner == owner,
            )
            .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        return bool(result.rowcount)

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

    def _task_conditions(
        self,
        workspace_id: UUID,
        states: Sequence[str],
        stage: str | None,
        words: Sequence[str] = (),
    ) -> list[Any]:
        tasks = t.tasks
        conditions = [tasks.c.workspace_id == workspace_id, tasks.c.state.in_(list(states))]
        if stage is not None:
            conditions.append(tasks.c.stage == stage)
        if words:
            conditions += _all_words(
                [
                    cast(tasks.c.id, String),
                    tasks.c.stage,
                    tasks.c.state,
                    tasks.c.human_action_required,
                    tasks.c.error_detail,
                    cast(tasks.c.inputs, String),
                    utc_text(tasks.c.created_at),
                    utc_text(tasks.c.completed_at),
                ],
                words,
            )
        return conditions

    async def page_tasks(
        self,
        workspace_id: UUID,
        *,
        states: Sequence[str],
        stage: str | None = None,
        limit: int = 50,
        after: tuple[datetime, UUID] | None = None,
        words: Sequence[str] = (),
    ) -> list[Task]:
        tasks = t.tasks
        conditions = self._task_conditions(workspace_id, states, stage, words)
        if after is not None:
            at, last = after
            conditions.append(
                (tasks.c.created_at > at) | ((tasks.c.created_at == at) & (tasks.c.id > last))
            )
        result = await self._c.execute(
            select(tasks).where(*conditions).order_by(tasks.c.created_at, tasks.c.id).limit(limit)
        )
        return TASKS.from_rows(result.mappings().all())

    async def count_tasks(
        self,
        workspace_id: UUID,
        *,
        states: Sequence[str],
        stage: str | None = None,
        words: Sequence[str] = (),
    ) -> int:
        result = await self._c.execute(
            select(func.count())
            .select_from(t.tasks)
            .where(*self._task_conditions(workspace_id, states, stage, words))
        )
        return int(result.scalar() or 0)

    async def tasks_by_stage(
        self, workspace_id: UUID, *, states: Sequence[str]
    ) -> list[tuple[str, int, datetime | None]]:
        tasks = t.tasks
        result = await self._c.execute(
            select(tasks.c.stage, func.count(), func.min(tasks.c.created_at))
            .where(*self._task_conditions(workspace_id, states, None))
            .group_by(tasks.c.stage)
        )
        return sorted(
            ((row[0], int(row[1]), row[2]) for row in result.all()),
            key=lambda row: (row[2] is None, row[2] or datetime.max, row[0]),
        )

    async def list_run_tasks(self, workspace_id: UUID, run_id: UUID) -> list[Task]:
        return await self._many(
            TASKS,
            self._scope(TASKS, workspace_id),
            t.tasks.c.run_id == run_id,
            order=t.tasks.c.created_at,
        )

    async def delete_tasks(self, workspace_id: UUID, task_ids: Sequence[UUID]) -> None:
        if not task_ids:
            return
        await self._c.execute(
            delete(t.tasks).where(
                t.tasks.c.workspace_id == workspace_id, t.tasks.c.id.in_(list(task_ids))
            )
        )

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

    @staticmethod
    def _event_conditions(
        workspace_id: UUID, actions: Sequence[str] | None, words: Sequence[str]
    ) -> list[Any]:
        a = t.audit_events
        conditions: list[Any] = [a.c.workspace_id == workspace_id]
        if actions:
            conditions.append(a.c.action.in_(list(actions)))
        if words:
            # Every word must appear, as in a table search, the time included.
            conditions += _all_words(
                [
                    a.c.action,
                    a.c.actor_type,
                    a.c.subject_type,
                    cast(a.c.subject_id, String),
                    a.c.reason_code,
                    cast(a.c.metadata_json, String),
                    utc_text(a.c.occurred_at),
                ],
                words,
            )
        return conditions

    async def page_events(
        self,
        workspace_id: UUID,
        *,
        actions: Sequence[str] | None = None,
        words: Sequence[str] = (),
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[AuditEventRecord]:
        a = t.audit_events
        conditions = self._event_conditions(workspace_id, actions, words)
        if before is not None:
            at, last = before
            conditions.append((a.c.occurred_at < at) | ((a.c.occurred_at == at) & (a.c.id < last)))
        result = await self._c.execute(
            select(a)
            .where(*conditions)
            .order_by(a.c.occurred_at.desc(), a.c.id.desc())
            .limit(limit)
        )
        return AUDIT.from_rows(result.mappings().all())

    async def count_events(
        self,
        workspace_id: UUID,
        *,
        actions: Sequence[str] | None = None,
        words: Sequence[str] = (),
    ) -> int:
        result = await self._c.execute(
            select(func.count())
            .select_from(t.audit_events)
            .where(*self._event_conditions(workspace_id, actions, words))
        )
        return int(result.scalar() or 0)

    async def count_events_by_action(self, workspace_id: UUID) -> dict[str, int]:
        a = t.audit_events
        result = await self._c.execute(
            select(a.c.action, func.count())
            .where(a.c.workspace_id == workspace_id)
            .group_by(a.c.action)
        )
        return {row[0]: int(row[1]) for row in sorted(result.all())}

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
        self,
        workspace_id: UUID,
        user_id: UUID,
        *,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
        include_deleted: bool = True,
    ) -> list[ChatSession]:
        conditions = [self._scope(SESSIONS, workspace_id), t.chat_sessions.c.user_id == user_id]
        if not include_deleted:
            conditions.append(t.chat_sessions.c.deleted_at.is_(None))
        return await self._newest_page(
            SESSIONS, t.chat_sessions.c.updated_at, *conditions, limit=limit, before=before
        )

    async def count_sessions(
        self, workspace_id: UUID, user_id: UUID, *, include_deleted: bool = True
    ) -> int:
        conditions = [self._scope(SESSIONS, workspace_id), t.chat_sessions.c.user_id == user_id]
        if not include_deleted:
            conditions.append(t.chat_sessions.c.deleted_at.is_(None))
        return await self._count(t.chat_sessions, *conditions)

    async def list_workspace_sessions(
        self, workspace_id: UUID, *, limit: int = 1000, after_id: UUID | None = None
    ) -> list[ChatSession]:
        conditions = [self._scope(SESSIONS, workspace_id)]
        if after_id is not None:
            conditions.append(t.chat_sessions.c.id > after_id)
        return await self._many(SESSIONS, *conditions, order=t.chat_sessions.c.id, limit=limit)

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

    async def usage_by_user(
        self, workspace_id: UUID, *, user_ids: Sequence[UUID] | None = None
    ) -> dict[UUID, UsageTotals]:
        sessions, messages = t.chat_sessions, t.chat_messages
        is_user = messages.c.role == MessageRole.USER.value
        is_answer = messages.c.role == MessageRole.ASSISTANT.value
        statement = (
            select(
                sessions.c.user_id,
                func.count(func.distinct(sessions.c.id)).label("sessions"),
                func.count(messages.c.id).filter(is_user).label("questions"),
                func.count(messages.c.id).filter(is_answer).label("answers"),
                func.coalesce(func.sum(messages.c.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(messages.c.output_tokens), 0).label("output_tokens"),
                func.max(messages.c.created_at).label("last_active_at"),
                func.avg(messages.c.latency_ms).filter(is_answer).label("avg_latency_ms"),
            )
            .select_from(sessions.outerjoin(messages, messages.c.session_id == sessions.c.id))
            .where(sessions.c.workspace_id == workspace_id)
            .group_by(sessions.c.user_id)
        )
        if user_ids is not None:
            statement = statement.where(sessions.c.user_id.in_(list(user_ids)))
        result = await self._c.execute(statement)
        return {
            UUID(str(row.user_id)): UsageTotals(
                sessions=int(row.sessions or 0),
                questions=int(row.questions or 0),
                answers=int(row.answers or 0),
                input_tokens=int(row.input_tokens or 0),
                output_tokens=int(row.output_tokens or 0),
                last_active_at=row.last_active_at,
                avg_latency_ms=(
                    float(row.avg_latency_ms) if row.avg_latency_ms is not None else None
                ),
            )
            for row in result
        }


class _Evaluations(_Base):
    _RUN_MUTABLE = (
        "status",
        "metrics",
        "gates",
        "failures",
        "per_case",
        "error_detail",
        "completed_at",
        "task_id",
    )

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
                created_at=run.get("created_at") or datetime.now(tz=_utc()),
                status=run.get("status", "succeeded"),
                kind=run.get("kind"),
                task_id=uuid_or_none(run.get("task_id")),
                requested_by=uuid_or_none(run.get("requested_by")),
                error_detail=run.get("error_detail"),
                completed_at=run.get("completed_at"),
            )
        )

    async def update_run(self, run: dict[str, Any]) -> None:
        values: dict[str, Any] = {key: run[key] for key in self._RUN_MUTABLE if key in run}
        if "failures" in values:
            values["failures"] = [str(item) for item in values["failures"]]
        if "task_id" in values:
            values["task_id"] = uuid_or_none(values["task_id"])
        await self._c.execute(
            update(t.evaluation_runs)
            .where(
                t.evaluation_runs.c.workspace_id == uuid_or_none(run["workspace_id"]),
                t.evaluation_runs.c.id == uuid_or_none(run["id"]),
            )
            .values(**values)
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
        self,
        workspace_id: UUID,
        *,
        benchmark_id: str | None = None,
        release_id: UUID | None = None,
        limit: int = 100,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[dict[str, Any]]:
        runs = t.evaluation_runs
        conditions = self._run_conditions(workspace_id, benchmark_id, release_id)
        if before is not None:
            at, last = before
            conditions.append(
                (runs.c.created_at < at) | ((runs.c.created_at == at) & (runs.c.id < last))
            )
        result = await self._c.execute(
            select(runs)
            .where(*conditions)
            .order_by(runs.c.created_at.desc(), runs.c.id.desc())
            .limit(limit)
        )
        return [dict(row) for row in result.mappings().all()]

    @staticmethod
    def _run_conditions(
        workspace_id: UUID, benchmark_id: str | None, release_id: UUID | None
    ) -> list[Any]:
        runs = t.evaluation_runs
        conditions: list[Any] = [runs.c.workspace_id == workspace_id]
        if benchmark_id is not None:
            conditions.append(runs.c.benchmark.like(f"{benchmark_id}%"))
        if release_id is not None:
            conditions.append(runs.c.release_id == release_id)
        return conditions

    async def count_runs(
        self,
        workspace_id: UUID,
        *,
        benchmark_id: str | None = None,
        release_id: UUID | None = None,
    ) -> int:
        return await self._count(
            t.evaluation_runs, *self._run_conditions(workspace_id, benchmark_id, release_id)
        )

    async def add_benchmark(self, benchmark: dict[str, Any]) -> None:
        await self._c.execute(
            insert(t.benchmarks).values(
                id=uuid_or_none(benchmark["id"]),
                workspace_id=uuid_or_none(benchmark["workspace_id"]),
                benchmark_id=benchmark["benchmark_id"],
                version=benchmark["version"],
                kind=benchmark["kind"],
                checksum=benchmark["checksum"],
                sensitivity=benchmark["sensitivity"],
                definition=benchmark["definition"],
                registered_by=uuid_or_none(benchmark.get("registered_by")),
                created_at=benchmark.get("created_at") or datetime.now(tz=_utc()),
            )
        )

    async def get_benchmark(
        self, workspace_id: UUID, benchmark_id: str, version: str
    ) -> dict[str, Any] | None:
        result = await self._c.execute(
            select(t.benchmarks).where(
                t.benchmarks.c.workspace_id == workspace_id,
                t.benchmarks.c.benchmark_id == benchmark_id,
                t.benchmarks.c.version == version,
            )
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_benchmarks(self, workspace_id: UUID) -> list[dict[str, Any]]:
        result = await self._c.execute(
            select(t.benchmarks)
            .where(t.benchmarks.c.workspace_id == workspace_id)
            .order_by(t.benchmarks.c.benchmark_id, t.benchmarks.c.version)
        )
        return [dict(row) for row in result.mappings().all()]

    _REVIEW_MUTABLE = (
        "status",
        "scores",
        "flags",
        "reviewer_confidence",
        "comment",
        "suggested_answer",
        "submitted_at",
    )

    async def add_review(self, review: dict[str, Any]) -> None:
        await self._c.execute(
            insert(t.evaluation_reviews).values(
                id=uuid_or_none(review["id"]),
                workspace_id=uuid_or_none(review["workspace_id"]),
                run_id=uuid_or_none(review["run_id"]),
                case_id=review["case_id"],
                assigned_to=uuid_or_none(review["assigned_to"]),
                assigned_by=uuid_or_none(review.get("assigned_by")),
                status=review.get("status", "assigned"),
                scores=review.get("scores", {}),
                flags=[str(flag) for flag in review.get("flags", [])],
                reviewer_confidence=review.get("reviewer_confidence"),
                comment=review.get("comment"),
                suggested_answer=review.get("suggested_answer"),
                created_at=review.get("created_at") or datetime.now(tz=_utc()),
                submitted_at=review.get("submitted_at"),
            )
        )

    async def update_review(self, review: dict[str, Any]) -> None:
        values: dict[str, Any] = {key: review[key] for key in self._REVIEW_MUTABLE if key in review}
        if "flags" in values:
            values["flags"] = [str(flag) for flag in values["flags"]]
        await self._c.execute(
            update(t.evaluation_reviews)
            .where(
                t.evaluation_reviews.c.workspace_id == uuid_or_none(review["workspace_id"]),
                t.evaluation_reviews.c.id == uuid_or_none(review["id"]),
            )
            .values(**values)
        )

    async def get_review(self, workspace_id: UUID, review_id: UUID) -> dict[str, Any] | None:
        result = await self._c.execute(
            select(t.evaluation_reviews).where(
                t.evaluation_reviews.c.workspace_id == workspace_id,
                t.evaluation_reviews.c.id == review_id,
            )
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_reviews(
        self,
        workspace_id: UUID,
        *,
        run_id: UUID | None = None,
        assigned_to: UUID | None = None,
    ) -> list[dict[str, Any]]:
        conditions = [t.evaluation_reviews.c.workspace_id == workspace_id]
        if run_id is not None:
            conditions.append(t.evaluation_reviews.c.run_id == run_id)
        if assigned_to is not None:
            conditions.append(t.evaluation_reviews.c.assigned_to == assigned_to)
        result = await self._c.execute(
            select(t.evaluation_reviews)
            .where(*conditions)
            .order_by(t.evaluation_reviews.c.created_at, t.evaluation_reviews.c.case_id)
        )
        return [dict(row) for row in result.mappings().all()]

    async def list_release_gates(self, workspace_id: UUID) -> list[dict[str, Any]]:
        result = await self._c.execute(
            select(t.release_gate_benchmarks)
            .where(t.release_gate_benchmarks.c.workspace_id == workspace_id)
            .order_by(t.release_gate_benchmarks.c.benchmark_id)
        )
        return [dict(row) for row in result.mappings().all()]

    async def set_release_gate(self, gate: dict[str, Any]) -> None:
        await self.clear_release_gate(UUID(str(gate["workspace_id"])), gate["benchmark_id"])
        await self._c.execute(
            insert(t.release_gate_benchmarks).values(
                workspace_id=uuid_or_none(gate["workspace_id"]),
                benchmark_id=gate["benchmark_id"],
                version=gate["version"],
                set_by=uuid_or_none(gate.get("set_by")),
                set_at=gate.get("set_at") or datetime.now(tz=_utc()),
            )
        )

    async def clear_release_gate(self, workspace_id: UUID, benchmark_id: str) -> None:
        await self._c.execute(
            delete(t.release_gate_benchmarks).where(
                t.release_gate_benchmarks.c.workspace_id == workspace_id,
                t.release_gate_benchmarks.c.benchmark_id == benchmark_id,
            )
        )

    async def delete_run(self, workspace_id: UUID, run_id: UUID) -> None:
        await self._c.execute(
            delete(t.evaluation_reviews).where(
                t.evaluation_reviews.c.workspace_id == workspace_id,
                t.evaluation_reviews.c.run_id == run_id,
            )
        )
        await self._c.execute(
            delete(t.evaluation_runs).where(
                t.evaluation_runs.c.workspace_id == workspace_id,
                t.evaluation_runs.c.id == run_id,
            )
        )

    async def delete_benchmark(self, workspace_id: UUID, benchmark_id: str, version: str) -> None:
        await self._c.execute(
            delete(t.benchmarks).where(
                t.benchmarks.c.workspace_id == workspace_id,
                t.benchmarks.c.benchmark_id == benchmark_id,
                t.benchmarks.c.version == version,
            )
        )


def _utc():
    from datetime import UTC

    return UTC


#: Ids per statement: well under asyncpg's 32,767 bind parameters.
_PURGE_CHUNK = 2_000


def _chunked(ids: Any) -> list[list[Any]]:
    values = list(ids)
    return [values[i : i + _PURGE_CHUNK] for i in range(0, len(values), _PURGE_CHUNK)]


class _ScopePurge(_Base):
    """``ScopePurgeRepository``: set-based, children first.

    Explicit deletes rather than ``ON DELETE CASCADE``, as ``delete_run`` does:
    SQLite enforces cascades only with a per-connection pragma. A self-reference
    (a rendition superseding another, a revision superseding another, a work
    merged into another) is cleared first, so no chunk deletes a row another
    still names.
    """

    async def _ids(self, workspace_id: UUID, column: Any, ids: Any) -> set[UUID]:
        found: set[UUID] = set()
        table = column.table
        for chunk in _chunked(ids):
            result = await self._c.execute(
                select(table.c.id).where(table.c.workspace_id == workspace_id, column.in_(chunk))
            )
            found.update(row[0] for row in result)
        return found

    async def _delete(self, workspace_id: UUID, column: Any, ids: Any) -> None:
        table = column.table
        for chunk in _chunked(ids):
            await self._c.execute(
                delete(table).where(table.c.workspace_id == workspace_id, column.in_(chunk))
            )

    async def _clear(self, workspace_id: UUID, column: Any, ids: Any) -> None:
        """``column = NULL`` on the rows whose id is in ``ids``."""
        table = column.table
        for chunk in _chunked(ids):
            await self._c.execute(
                update(table)
                .where(table.c.workspace_id == workspace_id, table.c.id.in_(chunk))
                .values({column.name: None})
            )

    async def works_found_by_runs(
        self, workspace_id: UUID, run_ids: Sequence[UUID]
    ) -> tuple[set[UUID], set[UUID]]:
        if not run_ids:
            return set(), set()
        a, o, r = t.source_assertions, t.source_observations, t.source_requests
        joined = a.join(o, a.c.source_observation_id == o.c.id).join(
            r, o.c.source_request_id == r.c.id
        )
        result = await self._c.execute(
            select(a.c.work_id, r.c.discovery_run_id)
            .select_from(joined)
            .where(a.c.workspace_id == workspace_id, a.c.work_id.is_not(None))
            .distinct()
        )
        runs = set(run_ids)
        mine: set[UUID] = set()
        others: set[UUID] = set()
        for work_id, run_id in result:
            (mine if run_id in runs else others).add(work_id)
        return mine - others, mine & others

    async def work_dependants(
        self, workspace_id: UUID, work_ids: Sequence[UUID]
    ) -> dict[str, set[UUID]]:
        ids = self._ids
        plans = await ids(workspace_id, t.acquisition_plans.c.work_id, work_ids)
        assets = await ids(workspace_id, t.assets.c.work_id, work_ids)
        renditions = await ids(workspace_id, t.renditions.c.work_id, work_ids)
        renditions |= await ids(workspace_id, t.renditions.c.asset_id, assets)
        claims = await ids(workspace_id, t.claims.c.work_id, work_ids)
        revisions = await ids(workspace_id, t.claim_revisions.c.claim_id, claims)
        reviews = await ids(workspace_id, t.reviews.c.revision_id, revisions)
        reviews |= await ids(workspace_id, t.reviews.c.subject_id, claims | revisions)
        return {
            "manifestations": await ids(workspace_id, t.manifestations.c.work_id, work_ids),
            "plans": plans,
            "attempts": await ids(
                workspace_id, t.acquisition_attempts.c.acquisition_plan_id, plans
            ),
            "assets": assets,
            "parse_runs": await ids(workspace_id, t.parse_runs.c.asset_id, assets),
            "renditions": renditions,
            "spans": await ids(workspace_id, t.evidence_spans.c.rendition_id, renditions),
            "experiments": await ids(workspace_id, t.experiments.c.work_id, work_ids),
            "claims": claims,
            "revisions": revisions,
            "reviews": reviews,
        }

    async def releases_holding(self, workspace_id: UUID, work_ids: Sequence[UUID]) -> set[UUID]:
        found: set[UUID] = set()
        items = t.release_items
        for chunk in _chunked(work_ids):
            result = await self._c.execute(
                select(items.c.release_id)
                .where(items.c.workspace_id == workspace_id, items.c.work_id.in_(chunk))
                .distinct()
            )
            found.update(row[0] for row in result)
        return found

    async def purge(
        self,
        workspace_id: UUID,
        *,
        work_ids: Sequence[UUID],
        dependants: dict[str, set[UUID]],
        run_ids: Sequence[UUID],
        scope_version_ids: Sequence[UUID],
        lineage_keys: Sequence[str],
    ) -> None:
        ws, d = workspace_id, dependants
        works = set(work_ids)
        rm, clear = self._delete, self._clear
        # What was made from the papers' full texts, deepest first.
        await rm(ws, t.reviews.c.id, d.get("reviews", ()))
        await clear(ws, t.claim_revisions.c.supersedes_revision_id, d.get("revisions", ()))
        await rm(ws, t.claim_revisions.c.id, d.get("revisions", ()))
        await rm(ws, t.claims.c.id, d.get("claims", ()))
        await rm(ws, t.experiments.c.id, d.get("experiments", ()))
        await rm(ws, t.evidence_spans.c.id, d.get("spans", ()))
        await clear(ws, t.renditions.c.supersedes_rendition_id, d.get("renditions", ()))
        await rm(ws, t.renditions.c.id, d.get("renditions", ()))
        await rm(ws, t.parse_runs.c.id, d.get("parse_runs", ()))
        subjects = (
            works | d.get("assets", set()) | d.get("manifestations", set()) | d.get("plans", set())
        )
        await rm(ws, t.rights_decisions.c.subject_id, subjects)
        await rm(ws, t.assets.c.id, d.get("assets", ()))
        await rm(ws, t.acquisition_attempts.c.id, d.get("attempts", ()))
        await rm(ws, t.acquisition_plans.c.id, d.get("plans", ()))
        await rm(ws, t.manifestations.c.id, d.get("manifestations", ()))
        # The papers' review state.
        await rm(ws, t.work_relations.c.from_work_id, works)
        await rm(ws, t.work_relations.c.to_work_id, works)
        await rm(ws, t.work_identifiers.c.work_id, works)
        await rm(ws, t.work_summaries.c.work_id, works)
        await rm(ws, t.duplicate_candidates.c.left_work_id, works)
        await rm(ws, t.duplicate_candidates.c.right_work_id, works)
        await rm(ws, t.work_metadata.c.work_id, works)
        decisions = await self._c.execute(
            select(t.identity_decisions.c.id, t.identity_decisions.c.subject_work_ids).where(
                t.identity_decisions.c.workspace_id == ws
            )
        )
        only_these = [
            decision_id
            for decision_id, subject_ids in decisions
            if subject_ids and {UUID(str(s)) for s in subject_ids} <= works
        ]
        await rm(ws, t.identity_decisions.c.id, only_these)
        # The runs, with every page, record and assertion: including assertions
        # linked to papers that stay, which other scopes found too.
        requests = await self._ids(ws, t.source_requests.c.discovery_run_id, run_ids)
        observations = await self._ids(ws, t.source_observations.c.source_request_id, requests)
        await rm(ws, t.source_assertions.c.source_observation_id, observations)
        await rm(ws, t.source_observations.c.id, observations)
        await rm(ws, t.source_requests.c.id, requests)
        await rm(ws, t.discovery_runs.c.id, run_ids)
        await clear(ws, t.works.c.merged_into_work_id, works)
        await rm(ws, t.works.c.id, works)
        for chunk in _chunked(lineage_keys):
            await self._c.execute(
                delete(t.source_cursors).where(
                    t.source_cursors.c.workspace_id == ws,
                    t.source_cursors.c.lineage_key.in_(chunk),
                )
            )
        await rm(ws, t.review_steps.c.scope_version_id, scope_version_ids)
        await rm(ws, t.scope_versions.c.id, scope_version_ids)


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
        self.stage_results = _SqlStageResults(connection)
        self.imports = _SqlImports(connection)
        self.tasks = _Tasks(connection, dialect=dialect)
        self.audit = _Audit(connection)
        self.chat = _Chat(connection)
        self.evaluations = _Evaluations(connection)
        self.purge = _ScopePurge(connection)
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


class _SqlImports:
    def __init__(self, connection) -> None:
        self._c = connection

    async def add(self, record: ImportRecord) -> None:
        await self._c.execute(
            insert(t.archive_imports).values(
                id=record.id,
                workspace_id=record.workspace_id,
                library_id=record.library_id,
                snapshot_id=record.snapshot_id,
                commit_sequence=record.commit_sequence,
                policy_sequence=record.policy_sequence,
                policy_checkpoint_digest=record.policy_checkpoint_digest,
                state=record.state.value,
                detail=record.detail,
                counts_json=dict(record.counts),
                imported_at=record.imported_at,
            )
        )

    async def latest(self, library_id: UUID) -> ImportRecord | None:
        result = await self._c.execute(
            select(t.archive_imports)
            .where(
                t.archive_imports.c.library_id == library_id,
                t.archive_imports.c.state == ImportState.ACTIVE.value,
            )
            .order_by(
                t.archive_imports.c.commit_sequence.desc(), t.archive_imports.c.imported_at.desc()
            )
            .limit(1)
        )
        row = result.mappings().first()
        if row is None:
            return None
        return ImportRecord(
            id=row["id"],
            workspace_id=row["workspace_id"],
            library_id=row["library_id"],
            snapshot_id=row["snapshot_id"],
            commit_sequence=row["commit_sequence"],
            policy_sequence=row["policy_sequence"],
            policy_checkpoint_digest=row["policy_checkpoint_digest"],
            state=ImportState(row["state"]),
            detail=row["detail"],
            counts=dict(row["counts_json"] or {}),
            imported_at=row["imported_at"],
        )

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
        values = {
            "library_id": library_id,
            "title": title,
            "publication_year": publication_year,
            "container_title": container_title,
        }
        where = and_(
            t.work_metadata.c.workspace_id == workspace_id, t.work_metadata.c.work_id == work_id
        )
        existing = await self._c.execute(select(t.work_metadata.c.work_id).where(where))
        if existing.first() is None:
            await self._c.execute(
                insert(t.work_metadata).values(workspace_id=workspace_id, work_id=work_id, **values)
            )
        else:
            await self._c.execute(update(t.work_metadata).where(where).values(**values))

    async def get_work_metadata(self, workspace_id: UUID, work_id: UUID) -> dict[str, Any] | None:
        result = await self._c.execute(
            select(t.work_metadata).where(
                t.work_metadata.c.workspace_id == workspace_id,
                t.work_metadata.c.work_id == work_id,
            )
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None


class _SqlStageResults:
    def __init__(self, connection) -> None:
        self._c = connection

    async def record(self, result: StageResultEnvelope, *, workspace_id: UUID) -> None:
        payload = result.model_dump(mode="json")
        existing = await self._c.execute(
            select(t.stage_results.c.id).where(
                t.stage_results.c.workspace_id == workspace_id,
                t.stage_results.c.reuse_fingerprint == result.reuse_fingerprint,
            )
        )
        row = existing.first()
        values = {
            "workspace_id": workspace_id,
            "stage": result.stage,
            "reuse_fingerprint": result.reuse_fingerprint,
            "outcome": result.outcome.value,
            "envelope_json": payload,
        }
        if row is None:
            await self._c.execute(t.stage_results.insert().values(id=uuid4(), **values))
            return
        # Replace: a retryable failure recorded last week must give way to this
        # week's success, or retrying it achieves nothing.
        await self._c.execute(
            t.stage_results.update().where(t.stage_results.c.id == row[0]).values(**values)
        )

    async def find(self, workspace_id: UUID, reuse_fingerprint: str) -> StageResultEnvelope | None:
        result = await self._c.execute(
            select(t.stage_results.c.envelope_json).where(
                t.stage_results.c.workspace_id == workspace_id,
                t.stage_results.c.reuse_fingerprint == reuse_fingerprint,
            )
        )
        row = result.first()
        if row is None or not row[0]:
            return None
        return StageResultEnvelope.model_validate(row[0])


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

    async def list_for_library(self, library_id: UUID) -> list[PublicationRecord]:
        result = await self._c.execute(
            select(t.archive_publications)
            .where(t.archive_publications.c.library_id == library_id)
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
