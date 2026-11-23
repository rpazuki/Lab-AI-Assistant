"""Reviewing a large collection by rule: bulk merge, bulk include and exclude.

Every bulk action is two calls with the same arguments: a preview that changes
nothing and says exactly what would happen (how many, which ones, and for a merge
every pair it would skip and why), then the apply. The apply works in bounded
chunks and reports what is left, so a request never outlives a proxy timeout;
the page repeats it until nothing remains. Re-planning on every call makes the
repeat safe: a pair merged by the previous chunk is simply no longer open.

Each decision a rule makes carries the rule, and each chunk writes one audit
event with its counts, so "who excluded this, and why" has an answer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import collect_pages
from lab_assistant.application.reconciliation import (
    IdentityService,
    duplicate_row,
    refresh_summaries,
)
from lab_assistant.contracts.errors import NotFound, StateTransitionError
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.domain.identity import (
    DUPLICATE_PROPOSAL_REASON,
    IdentityDecision,
    IdentityDecisionType,
    Work,
    kinds_compatible,
)
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.review import (
    CandidateFilter,
    DuplicateCandidate,
    DuplicateStatus,
    MergeRule,
    WorkSummary,
)
from lab_assistant.domain.states import RelevanceState
from lab_assistant.domain.workspace import Capability

__all__ = ["SKIP_REASONS", "BulkReviewService", "MergePlan", "paper"]

#: Why a bulk merge leaves a pair alone, in the order the guards are checked.
SKIP_REASONS: dict[str, str] = {
    "below_threshold": "title similarity is below the threshold",
    "already_merged": "one side was merged by another decision",
    "unknown_year": "one record has no year",
    "different_year": "the two records give different years",
    "different_authors": "the first authors differ",
    "unknown_authors": "a first author is missing",
    "different_kind": "different kinds of work, such as an article and a dataset",
    "different_doi": "each has its own DOI: two publications, not one",
    "generic_title": "the title pairs with many others (Editorial, Correction)",
    "group_too_large": "it would merge more works at once than the rule allows",
}

#: How many merge groups, or relevance changes, one apply call makes.
MERGE_CHUNK = 200
RELEVANCE_CHUNK = 1_000


@dataclass(slots=True)
class MergePlan:
    """What a merge rule would do now: groups to merge, and every pair it skips."""

    #: Each group's work ids; which one is kept is decided when it is applied.
    groups: list[list[UUID]] = field(default_factory=list)
    #: The open pairs each group answers.
    group_pairs: list[list[DuplicateCandidate]] = field(default_factory=list)
    considered: int = 0
    accepted_pairs: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    skipped_examples: dict[str, list[DuplicateCandidate]] = field(default_factory=dict)

    @property
    def works_merged(self) -> int:
        return sum(len(group) - 1 for group in self.groups)

    def skip(self, reason: str, rows: list[DuplicateCandidate]) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + len(rows)
        examples = self.skipped_examples.setdefault(reason, [])
        examples.extend(rows[: max(0, 3 - len(examples))])


class _Groups:
    """Union-find over work ids: pairs that share a work become one group."""

    def __init__(self) -> None:
        self._parent: dict[UUID, UUID] = {}

    def find(self, item: UUID) -> UUID:
        parent = self._parent.setdefault(item, item)
        if parent != item:
            parent = self._parent[item] = self.find(parent)
        return parent

    def join(self, left: UUID, right: UUID) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self._parent[max(a, b, key=str)] = min(a, b, key=str)


class BulkReviewService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._identity = IdentityService(context)

    def _require(self, actor: ActorContext) -> None:
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.DECIDE_IDENTITY)

    # ── candidates ──────────────────────────────────────────────────────────
    async def candidates(
        self, actor: ActorContext, rule: CandidateFilter, *, limit: int, after_id: UUID | None
    ) -> tuple[list[Work], int]:
        async with self._context.uow_factory() as uow:
            works = await uow.works.filter_works(
                actor.workspace_id, rule, limit=limit, after_id=after_id
            )
            total = await uow.works.count_filtered(actor.workspace_id, rule)
        return works, total

    async def facets(self, actor: ActorContext) -> dict[str, Any]:
        async with self._context.uow_factory() as uow:
            return await uow.works.summary_facets(actor.workspace_id)

    async def bulk_relevance(
        self,
        actor: ActorContext,
        rule: CandidateFilter,
        *,
        relevance: RelevanceState,
        only_unreviewed: bool = True,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Set one relevance on every candidate the filter matches.

        With ``only_unreviewed`` (the default) a candidate someone has already
        decided keeps that decision, so a broad rule cannot undo careful one-by-one
        work. Without ``apply`` nothing changes and the answer is the preview.
        One apply changes at most ``RELEVANCE_CHUNK``; ``remaining`` says how many
        still match.
        """
        self._require(actor)
        # Never matching what is already at the target is what lets an apply that
        # changes RELEVANCE_CHUNK at a time count down to nothing.
        target = rule.model_copy(
            update={
                "relevance_not": relevance,
                **({"relevance": RelevanceState.UNREVIEWED} if only_unreviewed else {}),
            }
        )
        context = self._context
        async with context.uow_factory() as uow:
            total = await uow.works.count_filtered(actor.workspace_id, target)
            sample = await uow.works.filter_works(actor.workspace_id, target, limit=10)
            summaries = await uow.works.get_summaries(actor.workspace_id, [w.id for w in sample])
        answer: dict[str, Any] = {
            "matched": total,
            "sample": [paper(work.id, summaries.get(work.id), work) for work in sample],
            "applied": 0,
            "remaining": total,
        }
        if not apply or total == 0:
            return answer

        described = describe_filter(rule, only_unreviewed=only_unreviewed)
        changed = 0
        async with context.uow_factory() as uow:
            works = await uow.works.filter_works(actor.workspace_id, target, limit=RELEVANCE_CHUNK)
            now = context.now()
            for work in works:
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
                        after={"relevance": relevance.value, "rule": described},
                        reason_code="steward_rule",
                        decided_by=actor.user_id,
                        decided_by_type="human",
                        decided_at=now,
                    )
                )
                changed += 1
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.RELEVANCE_BULK_DECIDED,
                    subject_type="workspace",
                    subject_id=actor.workspace_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="steward_rule",
                    metadata={"to": relevance.value, "changed": changed, "rule": described},
                )
            )
            await uow.commit()
        async with context.uow_factory() as uow:
            remaining = await uow.works.count_filtered(actor.workspace_id, target)
        answer["applied"] = changed
        answer["remaining"] = remaining
        return answer

    # ── duplicate queue ─────────────────────────────────────────────────────
    async def duplicate_queue(
        self,
        actor: ActorContext,
        *,
        min_similarity: float | None,
        limit: int,
        after: tuple[float, UUID] | None,
    ) -> tuple[list[DuplicateCandidate], int, dict[UUID, WorkSummary]]:
        """One page of open pairs, most similar first, with both papers' summaries."""
        async with self._context.uow_factory() as uow:
            rows = await uow.works.list_duplicate_candidates(
                actor.workspace_id,
                status=DuplicateStatus.OPEN.value,
                min_similarity=min_similarity,
                limit=limit,
                after=after,
            )
            total = await uow.works.count_duplicate_candidates(
                actor.workspace_id,
                status=DuplicateStatus.OPEN.value,
                min_similarity=min_similarity,
            )
            ids = {w for row in rows for w in (row.left_work_id, row.right_work_id)}
            summaries = await uow.works.get_summaries(actor.workspace_id, list(ids))
        return rows, total, summaries

    async def dismiss(self, actor: ActorContext, candidate_id: UUID) -> DuplicateCandidate:
        """These two are different papers: the pair leaves the queue, on the record."""
        self._require(actor)
        context = self._context
        async with context.uow_factory() as uow:
            row = await self._open_row(uow, actor, candidate_id)
            row.status = DuplicateStatus.DISMISSED
            row.resolved_by = actor.user_id
            row.resolved_at = context.now()
            await uow.works.update_duplicate_candidates([row])
            await uow.works.add_decision(
                IdentityDecision(
                    id=context.new_id(),
                    workspace_id=actor.workspace_id,
                    decision_type=IdentityDecisionType.REJECT_LINK,
                    subject_work_ids=(row.left_work_id, row.right_work_id),
                    after={"proposal": str(row.id), "verdict": "different_works"},
                    reason_code="reviewer_not_same_work",
                    decided_by=actor.user_id,
                    decided_by_type="human",
                    decided_at=row.resolved_at,
                )
            )
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.DUPLICATE_DISMISSED,
                    subject_type="duplicate_candidate",
                    subject_id=row.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="reviewer_not_same_work",
                )
            )
            await uow.commit()
        return row

    async def merge_pair(self, actor: ActorContext, candidate_id: UUID) -> IdentityDecision:
        """Merge one open pair, keeping the work with more identifiers."""
        self._require(actor)
        async with self._context.uow_factory() as uow:
            row = await self._open_row(uow, actor, candidate_id)
            left = await uow.works.get(actor.workspace_id, row.left_work_id)
            right = await uow.works.get(actor.workspace_id, row.right_work_id)
            if left is None or right is None:
                raise NotFound("both works must exist in this workspace")
            keep, rest = await _keep_first(uow, actor.workspace_id, [left, right])
        return await self._identity.merge(
            actor,
            keep_work_id=keep.id,
            merge_work_id=rest[0].id,
            reason_code="reviewer_confirmed_same_work",
        )

    @staticmethod
    async def _open_row(uow: Any, actor: ActorContext, candidate_id: UUID) -> DuplicateCandidate:
        row = await uow.works.get_duplicate_candidate(actor.workspace_id, candidate_id)
        if row is None:
            raise NotFound(f"duplicate pair {candidate_id} not found")
        if row.status is not DuplicateStatus.OPEN:
            raise StateTransitionError(f"this pair was already {row.status.value}")
        return row

    # ── bulk merge ──────────────────────────────────────────────────────────
    async def plan_merge(self, actor: ActorContext, rule: MergeRule) -> MergePlan:
        """Which open pairs the rule merges, as groups, and why it skips the others."""
        async with self._context.uow_factory() as uow:
            rows = await uow.works.list_duplicate_candidates(
                actor.workspace_id, status=DuplicateStatus.OPEN.value
            )
            works = {
                w.id: w
                for w in await collect_pages(
                    lambda after, size: uow.works.list_works(
                        actor.workspace_id, limit=size, after_id=after
                    ),
                    page_size=5_000,
                )
            }
            dois: dict[UUID, set[str]] = defaultdict(set)
            for identifier in await collect_pages(
                lambda after, size: uow.works.list_all_identifiers(
                    actor.workspace_id, limit=size, after_id=after
                ),
                page_size=5_000,
            ):
                if identifier.status == "active" and identifier.scheme.value.endswith("doi"):
                    dois[identifier.work_id].add(identifier.value_normalized)

        pairs_per_work: dict[UUID, int] = defaultdict(int)
        for row in rows:
            pairs_per_work[row.left_work_id] += 1
            pairs_per_work[row.right_work_id] += 1

        plan = MergePlan(considered=len(rows))
        accepted: list[DuplicateCandidate] = []
        for row in rows:
            reason = _skip_reason(row, rule, works, dois, pairs_per_work)
            if reason:
                plan.skip(reason, [row])
            else:
                accepted.append(row)

        groups = _Groups()
        for row in accepted:
            groups.join(row.left_work_id, row.right_work_id)
        pairs: dict[UUID, list[DuplicateCandidate]] = defaultdict(list)
        for row in accepted:
            pairs[groups.find(row.left_work_id)].append(row)
        kept: list[tuple[list[UUID], list[DuplicateCandidate]]] = []
        for group_rows in pairs.values():
            ids = sorted(
                {w for row in group_rows for w in (row.left_work_id, row.right_work_id)}, key=str
            )
            if len(ids) > rule.max_group_size:
                plan.skip("group_too_large", group_rows)
                continue
            kept.append((ids, group_rows))
            plan.accepted_pairs += len(group_rows)
        # Most similar first, so a partial apply takes the safest groups.
        kept.sort(key=lambda item: (-max(r.similarity for r in item[1]), str(item[0][0])))
        plan.groups = [ids for ids, _ in kept]
        plan.group_pairs = [group_rows for _, group_rows in kept]
        return plan

    async def apply_merge(self, actor: ActorContext, rule: MergeRule) -> dict[str, Any]:
        """Merge up to ``MERGE_CHUNK`` groups the rule accepts; report what is left."""
        self._require(actor)
        plan = await self.plan_merge(actor, rule)
        context = self._context
        chosen = plan.groups[:MERGE_CHUNK]
        merged = 0
        described = rule.model_dump(mode="json")
        async with context.uow_factory() as uow:
            for group in chosen:
                works = [await uow.works.get(actor.workspace_id, work_id) for work_id in group]
                present = [work for work in works if work is not None and work.is_active]
                if len(present) < 2:
                    continue
                keep, rest = await _keep_first(uow, actor.workspace_id, present)
                for work in rest:
                    await self._identity.merge_in(
                        uow,
                        actor,
                        keep=keep,
                        merge=work,
                        reason_code="steward_merge_rule",
                        rule=described,
                    )
                    merged += 1
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.IDENTITIES_BULK_MERGED,
                    subject_type="workspace",
                    subject_id=actor.workspace_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    reason_code="steward_merge_rule",
                    metadata={"groups": len(chosen), "works_merged": merged, "rule": described},
                )
            )
            await uow.commit()
        return {
            "groups_merged": len(chosen),
            "works_merged": merged,
            "remaining_groups": max(len(plan.groups) - len(chosen), 0),
        }

    # ── backfill ────────────────────────────────────────────────────────────
    async def backfill(self, workspace_id: UUID) -> dict[str, int]:
        """Fill the summaries and queue rows a database predating them lacks. Idempotent."""
        context = self._context
        summarised = 0
        while True:
            async with context.uow_factory() as uow:
                missing = await uow.works.works_without_summary(workspace_id, limit=500)
                if not missing:
                    break
                await refresh_summaries(uow, workspace_id, missing, now=context.now())
                await uow.commit()
            summarised += len(missing)

        async with context.uow_factory() as uow:
            known = {row.id for row in await uow.works.list_duplicate_candidates(workspace_id)}
            works = {
                w.id: w
                for w in await collect_pages(
                    lambda after, size: uow.works.list_works(
                        workspace_id, limit=size, after_id=after
                    ),
                    page_size=5_000,
                )
            }
            proposals = await collect_pages(
                lambda after, size: uow.works.list_decisions_page(
                    workspace_id,
                    reason_code=DUPLICATE_PROPOSAL_REASON,
                    limit=size,
                    after_id=after,
                ),
                page_size=5_000,
            )
            rows = []
            for decision in proposals:
                if decision.id in known or len(decision.subject_work_ids) != 2:
                    continue
                row = duplicate_row(decision)
                row.left_work_id = _surviving(works, row.left_work_id)
                row.right_work_id = _surviving(works, row.right_work_id)
                if row.left_work_id == row.right_work_id:
                    # Already joined by a merge made before the queue existed.
                    row.status = DuplicateStatus.MERGED
                rows.append(row)
            await uow.works.add_duplicate_candidates(rows)
            await uow.commit()
        return {"summarised": summarised, "queued": len(rows)}


