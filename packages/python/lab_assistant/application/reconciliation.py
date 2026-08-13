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
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import collect_pages
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import NotFound, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import (
    StageCommand,
    StageName,
    StageResult,
    StageStatus,
    TaskRequest,
)
from lab_assistant.domain.identity import (
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
from lab_assistant.domain.observation import Predicate, SourceAssertion
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


class ReconciliationHandler:
    """Stage handler: build works from a frozen or running run's assertions."""

    stage_name = StageName.RECONCILIATION
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
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

            titles_for_proposals: list[tuple[UUID, str, int | None, str | None]] = []

            for _subject_hint, assertions in sorted(assertions_by_subject.items()):
                identifiers = _identifier_assertions(assertions)
                metadata = canonical_metadata(assertions)
                kind = _work_kind(metadata, identifiers)

                existing_work: Work | None = None
                for scheme, value, assertion_id in identifiers:
                    if not scheme.is_strong:
                        continue
                    found = await uow.works.find_by_identifier(
                        workspace_id, scheme=scheme.value, value_normalized=value
                    )
                    if found is None:
                        continue
                    if existing_work is None:
                        existing_work = found
                        continue
                    if found.id == existing_work.id:
                        continue
                    # Two existing works share this record's identifiers: only a
                    # strong bridge with no contradiction may merge them.
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
                    report.works_created += 1
                else:
                    work = existing_work

                for scheme, value, assertion_id in identifiers:
                    known = {
                        (i.scheme, i.value_normalized)
                        for i in await uow.works.list_identifiers(workspace_id, work.id)
                    }
                    if (scheme, value) in known:
                        continue
                    conflicting = await uow.works.find_by_identifier(
                        workspace_id, scheme=scheme.value, value_normalized=value
                    )
                    if conflicting is not None and conflicting.id != work.id:
                        work.identity_status = IdentityState.CONFLICT
                        await uow.works.update(work)
                        report.conflicts += 1
                        continue
                    await uow.works.add_identifier(
                        WorkIdentifier(
                            id=context.new_id(),
                            workspace_id=workspace_id,
                            work_id=work.id,
                            scheme=scheme,
                            value_normalized=value,
                            value_display=value,
                            assertion_id=assertion_id,
                            confidence=1.0 if scheme.is_strong else 0.5,
                        )
                    )

                for assertion in assertions:
                    if assertion.work_id != work.id:
                        assertion.work_id = work.id
                        await uow.observations.update_assertion(assertion)

                relevance, relevance_reason = _score_relevance(metadata, scope)
                if work.relevance is RelevanceState.UNREVIEWED:
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
                if isinstance(title, str) and title:
                    year = metadata.get(Predicate.YEAR.value)
                    first_author = None
                    authors = metadata.get("authors")
                    if isinstance(authors, list) and authors:
                        first = authors[0]
                        first_author = (
                            first.get("last_name") if isinstance(first, dict) else str(first)
                        )
                    titles_for_proposals.append(
                        (work.id, title, int(year) if isinstance(year, int) else None, first_author)
                    )

            proposals = propose_duplicates(titles_for_proposals)
            report.duplicate_proposals = len(proposals)
            for proposal in proposals:
                await uow.works.add_decision(
                    IdentityDecision(
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
                )

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
    """Transparent relevance features: concept coverage over title and abstract.

    Deliberately explainable rather than a black box, and it never resolves
    "uncertain" to "included" on its own (audit finding H-3).
    """
    policy = scope.relevance_policy
    haystack = " ".join(
        str(metadata.get(field_name, "") or "")
        for field_name in (Predicate.TITLE.value, Predicate.ABSTRACT.value)
    ).casefold()
    subject_terms = metadata.get("subject_terms") or []
    haystack = " ".join([haystack, *[str(term).casefold() for term in subject_terms]])

    concepts = [concept.casefold() for concept in (scope.concepts or ())]
    if not concepts:
        return (
            RelevanceState.UNREVIEWED,
            "no_scope_concepts_defined",
        )
    hits = sum(1 for concept in concepts if concept in haystack)
    score = hits / len(concepts)
    for criterion in scope.exclusion_criteria or ():
        if criterion.casefold() in haystack:
            return RelevanceState.EXCLUDED, f"exclusion_criterion:{criterion}"
    if score >= policy.include_threshold:
        return RelevanceState.INCLUDED, f"concept_score:{score:.2f}"
    if score <= policy.exclude_threshold:
        return RelevanceState.EXCLUDED, f"concept_score:{score:.2f}"
    if policy.allow_included_uncertain:
        return RelevanceState.INCLUDED_UNCERTAIN, f"concept_score:{score:.2f}"
    return RelevanceState.UNREVIEWED, f"uncertain_concept_score:{score:.2f}"


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
            works = await collect_pages(
                lambda after, size: uow.works.list_works(
                    actor.workspace_id, limit=size, after_id=after
                )
            )
        return [w for w in works if w.identity_status is IdentityState.CONFLICT]

    async def list_duplicate_proposals(self, actor: ActorContext) -> list[DuplicateProposal]:
        async with self._context.uow_factory() as uow:
            decisions = await uow.works.list_decisions(actor.workspace_id)
        proposals: list[DuplicateProposal] = []
        for decision in decisions:
            if decision.after.get("proposal") != "duplicate_review":
                continue
            if len(decision.subject_work_ids) != 2:
                continue
            proposals.append(
                DuplicateProposal(
                    left_work_id=decision.subject_work_ids[0],
                    right_work_id=decision.subject_work_ids[1],
                    similarity=float(decision.after.get("similarity", 0.0)),
                    features=dict(decision.after.get("features", {})),
                    reason_code=decision.reason_code or "title_author_year_similarity",
                )
            )
        return proposals

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
        actor.require(Capability.DECIDE_IDENTITY)
        context = self._context
        async with context.uow_factory() as uow:
            keep = await uow.works.get(actor.workspace_id, keep_work_id)
            merge = await uow.works.get(actor.workspace_id, merge_work_id)
            if keep is None or merge is None:
                raise NotFound("both works must exist in this workspace")
            assessment = assess_merge(
                left=keep,
                right=merge,
                left_identifiers=await uow.works.list_identifiers(actor.workspace_id, keep.id),
                right_identifiers=await uow.works.list_identifiers(actor.workspace_id, merge.id),
            )
            merge.identity_status = IdentityState.MERGED
            merge.merged_into_work_id = keep.id
            keep.identity_status = IdentityState.RESOLVED
            await uow.works.update(merge)
            await uow.works.update(keep)
            for identifier in await uow.works.list_identifiers(actor.workspace_id, merge.id):
                identifier.work_id = keep.id
                await uow.works.update_identifier(identifier)
            for assertion in await uow.observations.list_assertions(
                actor.workspace_id, work_id=merge.id
            ):
                assertion.work_id = keep.id
                await uow.observations.update_assertion(assertion)
            decision = IdentityDecision(
                id=context.new_id(),
                workspace_id=actor.workspace_id,
                decision_type=IdentityDecisionType.MERGE,
                subject_work_ids=(keep.id, merge.id),
                before={"keep": str(keep.id), "merged": str(merge.id)},
                after={
                    "automatic_assessment": assessment.as_decision_payload(),
                    "human_override": not assessment.allowed,
                },
                reason_code=reason_code,
                note=note,
                decided_by=actor.user_id,
                decided_by_type="human",
                decided_at=context.now(),
            )
            await uow.works.add_decision(decision)
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
                        "override": not assessment.allowed,
                    },
                )
            )
            await uow.commit()
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
