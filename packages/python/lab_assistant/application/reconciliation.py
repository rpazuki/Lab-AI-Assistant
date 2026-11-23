"""Stage 2: reconciliation and adjudication.

Assertions become works. Disagreement is preserved:

* exact strong-identifier bridges merge automatically, with the full assessment
  persisted as the decision explanation;
* a contradiction quarantines the work as ``identity_conflict``;
* title/author/year similarity produces a *proposal* for a human queue;
* unknown relevance is never silently included.

Every automatic action writes an ``IdentityDecision`` with ``decided_by_type =
automatic_rule``, so "why are these one work" is always answerable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import collect_pages
from lab_assistant.application.review_history import record_step
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import NotFound, StateTransitionError, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import (
    StageCommand,
    StageName,
    StageResult,
    StageStatus,
    TaskRequest,
)
from lab_assistant.domain.identity import (
    DUPLICATE_PROPOSAL_REASON,
    DuplicateProposal,
    IdentityDecision,
    IdentityDecisionType,
    RelationKind,
    Work,
    WorkIdentifier,
    WorkKind,
    WorkRelation,
    assess_merge,
    normalise_identifier,
    propose_duplicates,
    resolve_canonical_field,
)
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.observation import Predicate, SourceAssertion
from lab_assistant.domain.review import (
    DuplicateCandidate,
    DuplicateStatus,
    ReviewStepKind,
    summarise_work,
)
from lab_assistant.domain.screening import has_abstract, relevance_text, score_for_scope
from lab_assistant.domain.states import IdentityState, RelevanceState
from lab_assistant.domain.workspace import Capability
from lab_assistant.provenance.fingerprints import task_fingerprint

__all__ = [
    "AUTHORITY_ORDER",
    "IdentityService",
    "ReconciliationHandler",
    "ReconciliationReport",
    "canonical_metadata",
]

IMPLEMENTATION_VERSION = "reconciliation/1"

#: Records written per transaction. Small enough that a restart loses little and
#: candidates appear while a large run is in progress; large enough that commits
#: are a small share of the time.
RECONCILIATION_BATCH_SIZE = 500

#: How far a chain of merges is followed before giving up on it.
_MAX_MERGE_CHAIN = 16

#: Field authority by source class. A DOI registry outranks an aggregator for
#: registration metadata; a full-text asset outranks both for reported values.
AUTHORITY_ORDER: tuple[str, ...] = (
    "full_text_asset",
    "doi_registry",
    "biomedical_index",
    "preprint_server",
    "aggregator",
    "legacy_import",
)


@dataclass(slots=True)
class ReconciliationReport:
    works_created: int = 0
    automatic_merges: int = 0
    conflicts: int = 0
    duplicate_proposals: int = 0
    relevance_included: int = 0
    relevance_excluded: int = 0
    relevance_review: int = 0
    title_only_merges: int = 0  # must always be zero, by invariant
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_metrics(self) -> dict[str, Any]:
        return {
            "works_created": self.works_created,
            "automatic_merges": self.automatic_merges,
            "conflicts": self.conflicts,
            "duplicate_proposals": self.duplicate_proposals,
            "relevance_included": self.relevance_included,
            "relevance_excluded": self.relevance_excluded,
            "relevance_review": self.relevance_review,
            "title_only_merges": self.title_only_merges,
        }


def canonical_metadata(assertions: list[SourceAssertion]) -> dict[str, Any]:
    """Resolve canonical metadata *with* the assertion ids that support it."""
    grouped: dict[Predicate, list[dict[str, Any]]] = defaultdict(list)
    for assertion in assertions:
        grouped[assertion.predicate].append(
            {
                "id": assertion.id,
                "value_json": assertion.value_json,
                "normalised_value_json": assertion.normalised_value_json,
                "confidence": assertion.confidence,
                "source_priority_class": assertion.source_priority_class,
                "asserted_at_source": assertion.asserted_at_source,
            }
        )

    resolved: dict[str, Any] = {}
    provenance: dict[str, list[str]] = {}
    for predicate in (
        Predicate.TITLE,
        Predicate.ABSTRACT,
        Predicate.JOURNAL,
        Predicate.YEAR,
        Predicate.PUBLICATION_DATE,
        Predicate.LANGUAGE,
        Predicate.LICENCE,
        Predicate.RESOURCE_KIND,
        Predicate.PUBLISHER,
        Predicate.ACCESS_STATUS,
    ):
        if predicate not in grouped:
            continue
        value, supporting = resolve_canonical_field(
            grouped[predicate], authority_order=AUTHORITY_ORDER
        )
        if value is None:
            # Fall back to the verbatim value when no normalised form exists.
            value = grouped[predicate][0]["value_json"]
        resolved[predicate.value] = value
        provenance[predicate.value] = [str(i) for i in supporting]

    authors = [a["value_json"] for a in grouped.get(Predicate.AUTHOR, [])]
    if authors:
        resolved["authors"] = authors
        provenance["authors"] = [str(a["id"]) for a in grouped[Predicate.AUTHOR]]
    subjects = [a["value_json"] for a in grouped.get(Predicate.SUBJECT_TERM, [])]
    if subjects:
        resolved["subject_terms"] = subjects
        provenance["subject_terms"] = [str(a["id"]) for a in grouped[Predicate.SUBJECT_TERM]]

    resolved["_provenance"] = provenance
    return resolved


async def refresh_summaries(
    uow: Any, workspace_id: UUID, work_ids: Iterable[UUID], *, now: datetime
) -> None:
    """Rebuild the filter summaries of these works from their current assertions."""
    ids = list(dict.fromkeys(work_ids))
    if not ids:
        return
    inputs = await uow.works.summary_inputs(workspace_id, ids)
    await uow.works.upsert_summaries(
        [
            summarise_work(work_id, workspace_id, canonical_metadata(assertions), sources, now=now)
            for work_id, (assertions, sources) in inputs.items()
        ]
    )


class ReconciliationHandler:
    """Stage handler: build works from a frozen or running run's assertions."""

    stage_name = StageName.RECONCILIATION
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        """Group the run's records into works, a batch of records per transaction.

        Every lookup goes to an in-memory map of the workspace's identifiers rather
        than to the database: a run is tens of thousands of records, each with
        several identifiers, and the work they resolve to is mostly created by this
        same run. Each batch commits on its own, so candidates appear while the run
        is in progress, and a retry after a restart resumes rather than starting
        again: a record whose assertions already name a work keeps that work.
        """
        context = self._context
        workspace_id = command.workspace_id
        run_id = UUID(str(command.inputs["discovery_run_id"]))
        report = ReconciliationReport()

        async with context.uow_factory() as uow:
            run = await uow.observations.get_run(workspace_id, run_id)
            if run is None:
                raise NotFound(f"discovery run {run_id} not found")
            scope = await uow.configs.get_scope_version(workspace_id, run.scope_version_id)
            if scope is None:
                raise NotFound("scope version not found for run")
            requests = await uow.observations.list_requests(workspace_id, run_id)
            observations = []
            for request in requests:
                observations.extend(
                    await uow.observations.list_observations(workspace_id, request_id=request.id)
                )
            assertions_by_subject: dict[str, list[SourceAssertion]] = defaultdict(list)
            for observation in observations:
                for assertion in await uow.observations.list_assertions(
                    workspace_id, observation_id=observation.id
                ):
                    assertions_by_subject[assertion.subject_hint].append(assertion)
            index = await _IdentityIndex.load(uow, workspace_id)

        #: One entry per work, in the order works were first met.
        titles_by_work: dict[UUID, tuple[UUID, str, int | None, str | None]] = {}
        groups = [assertions for _, assertions in sorted(assertions_by_subject.items())]
        for start in range(0, len(groups), RECONCILIATION_BATCH_SIZE):
            async with context.uow_factory() as uow:
                touched: list[UUID] = []
                for assertions in groups[start : start + RECONCILIATION_BATCH_SIZE]:
                    await self._reconcile_record(
                        uow, workspace_id, assertions, scope, index, report, titles_by_work
                    )
                    touched.extend(a.work_id for a in assertions if a.work_id is not None)
                await refresh_summaries(uow, workspace_id, touched, now=context.now())
                await uow.commit()

        async with context.uow_factory() as uow:
            # A pair already in the review queue, from an earlier pass over this
            # run or another run, is not queued twice.
            queued = {
                frozenset((row.left_work_id, row.right_work_id))
                for row in await uow.works.list_duplicate_candidates(workspace_id)
            }
            active_titles = [
                entry
                for work_id, entry in titles_by_work.items()
                if (work := index.works.get(work_id)) is not None and work.is_active
            ]
            proposals = [
                proposal
                for proposal in propose_duplicates(active_titles)
                if frozenset((proposal.left_work_id, proposal.right_work_id)) not in queued
            ]
            report.duplicate_proposals = len(proposals)
            queue: list[DuplicateCandidate] = []
            for proposal in proposals:
                decision = IdentityDecision(
                    id=context.new_id(),
                    workspace_id=workspace_id,
                    decision_type=IdentityDecisionType.REOPEN,
                    subject_work_ids=(proposal.left_work_id, proposal.right_work_id),
                    after={
                        "proposal": "duplicate_review",
                        "similarity": proposal.similarity,
                        "features": proposal.features,
                    },
                    reason_code=proposal.reason_code,
                    decided_by_type="automatic_rule",
                    decided_at=context.now(),
                )
                await uow.works.add_decision(decision)
                queue.append(duplicate_row(decision))
            await uow.works.add_duplicate_candidates(queue)

            await uow.audit.append(
                context.audit.build(
                    workspace_id=workspace_id,
                    actor_type=ActorType.WORKER,
                    actor_id=command.actor_id,
                    action=AuditAction.IDENTITY_DECIDED,
                    subject_type="discovery_run",
                    subject_id=run_id,
                    previous_hash=await uow.audit.latest_hash(workspace_id),
                    run_id=run_id,
                    reason_code="reconciliation_pass",
                    metadata=report.as_metrics(),
                )
            )
            await record_step(
                uow,
                context,
                workspace_id=workspace_id,
                kind=ReviewStepKind.RECONCILED,
                actor_id=None,
                scope_version_id=run.scope_version_id,
                subject_id=run_id,
                counts=report.as_metrics(),
            )
            await uow.commit()

        return StageResult(
            stage=StageName.RECONCILIATION,
            status=StageStatus.SUCCEEDED,
            metrics=report.as_metrics(),
            quality_gates={
                "no_title_only_merge": report.title_only_merges == 0,
                "conflicts_quarantined": True,
            },
            human_action_required=(
                "identity conflicts need adjudication" if report.conflicts else None
            ),
            next_tasks=(
                TaskRequest(
                    stage=StageName.ACQUISITION_PLANNING,
                    inputs={"discovery_run_id": str(run_id)},
                ),
            ),
        )

    async def _reconcile_record(
        self,
        uow: Any,
        workspace_id: UUID,
        assertions: list[SourceAssertion],
        scope: Any,
        index: _IdentityIndex,
        report: ReconciliationReport,
        titles_by_work: dict[UUID, tuple[UUID, str, int | None, str | None]],
    ) -> None:
        """Resolve one source record to a work, creating or merging as the rules say."""
        context = self._context
        identifiers = _identifier_assertions(assertions)
        metadata = canonical_metadata(assertions)
        kind = _work_kind(metadata, identifiers)

        # A record a previous attempt already resolved keeps its work. Without
        # this, a retry would give every record with no strong identifier a
        # second work.
        existing_work = await index.resolved_work(
            uow, workspace_id, [a.work_id for a in assertions if a.work_id is not None]
        )
        for scheme, value, assertion_id in identifiers:
            if not scheme.is_strong:
                continue
            found = await index.find(uow, workspace_id, scheme.value, value)
            if found is None:
                continue
            if existing_work is None:
                existing_work = found
                continue
            if found.id == existing_work.id:
                continue
            # Two existing works share this record's identifiers: only a strong
            # bridge with no contradiction may merge them.
            left_ids = await uow.works.list_identifiers(workspace_id, existing_work.id)
            right_ids = await uow.works.list_identifiers(workspace_id, found.id)
            assessment = assess_merge(
                left=existing_work,
                right=found,
                left_identifiers=left_ids,
                right_identifiers=right_ids,
            )
            decision = IdentityDecision(
                id=context.new_id(),
                workspace_id=workspace_id,
                decision_type=(
                    IdentityDecisionType.MERGE
                    if assessment.allowed
                    else IdentityDecisionType.REOPEN
                ),
                subject_work_ids=(existing_work.id, found.id),
                supporting_assertion_ids=(assertion_id,),
                before={"works": [str(existing_work.id), str(found.id)]},
                after=assessment.as_decision_payload(),
                reason_code=assessment.reason_code,
                decided_by_type="automatic_rule",
                decided_at=context.now(),
            )
            await uow.works.add_decision(decision)
            if assessment.allowed:
                found.identity_status = IdentityState.MERGED
                found.merged_into_work_id = existing_work.id
                await uow.works.update(found)
                report.automatic_merges += 1
            else:
                existing_work.identity_status = IdentityState.CONFLICT
                found.identity_status = IdentityState.CONFLICT
                await uow.works.update(existing_work)
                await uow.works.update(found)
                report.conflicts += 1

        if existing_work is None:
            work = Work(
                id=context.new_id(),
                workspace_id=workspace_id,
                work_kind=kind,
                identity_status=(
                    IdentityState.RESOLVED
                    if any(s.is_strong for s, _, _ in identifiers)
                    else IdentityState.UNRESOLVED
                ),
                created_at=context.now(),
            )
            await uow.works.add(work)
            index.works[work.id] = work
            report.works_created += 1
        else:
            work = existing_work

        for scheme, value, assertion_id in identifiers:
            if index.has(work.id, scheme.value, value):
                continue
            conflicting = await index.find(uow, workspace_id, scheme.value, value)
            if conflicting is not None and conflicting.id != work.id:
                work.identity_status = IdentityState.CONFLICT
                await uow.works.update(work)
                report.conflicts += 1
                continue
            identifier = WorkIdentifier(
                id=context.new_id(),
                workspace_id=workspace_id,
                work_id=work.id,
                scheme=scheme,
                value_normalized=value,
                value_display=value,
                assertion_id=assertion_id,
                confidence=1.0 if scheme.is_strong else 0.5,
            )
            await uow.works.add_identifier(identifier)
            index.add(identifier)

        unassigned = [a for a in assertions if a.work_id != work.id]
        if unassigned:
            await uow.observations.assign_assertions(
                workspace_id, [a.id for a in unassigned], work.id
            )
            for assertion in unassigned:
                assertion.work_id = work.id

        relevance, relevance_reason = _score_relevance(metadata, scope)
        # Only a change is a decision: recording "still unreviewed" once per
        # record, on every pass, filled the log with tens of thousands of rows.
        if work.relevance is RelevanceState.UNREVIEWED and relevance is not work.relevance:
            work.relevance = relevance
            work.canonical_metadata_version += 1
            await uow.works.update(work)
            await uow.works.add_decision(
                IdentityDecision(
                    id=context.new_id(),
                    workspace_id=workspace_id,
                    decision_type=IdentityDecisionType.SET_RELEVANCE,
                    subject_work_ids=(work.id,),
                    after={"relevance": relevance.value},
                    reason_code=relevance_reason,
                    decided_by_type="automatic_rule",
                    decided_at=context.now(),
                )
            )
        if relevance is RelevanceState.INCLUDED:
            report.relevance_included += 1
        elif relevance is RelevanceState.EXCLUDED:
            report.relevance_excluded += 1
        else:
            report.relevance_review += 1

        title = metadata.get(Predicate.TITLE.value)
        if isinstance(title, str) and title and work.id not in titles_by_work:
            year = metadata.get(Predicate.YEAR.value)
            first_author = None
            authors = metadata.get("authors")
            if isinstance(authors, list) and authors:
                first = authors[0]
                first_author = first.get("last_name") if isinstance(first, dict) else str(first)
            titles_by_work[work.id] = (
                work.id,
                title,
                int(year) if isinstance(year, int) else None,
                first_author,
            )