def _skip_reason(
    row: DuplicateCandidate,
    rule: MergeRule,
    works: dict[UUID, Work],
    dois: dict[UUID, set[str]],
    pairs_per_work: dict[UUID, int],
) -> str | None:
    """The first guard of ``rule`` this pair fails, or None to merge it."""
    if row.similarity < rule.min_similarity:
        return "below_threshold"
    left, right = works.get(row.left_work_id), works.get(row.right_work_id)
    if left is None or right is None or not left.is_active or not right.is_active:
        return "already_merged"
    if rule.require_same_year:
        if row.year_gap is None:
            return "unknown_year"
        if row.year_gap != 0:
            return "different_year"
    if rule.skip_different_authors and row.first_author_match is False:
        return "different_authors"
    if rule.require_known_authors and row.first_author_match is None:
        return "unknown_authors"
    if not kinds_compatible(left.work_kind, right.work_kind):
        return "different_kind"
    left_dois, right_dois = dois.get(left.id), dois.get(right.id)
    if rule.skip_different_dois and left_dois and right_dois and not left_dois & right_dois:
        return "different_doi"
    if max(pairs_per_work[left.id], pairs_per_work[right.id]) > rule.max_pairs_per_work:
        return "generic_title"
    return None


async def _keep_first(uow: Any, workspace_id: UUID, works: list[Work]) -> tuple[Work, list[Work]]:
    """The work to keep, then the rest: most identifiers, then most sources, then id."""
    counts = {
        work.id: len(await uow.works.list_identifiers(workspace_id, work.id)) for work in works
    }
    summaries = await uow.works.get_summaries(workspace_id, [w.id for w in works])
    ordered = sorted(
        works,
        key=lambda w: (
            -counts[w.id],
            -(summaries[w.id].source_count if w.id in summaries else 0),
            str(w.id),
        ),
    )
    return ordered[0], ordered[1:]


