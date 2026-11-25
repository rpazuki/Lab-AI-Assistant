"""Collection progress: how far the collection has got, stage by stage, before publication.

The Analytics page describes the published release, which is what Ask serves.
This describes the work in progress behind it, for the people building it:

* **funnel**: papers found, included, planned, fetched, parsed, extracted, and
  in the release being served;
* **discovery**: what each source found, what merged, what is still in conflict;
* **screening**: relevance, how much a person decided, abstract coverage;
* **acquisition**: plans by status and, for papers not fetched, the latest answer
  their sources gave, and how fetched files were tied to their paper;
* **parsing** and **extraction**: parse quality and why documents are degraded,
  and the values waiting for review;
* **daily**: papers found, files fetched and documents parsed per day.

Every number is counted over the whole workspace each time the page loads; none
is a sample.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import collect_pages
from lab_assistant.application.parse_quality import ParseQualityService
from lab_assistant.application.releases import DEFAULT_ALIAS
from lab_assistant.contracts.stage import StageName
from lab_assistant.domain.review import CandidateFilter
from lab_assistant.domain.states import IdentityState, ParseState, RelevanceState, TaskState
from lab_assistant.domain.workspace import Capability

__all__ = ["CollectionProgressService"]

#: Days the daily series covers.
DAYS = 30


def _day(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date().isoformat()


class CollectionProgressService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def view(self, actor: ActorContext) -> dict[str, Any]:
        actor.require(Capability.MANAGE_SCOPE)
        context = self._context
        ws = actor.workspace_id
        async with context.uow_factory() as uow:
            works = uow.works

            async def count(**rule: Any) -> int:
                return await works.count_filtered(ws, CandidateFilter(**rule))

            found = await count()
            relevance = {state.value: await count(relevance=state) for state in RelevanceState}
            included = (
                relevance[RelevanceState.INCLUDED.value]
                + relevance[RelevanceState.INCLUDED_UNCERTAIN.value]
            )
            with_abstract = await count(has_abstract=True)
            included_with_abstract = await count(
                relevance=RelevanceState.INCLUDED, has_abstract=True
            ) + await count(relevance=RelevanceState.INCLUDED_UNCERTAIN, has_abstract=True)
            merged = await count(identity_status=IdentityState.MERGED)
            conflicts = await count(identity_status=IdentityState.CONFLICT)
            facets = await works.summary_facets(ws)
            decided_by_person = len(await works.human_relevance_work_ids(ws))
            open_pairs = await works.count_duplicate_candidates(ws, status="open")
            runs = await uow.observations.count_runs(ws)

            plans = await uow.assets.count_plans_by_status(ws)
            waiting_reasons = await uow.assets.latest_outcomes(ws, status="assisted")
            failed_reasons = await uow.assets.latest_outcomes(ws, status="failed")
            assets = await uow.assets.list_assets(ws)
            renditions = await uow.renditions.list_renditions(ws)

            review_queue = await uow.claims.count_review_queue(ws, include_invalid=True)
            extraction = {
                state.value: await uow.tasks.count_tasks(
                    ws, states=[state.value], stage=StageName.EXTRACTION.value
                )
                for state in TaskState
            }
            claims = await collect_pages(
                lambda after, size: uow.claims.list_claims(ws, limit=size, after_id=after)
            )
            alias = await uow.releases.get_alias(ws, DEFAULT_ALIAS)
            served = (
                await uow.releases.list_items(ws, alias.release_id)
                if alias is not None and alias.release_id is not None
                else []
            )
            all_works = await collect_pages(
                lambda after, size: works.list_works(ws, limit=size, after_id=after),
                page_size=5_000,
            )

        parse_view = await ParseQualityService(context).degraded_summary(ws)
        parsed_ready = {r.work_id for r in renditions if r.quality_status is ParseState.READY}
        extracted = {claim.work_id for claim in claims}
        fetched = {asset.work_id for asset in assets}
        identity = Counter(
            "unconfirmed"
            if not asset.identity_verified
            else (asset.metadata or {}).get("identity_basis") or "identifier"
            for asset in assets
        )
        preprints = sum(1 for a in assets if (a.metadata or {}).get("version") == "preprint")

        today = context.now().astimezone(UTC).date()
        days = [(today - timedelta(days=offset)).isoformat() for offset in range(DAYS - 1, -1, -1)]
        found_by_day = Counter(_day(w.created_at) for w in all_works)
        fetched_by_day = Counter(_day(a.created_at) for a in assets)
        parsed_by_day = Counter(_day(r.created_at) for r in renditions)

        return {
            "funnel": [
                {"stage": "found", "label": "Found", "count": found},
                {"stage": "included", "label": "Included", "count": included},
                {"stage": "planned", "label": "Planned for fetching", "count": sum(plans.values())},
                {"stage": "fetched", "label": "Full text fetched", "count": len(fetched)},
                {"stage": "parsed", "label": "Parsed, ready", "count": len(parsed_ready)},
                {"stage": "extracted", "label": "Values extracted", "count": len(extracted)},
                {
                    "stage": "served",
                    "label": "In the release Ask serves",
                    "count": len({item.record.work_id for item in served}),
                },
            ],
            "discovery": {
                "runs": runs,
                "by_source": facets.get("sources", {}),
                "merged": merged,
                "open_duplicate_pairs": open_pairs,
                "identity_conflicts": conflicts,
            },
            "screening": {
                "relevance": relevance,
                "decided_by_person": decided_by_person,
                "with_abstract": with_abstract,
                "included_with_abstract": included_with_abstract,
                "found": found,
                "included": included,
            },
            "acquisition": {
                "plans": plans,
                "waiting_reasons": dict(sorted(waiting_reasons.items(), key=lambda kv: -kv[1])),
                "failed_reasons": dict(sorted(failed_reasons.items(), key=lambda kv: -kv[1])),
                "identity": dict(identity),
                "preprints": preprints,
            },
            "parsing": {
                "by_status": dict(Counter(r.quality_status.value for r in renditions)),
                "degraded_by_reason": parse_view,
            },
            "extraction": {
                "papers_with_values": len(extracted),
                "values": len(claims),
                "waiting_review": review_queue,
                "tasks": {state: n for state, n in extraction.items() if n},
            },
            "daily": [
                {
                    "day": day,
                    "found": found_by_day.get(day, 0),
                    "fetched": fetched_by_day.get(day, 0),
                    "parsed": parsed_by_day.get(day, 0),
                }
                for day in days
            ],
        }