class _IdentityIndex:
    """The workspace's identifiers, in memory, for one reconciliation pass.

    Mirrors what ``find_by_identifier`` and ``list_identifiers`` would answer,
    and is kept current as the pass adds identifiers and works, so the pass asks
    the database only for works it has not met yet.
    """

    def __init__(self) -> None:
        #: (scheme, value) -> the work holding it as an active identifier.
        self.active: dict[tuple[str, str], UUID] = {}
        #: work -> every (scheme, value) it holds, in any status.
        self.by_work: dict[UUID, set[tuple[str, str]]] = defaultdict(set)
        self.works: dict[UUID, Work] = {}

    @classmethod
    async def load(cls, uow: Any, workspace_id: UUID) -> _IdentityIndex:
        index = cls()
        for identifier in await collect_pages(
            lambda after, size: uow.works.list_all_identifiers(
                workspace_id, limit=size, after_id=after
            ),
            page_size=5_000,
        ):
            index.add(identifier)
        return index

    def add(self, identifier: WorkIdentifier) -> None:
        key = (identifier.scheme.value, identifier.value_normalized)
        self.by_work[identifier.work_id].add(key)
        if identifier.status == "active":
            self.active[key] = identifier.work_id

    def has(self, work_id: UUID, scheme: str, value: str) -> bool:
        return (scheme, value) in self.by_work.get(work_id, ())

    async def work(self, uow: Any, workspace_id: UUID, work_id: UUID) -> Work | None:
        work = self.works.get(work_id)
        if work is None:
            work = await uow.works.get(workspace_id, work_id)
            if work is not None:
                self.works[work_id] = work
        return work

    async def find(self, uow: Any, workspace_id: UUID, scheme: str, value: str) -> Work | None:
        """The active work holding this identifier, as ``find_by_identifier`` says."""
        work_id = self.active.get((scheme, value))
        if work_id is None:
            return None
        work = await self.work(uow, workspace_id, work_id)
        return work if work is not None and work.is_active else None

    async def resolved_work(
        self, uow: Any, workspace_id: UUID, work_ids: list[UUID]
    ) -> Work | None:
        """The active work these assertions already point at, following merges."""
        for work_id in sorted(set(work_ids), key=str):
            work = await self.work(uow, workspace_id, work_id)
            for _ in range(_MAX_MERGE_CHAIN):
                if work is None or work.merged_into_work_id is None:
                    break
                work = await self.work(uow, workspace_id, work.merged_into_work_id)
            if work is not None and work.is_active:
                return work
        return None


