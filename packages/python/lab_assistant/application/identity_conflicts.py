"""Identity conflicts: finding them, seeing why, and settling them, one or many.

A work is in conflict when the sources disagree about which strong identifiers
(DOI, PMID, PMCID) are its own, and no rule could settle it:

* **two papers matched one record** — a record named identifiers that two
  existing papers hold, and the merge was refused (two different DOIs, say). The
  refusal is recorded as a ``reopen`` decision naming both;
* **an identifier held by another paper** — a record claimed an identifier for
  this paper that another paper already holds. The identifier stayed with the
  other paper.

A conflicted paper cannot enter a release. A person settles it by merging it
into the other paper (the same paper, recorded twice) or by keeping the two
separate, which marks it resolved where its identifiers already are. The list is
searchable by title words, and either answer can be applied to every paper a
search found, in chunks the page repeats, like every bulk action.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_assistant.application.bulk_review import paper
from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import collect_pages
from lab_assistant.application.reconciliation import IdentityService, _identifier_assertions
from lab_assistant.domain.identity import IdentityDecisionType
from lab_assistant.domain.review import _words
from lab_assistant.domain.states import IdentityState

__all__ = ["CHUNK", "SAME_PAPER", "IdentityConflictService"]

#: Conflicts kept separate per call; the page repeats until none remain.
CHUNK = 200

KEPT_SEPARATE = "steward_kept_separate"
#: The reason a steward's "same paper" merge is recorded with, one or many.
SAME_PAPER = "steward_same_paper"
#: Merged works are followed to the paper they became, at most this far.
_MAX_MERGE_CHAIN = 16


class IdentityConflictService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def page(
        self,
        actor: ActorContext,
        *,
        words: tuple[str, ...] = (),
        limit: int = 50,
        after: UUID | None = None,
    ) -> dict[str, Any]:
        """One page of conflicted papers matching the words, each with why and with whom."""
        works = works_found = await self._matching(actor, words)
        total = len(works)
        if after is not None:
            works = [w for w in works if str(w.id) > str(after)]
        shown = works[:limit]
        ws = actor.workspace_id
        async with self._context.uow_factory() as uow:
            summaries = await uow.works.get_summaries(ws, [w.id for w in shown])
            # Read once for the page, not once per paper: a refused merge names
            # both papers.
            refused = await uow.works.list_decisions(
                ws, decision_type=IdentityDecisionType.REOPEN.value
            )
            rows = []
            for work in shown:
                identifiers = await uow.works.list_identifiers(ws, work.id)
                others = await self._counterparts(uow, ws, work.id, identifiers, refused)
                other_summaries = await uow.works.get_summaries(ws, [o for o, _, _ in others])
                rows.append(
                    {
                        **paper(work.id, summaries.get(work.id), work),
                        "identifiers": [
                            {"scheme": i.scheme.value, "value": i.value_display}
                            for i in identifiers
                            if i.scheme.is_strong and i.status == "active"
                        ],
                        "with": [
                            {
                                **paper(other, other_summaries.get(other)),
                                "reason": reason,
                                "identifier": identifier,
                            }
                            for other, reason, identifier in others
                        ],
                    }
                )
        return {
            "items": rows,
            "total": total,
            # Distinct (paper, other paper) pairs across every conflict the search
            # found: what "merge each as the same paper" would merge.
            "pairs": len(await self._pairs(actor, works_found)),
            "next_cursor": str(shown[-1].id)
            if len(shown) == limit and len(works) > limit
            else None,
        }

    async def keep_separate(self, actor: ActorContext, work_id: UUID) -> None:
        """These are different papers: mark this one resolved, its identifiers as they are."""
        await IdentityService(self._context).resolve_conflict(
            actor, work_id=work_id, keep_identifier=None, reason_code=KEPT_SEPARATE
        )

    async def keep_all_separate(
        self, actor: ActorContext, *, words: tuple[str, ...] = ()
    ) -> dict[str, int]:
        """Keep up to ``CHUNK`` of the papers the search found separate; say how many remain."""
        works = await self._matching(actor, words)
        for work in works[:CHUNK]:
            await self.keep_separate(actor, work.id)
        return {"resolved": min(len(works), CHUNK), "remaining": max(0, len(works) - CHUNK)}

    async def merge_all_same_paper(
        self, actor: ActorContext, *, words: tuple[str, ...] = ()
    ) -> dict[str, int]:
        """Merge each conflict the search found with its other paper(s), ``CHUNK`` groups a call.

        Pairs that share a paper form one group (A with B, B with C), merged
        into the one with the most strong identifiers, so no merge targets a
        paper an earlier one in the group already merged away. Each merge is the
        same audited, reasoned merge as the button on a single conflict.
        """
        identity = IdentityService(self._context)
        groups = _groups(await self._pairs(actor, await self._matching(actor, words)))
        merged = 0
        for group in groups[:CHUNK]:
            keep = await self._keeper(actor, group)
            for other in sorted(group - {keep}, key=str):
                await identity.merge(
                    actor, keep_work_id=keep, merge_work_id=other, reason_code=SAME_PAPER
                )
                merged += 1
        remaining = sum(len(group) - 1 for group in groups[CHUNK:])
        return {"merged": merged, "remaining": remaining}

    async def _pairs(self, actor: ActorContext, works: list[Any]) -> list[tuple[UUID, UUID]]:
        """Each conflicted paper with each other paper, both still active, every pair once."""
        ws = actor.workspace_id
        pairs: set[tuple[UUID, UUID]] = set()
        async with self._context.uow_factory() as uow:
            refused = await uow.works.list_decisions(
                ws, decision_type=IdentityDecisionType.REOPEN.value
            )
            for work in works:
                identifiers = await uow.works.list_identifiers(ws, work.id)
                for other, _, _ in await self._counterparts(uow, ws, work.id, identifiers, refused):
                    active = await _active(uow, ws, other)
                    if active is not None and active != work.id:
                        left, right = sorted((work.id, active), key=str)
                        pairs.add((left, right))
        return sorted(pairs, key=lambda pair: (str(pair[0]), str(pair[1])))

    async def _keeper(self, actor: ActorContext, group: set[UUID]) -> UUID:
        """The paper the others merge into: the most strong identifiers, then the lowest id."""
        ws = actor.workspace_id
        async with self._context.uow_factory() as uow:
            strong = {
                work_id: sum(
                    1
                    for i in await uow.works.list_identifiers(ws, work_id)
                    if i.scheme.is_strong and i.status == "active"
                )
                for work_id in group
            }
        return min(group, key=lambda work_id: (-strong[work_id], str(work_id)))

    async def _matching(self, actor: ActorContext, words: tuple[str, ...]) -> list[Any]:
        ws = actor.workspace_id
        wanted = _words(words)
        async with self._context.uow_factory() as uow:
            works = await collect_pages(
                lambda after, size: uow.works.list_works(
                    ws,
                    identity_status=IdentityState.CONFLICT.value,
                    limit=size,
                    after_id=after,
                )
            )
            if wanted:
                summaries = await uow.works.get_summaries(ws, [w.id for w in works])
                works = [
                    w
                    for w in works
                    if (s := summaries.get(w.id)) is not None
                    and all(word in s.title_search for word in wanted)
                ]
        return sorted(works, key=lambda w: str(w.id))

    @staticmethod
    async def _counterparts(
        uow: Any, ws: UUID, work_id: UUID, identifiers: list[Any], refused: list[Any]
    ) -> list[tuple[UUID, str, str | None]]:
        """(other paper, why, the identifier in question), each other paper once."""
        found: dict[UUID, tuple[str, str | None]] = {}
        for decision in refused:
            # A duplicate-review proposal is a reopen too, but it only says two
            # titles look alike: it belongs in Pairs to review, not here.
            if (
                work_id not in decision.subject_work_ids
                or (decision.after or {}).get("proposal") == "duplicate_review"
            ):
                continue
            for other in decision.subject_work_ids:
                if other != work_id:
                    found.setdefault(other, ("two_papers_matched_one_record", None))
        own = {(i.scheme.value, i.value_normalized) for i in identifiers}
        assertions = await uow.observations.list_assertions(ws, work_id=work_id)
        for scheme, value, _ in _identifier_assertions(list(assertions)):
            if not scheme.is_strong or (scheme.value, value) in own:
                continue
            holder = await uow.works.find_by_identifier(
                ws, scheme=scheme.value, value_normalized=value
            )
            if holder is not None and holder.id != work_id:
                found.setdefault(
                    holder.id, ("identifier_held_by_another_paper", f"{scheme.value}:{value}")
                )
        return [(other, reason, ident) for other, (reason, ident) in found.items()]


async def _active(uow: Any, ws: UUID, work_id: UUID) -> UUID | None:
    """The active paper this one is, following merges; ``None`` if it is gone."""
    for _ in range(_MAX_MERGE_CHAIN):
        work = await uow.works.get(ws, work_id)
        if work is None:
            return None
        if work.merged_into_work_id is None:
            return work.id if work.is_active else None
        work_id = work.merged_into_work_id
    return None


def _groups(pairs: list[tuple[UUID, UUID]]) -> list[set[UUID]]:
    """Papers joined by any pair, as groups, in a stable order."""
    parent: dict[UUID, UUID] = {}

    def root(node: UUID) -> UUID:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in pairs:
        a, b = root(left), root(right)
        if a != b:
            parent[max(a, b, key=str)] = min(a, b, key=str)
    groups: dict[UUID, set[UUID]] = {}
    for node in list(parent):
        groups.setdefault(root(node), set()).add(node)
    return sorted(groups.values(), key=lambda group: min(str(n) for n in group))