def _surviving(works: dict[UUID, Work], work_id: UUID) -> UUID:
    """Follow merges to the work that holds this one's records now."""
    seen: set[UUID] = set()
    while (work := works.get(work_id)) is not None and work.merged_into_work_id is not None:
        if work_id in seen:
            break
        seen.add(work_id)
        work_id = work.merged_into_work_id
    return work_id


def paper(work_id: UUID, summary: WorkSummary | None, work: Work | None = None) -> dict[str, Any]:
    """What a person needs to recognise a paper in a queue or a preview."""
    out: dict[str, Any] = {
        "work_id": str(work_id),
        "title": summary.title if summary else None,
        "year": summary.year if summary else None,
        "journal": summary.journal if summary else None,
        "resource_kind": summary.resource_kind if summary else None,
        "first_author": summary.first_author if summary else None,
        "sources": summary.sources.split() if summary else [],
    }
    if work is not None:
        out["relevance"] = work.relevance.value
        out["identity_status"] = work.identity_status.value
    return out


def describe_filter(rule: CandidateFilter, *, only_unreviewed: bool) -> dict[str, Any]:
    """The filter as stored with each decision it makes: only the conditions set."""
    described = {
        key: value
        for key, value in rule.model_dump(mode="json", exclude={"relevance_not"}).items()
        if value not in (None, [], ())
    }
    described["only_unreviewed"] = only_unreviewed
    return described