def duplicate_row(
    decision: IdentityDecision, *, status: DuplicateStatus | None = None
) -> DuplicateCandidate:
    """The review-queue row for a duplicate proposal decision."""
    features = dict((decision.after or {}).get("features") or {})
    left, right = decision.subject_work_ids[0], decision.subject_work_ids[1]
    return DuplicateCandidate(
        id=decision.id,
        workspace_id=decision.workspace_id,
        left_work_id=left,
        right_work_id=right,
        similarity=float((decision.after or {}).get("similarity", 0.0)),
        year_gap=features.get("year_gap"),
        first_author_match=features.get("first_author_match"),
        status=status or DuplicateStatus.OPEN,
        created_at=decision.decided_at,
    )


def _identifier_assertions(
    assertions: list[SourceAssertion],
) -> list[tuple[IdentifierScheme, str, UUID]]:
    out: list[tuple[IdentifierScheme, str, UUID]] = []
    for assertion in assertions:
        if assertion.predicate is not Predicate.IDENTIFIER:
            continue
        payload = assertion.normalised_value_json or assertion.value_json
        if not isinstance(payload, dict):
            continue
        try:
            scheme = IdentifierScheme(str(payload.get("scheme")))
            value = normalise_identifier(scheme, str(payload.get("value", "")))
        except ValueError:
            continue
        out.append((scheme, value, assertion.id))
    return out


