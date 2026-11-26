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
searchable by title words, and keeping separate can be applied to every paper a
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

__all__ = ["CHUNK", "IdentityConflictService"]

#: Conflicts kept separate per call; the page repeats until none remain.
CHUNK = 200

KEPT_SEPARATE = "steward_kept_separate"


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
        works = await self._matching(actor, words)
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