def _work_kind(
    metadata: dict[str, Any], identifiers: list[tuple[IdentifierScheme, str, UUID]]
) -> WorkKind:
    """Derive resource kind from assertions, not from a DOI prefix table.

    A DOI alone does not imply a resource kind (data model §7.2); the asserted
    type and the registration ecosystem do.
    """
    asserted = metadata.get(Predicate.RESOURCE_KIND.value)
    if isinstance(asserted, str):
        mapping = {
            "journal-article": WorkKind.ARTICLE,
            "article": WorkKind.ARTICLE,
            "posted-content": WorkKind.PREPRINT,
            "preprint": WorkKind.PREPRINT,
            "dataset": WorkKind.DATASET,
            "software": WorkKind.SOFTWARE,
            "protocol": WorkKind.PROTOCOL,
            "thesis": WorkKind.THESIS,
            "report": WorkKind.REPORT,
            "book-chapter": WorkKind.BOOK_CHAPTER,
            "proceedings-article": WorkKind.CONFERENCE_ITEM,
        }
        if asserted in mapping:
            return mapping[asserted]
    schemes = {scheme for scheme, _, _ in identifiers}
    if IdentifierScheme.BIORXIV_DOI in schemes or IdentifierScheme.ARXIV in schemes:
        return WorkKind.PREPRINT
    if IdentifierScheme.DATACITE_DOI in schemes:
        return WorkKind.DATASET
    if IdentifierScheme.PMID in schemes or IdentifierScheme.PMCID in schemes:
        return WorkKind.ARTICLE
    return WorkKind.UNKNOWN


def _score_relevance(metadata: dict[str, Any], scope: Any) -> tuple[RelevanceState, str]:
    """The scope's screening rules applied to one record (``domain/screening.py``).

    Deliberately explainable rather than a black box, and it never resolves
    "uncertain" to "included" on its own (audit finding H-3).
    """
    return score_for_scope(relevance_text(metadata), scope, has_abstract=has_abstract(metadata))


class IdentityService:
    """Human adjudication: merge, split, link versions, reject, set relevance."""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def list_conflicts(self, actor: ActorContext) -> list[Work]:
        """Every unresolved identity conflict.

        A truncated conflict queue is worse than a long one: the works past the cut
        stay unresolved, and nothing in the interface says they exist.
        """
        async with self._context.uow_factory() as uow:
            return await collect_pages(
                lambda after, size: uow.works.list_works(
                    actor.workspace_id,
                    identity_status=IdentityState.CONFLICT.value,
                    limit=size,
                    after_id=after,
                )
            )

    async def count_conflicts(self, actor: ActorContext) -> int:
        async with self._context.uow_factory() as uow:
            return await uow.works.count_works(
                actor.workspace_id, identity_status=IdentityState.CONFLICT.value
            )

    async def list_duplicate_proposals(self, actor: ActorContext) -> list[DuplicateProposal]:
        """Every pair waiting for duplicate review."""
        proposals: list[DuplicateProposal] = []
        cursor: UUID | None = None
        while True:
            page, cursor, _total = await self.duplicate_proposal_page(
                actor, limit=5_000, after_id=cursor
            )
            proposals.extend(page)
            if cursor is None:
                return proposals

    async def duplicate_proposal_page(
        self, actor: ActorContext, *, limit: int, after_id: UUID | None = None
    ) -> tuple[list[DuplicateProposal], UUID | None, int]:
        """One page of the duplicate-review queue, the cursor to the next, and its size.

        A run of tens of thousands of records can queue thousands of pairs; the
        collections page reads them a page at a time rather than all at once.
        """
        async with self._context.uow_factory() as uow:
            decisions = await uow.works.list_decisions_page(
                actor.workspace_id,
                reason_code=DUPLICATE_PROPOSAL_REASON,
                limit=limit,
                after_id=after_id,
            )
            total = await uow.works.count_decisions(
                actor.workspace_id, reason_code=DUPLICATE_PROPOSAL_REASON
            )
        proposals = [
            DuplicateProposal(
                left_work_id=decision.subject_work_ids[0],
                right_work_id=decision.subject_work_ids[1],
                similarity=float(decision.after.get("similarity", 0.0)),
                features=dict(decision.after.get("features", {})),
                reason_code=decision.reason_code or DUPLICATE_PROPOSAL_REASON,
            )
            for decision in decisions
            if decision.after.get("proposal") == "duplicate_review"
            and len(decision.subject_work_ids) == 2
        ]
        next_cursor = decisions[-1].id if len(decisions) == limit else None
        return proposals, next_cursor, total

    async def merge(
        self,
        actor: ActorContext,
        *,
        keep_work_id: UUID,
        merge_work_id: UUID,
        reason_code: str,
        note: str | None = None,
    ) -> IdentityDecision:
        """Human merge. Records the assessment even when it overrides it."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.DECIDE_IDENTITY)
        context = self._context
        async with context.uow_factory() as uow:
            keep = await uow.works.get(actor.workspace_id, keep_work_id)
            merge = await uow.works.get(actor.workspace_id, merge_work_id)
            if keep is None or merge is None:
                raise NotFound("both works must exist in this workspace")
            decision = await self.merge_in(
                uow, actor, keep=keep, merge=merge, reason_code=reason_code, note=note
            )
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.IDENTITY_DECIDED,
                    subject_type="work",
                    subject_id=keep.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=reason_code,
                    metadata={
                        "decision": "merge",
                        "merged_work_id": str(merge.id),
                        "override": bool(decision.after.get("human_override")),
                    },
                )
            )
            await record_step(
                uow,
                context,
                workspace_id=actor.workspace_id,
                kind=ReviewStepKind.MERGED,
                actor_id=actor.user_id,
                work_ids=(keep.id, merge.id),
                subject_id=decision.id,
                rule={"reason": reason_code},
                note=note,
            )
            await uow.commit()
        return decision

    async def merge_in(
        self,
        uow: Any,
        actor: ActorContext,
        *,
        keep: Work,
        merge: Work,
        reason_code: str,
        note: str | None = None,
        rule: dict[str, Any] | None = None,
    ) -> IdentityDecision:
        """Merge ``merge`` into ``keep`` inside the caller's transaction; no audit row.

        Identifiers and assertions move to the kept work, the duplicate-review
        queue follows them (the pair itself is resolved; any other pair that named
        the merged work now names the kept one), and the kept work's summary is
        rebuilt. A work already merged cannot be merged again.
        """
        context = self._context
        if keep.id == merge.id:
            raise ValidationFailure("a work cannot be merged into itself")
        for work in (keep, merge):
            if not work.is_active:
                raise StateTransitionError(
                    f"work {work.id} was already merged into {work.merged_into_work_id}"
                )
        keep_ids = await uow.works.list_identifiers(actor.workspace_id, keep.id)
        merge_ids = await uow.works.list_identifiers(actor.workspace_id, merge.id)
        assessment = assess_merge(
            left=keep, right=merge, left_identifiers=keep_ids, right_identifiers=merge_ids
        )
        merge.identity_status = IdentityState.MERGED
        merge.merged_into_work_id = keep.id
        keep.identity_status = IdentityState.RESOLVED
        await uow.works.update(merge)
        await uow.works.update(keep)
        for identifier in merge_ids:
            identifier.work_id = keep.id
            await uow.works.update_identifier(identifier)
        moved = await uow.observations.list_assertions(actor.workspace_id, work_id=merge.id)
        await uow.observations.assign_assertions(actor.workspace_id, [a.id for a in moved], keep.id)
        now = context.now()
        changed = []
        for row in await uow.works.list_duplicate_candidates(actor.workspace_id, work_id=merge.id):
            pair = {row.left_work_id, row.right_work_id}
            if row.left_work_id == merge.id:
                row.left_work_id = keep.id
            if row.right_work_id == merge.id:
                row.right_work_id = keep.id
            if row.left_work_id == row.right_work_id and row.status is DuplicateStatus.OPEN:
                # This pair, or one that only joined these two works, is answered.
                row.status = (
                    DuplicateStatus.MERGED
                    if pair == {keep.id, merge.id}
                    else DuplicateStatus.SUPERSEDED
                )
                row.resolved_by = actor.user_id
                row.resolved_at = now
            changed.append(row)
        await uow.works.update_duplicate_candidates(changed)
        decision = IdentityDecision(
            id=context.new_id(),
            workspace_id=actor.workspace_id,
            decision_type=IdentityDecisionType.MERGE,
            subject_work_ids=(keep.id, merge.id),
            before={"keep": str(keep.id), "merged": str(merge.id)},
            after={
                "automatic_assessment": assessment.as_decision_payload(),
                "human_override": not assessment.allowed,
                **({"rule": rule} if rule else {}),
            },
            reason_code=reason_code,
            note=note,
            decided_by=actor.user_id,
            decided_by_type="human",
            decided_at=now,
        )
        await uow.works.add_decision(decision)
        await refresh_summaries(uow, actor.workspace_id, [keep.id], now=now)
        return decision

    async def link_versions(
        self,
        actor: ActorContext,
        *,
        preprint_work_id: UUID,
        version_of_record_work_id: UUID,
        reason_code: str = "preprint_of_version_of_record",
    ) -> WorkRelation:
        """Link a preprint to its version of record *without* merging them."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.DECIDE_IDENTITY)
        context = self._context
        async with context.uow_factory() as uow:
            preprint = await uow.works.get(actor.workspace_id, preprint_work_id)
            record = await uow.works.get(actor.workspace_id, version_of_record_work_id)
            if preprint is None or record is None:
                raise NotFound("both works must exist in this workspace")
            relation = WorkRelation(
                id=context.new_id(),
                workspace_id=actor.workspace_id,
                from_work_id=preprint.id,
                to_work_id=record.id,
                kind=RelationKind.IS_PREPRINT_OF,
                decided_by=actor.user_id,
                created_at=context.now(),
            )
            await uow.works.add_relation(relation)
            await uow.works.add_decision(
                IdentityDecision(
                    id=context.new_id(),
                    workspace_id=actor.workspace_id,
                    decision_type=IdentityDecisionType.LINK_VERSION,
                    subject_work_ids=(preprint.id, record.id),
                    after={"relation": RelationKind.IS_PREPRINT_OF.value},
                    reason_code=reason_code,
                    decided_by=actor.user_id,
                    decided_by_type="human",
                    decided_at=context.now(),
                )
            )
            await uow.commit()
        return relation

    async def set_relevance(
        self,
        actor: ActorContext,
        *,
        work_id: UUID,
        relevance: RelevanceState,
        reason_code: str,
        note: str | None = None,
    ) -> Work:
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.DECIDE_IDENTITY)
        context = self._context
        async with context.uow_factory() as uow:
            work = await uow.works.get(actor.workspace_id, work_id)
            if work is None:
                raise NotFound(f"work {work_id} not found")
            before = work.relevance
            work.relevance = relevance
            work.version += 1
            await uow.works.update(work)
            await uow.works.add_decision(
                IdentityDecision(
                    id=context.new_id(),
                    workspace_id=actor.workspace_id,
                    decision_type=IdentityDecisionType.SET_RELEVANCE,
                    subject_work_ids=(work.id,),
                    before={"relevance": before.value},
                    after={"relevance": relevance.value},
                    reason_code=reason_code,
                    note=note,
                    decided_by=actor.user_id,
                    decided_by_type="human",
                    decided_at=context.now(),
                )
            )
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEVANCE_DECIDED,
                    subject_type="work",
                    subject_id=work.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code=reason_code,
                    metadata={"from": before.value, "to": relevance.value},
                )
            )
            await record_step(
                uow,
                context,
                workspace_id=actor.workspace_id,
                kind=ReviewStepKind.RELEVANCE_SET,
                actor_id=actor.user_id,
                work_ids=(work.id,),
                rule={"reason": reason_code, "from": before.value, "relevance": relevance.value},
                note=note,
            )
            await uow.commit()
        return work

    async def resolve_conflict(
        self,
        actor: ActorContext,
        *,
        work_id: UUID,
        keep_identifier: tuple[str, str] | None,
        reason_code: str,
        note: str | None = None,
    ) -> Work:
        """Resolve an identity conflict by retiring the losing identifier."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.DECIDE_IDENTITY)
        context = self._context
        async with context.uow_factory() as uow:
            work = await uow.works.get(actor.workspace_id, work_id)
            if work is None:
                raise NotFound(f"work {work_id} not found")
            identifiers = await uow.works.list_identifiers(actor.workspace_id, work_id)
            if keep_identifier is not None:
                scheme, value = keep_identifier
                if not any(
                    i.scheme.value == scheme and i.value_normalized == value for i in identifiers
                ):
                    raise ValidationFailure("kept identifier is not attached to this work")
                for identifier in identifiers:
                    if identifier.scheme.value == scheme and identifier.value_normalized == value:
                        continue
                    if identifier.scheme.value != scheme:
                        continue
                    identifier.status = "retired"
                    await uow.works.update_identifier(identifier)
            work.identity_status = IdentityState.RESOLVED
            work.version += 1
            await uow.works.update(work)
            await uow.works.add_decision(
                IdentityDecision(
                    id=context.new_id(),
                    workspace_id=actor.workspace_id,
                    decision_type=IdentityDecisionType.RESOLVE_FIELD,
                    subject_work_ids=(work.id,),
                    after={"kept_identifier": list(keep_identifier) if keep_identifier else None},
                    reason_code=reason_code,
                    note=note,
                    decided_by=actor.user_id,
                    decided_by_type="human",
                    decided_at=context.now(),
                )
            )
            await uow.commit()
        return work
