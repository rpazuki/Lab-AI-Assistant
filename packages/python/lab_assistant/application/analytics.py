"""Release-scoped, policy-filtered corpus analytics.

The predecessor's analytics tab (temporal, journals, MeSH terms, topics, corpus
stats) is a real user-visible feature that the migration plan mentioned only as a
leakage surface. It returns here on two conditions:

* every count is **anchored to a release**, so a number means something
  reproducible rather than "whatever is in the database right now";
* every count is **policy filtered by the same rule as retrieval**, so a total
  cannot reveal the existence of a record the caller may not open.

"MeSH terms" is a PubMed-specific field. In a source-neutral model the equivalent
is a subject-assertion projection, which is what ``subject_terms`` returns.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.contracts.errors import NotFound
from lab_assistant.domain.workspace import Capability

__all__ = ["AnalyticsService", "CorpusStats"]


@dataclass(frozen=True, slots=True)
class CorpusStats:
    release_id: UUID
    release_name: str
    release_version: str
    published_at: str | None
    works: int
    renditions: int
    claims: int
    chunks: int
    #: How many release items the caller may not see. A count is still a
    #: disclosure — it tells a member how much material exists that they cannot
    #: open — so it is reported only to callers who may manage the release, and is
    #: ``None`` for everyone else. Stewards need it to answer "is this release
    #: complete"; an ordinary member does not.
    withheld: int | None


class AnalyticsService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._retrieval = RetrievalService(context)

    async def _resolve(self, actor: ActorContext, release_id: UUID | None, alias: str) -> UUID:
        from lab_assistant.application.mirror_access import served_release_id

        async with self._context.uow_factory() as uow:
            return await served_release_id(
                uow, actor.workspace_id, release_id=release_id, alias=alias
            )

    async def corpus_stats(
        self, actor: ActorContext, *, release_id: UUID | None = None, alias: str = "current"
    ) -> CorpusStats:
        actor.require(Capability.VIEW_ANALYTICS)
        context = self._context
        resolved = await self._resolve(actor, release_id, alias)
        visible, _denied = await self._retrieval.visible_work_ids(actor)
        visible_set = set(visible)
        async with context.uow_factory() as uow:
            release = await uow.releases.get(actor.workspace_id, resolved)
            if release is None:
                raise NotFound(f"release {resolved} not found")
            items = await uow.releases.list_items(actor.workspace_id, resolved)
            permitted = [item for item in items if item.record.work_id in visible_set]
            chunks = (
                # The same visibility filter retrieval uses. Counting every chunk in
                # the release would let a member infer the volume of material they
                # are not allowed to read.
                await context.search_index.count_chunks(
                    actor.workspace_id, resolved, work_ids=visible
                )
                if context.search_index is not None
                else 0
            )
        return CorpusStats(
            release_id=resolved,
            release_name=release.name,
            release_version=release.semantic_version,
            published_at=release.published_at.isoformat() if release.published_at else None,
            works=len(permitted),
            renditions=sum(1 for item in permitted if item.record.rendition_id),
            claims=sum(len(item.record.claim_revision_ids) for item in permitted),
            chunks=chunks,
            withheld=(
                len(items) - len(permitted) if actor.has(Capability.MANAGE_RELEASE) else None
            ),
        )

    async def temporal(
        self, actor: ActorContext, *, release_id: UUID | None = None, alias: str = "current"
    ) -> list[dict[str, Any]]:
        counts = await self._facet(actor, release_id=release_id, alias=alias, facet="year")
        return [
            {"year": int(key), "count": value}
            for key, value in sorted(counts.items())
            if str(key).isdigit()
        ]

    async def journals(
        self,
        actor: ActorContext,
        *,
        release_id: UUID | None = None,
        alias: str = "current",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        counts = await self._facet(actor, release_id=release_id, alias=alias, facet="journal")
        return [
            {"journal": key, "count": value} for key, value in Counter(counts).most_common(limit)
        ]

    async def subject_terms(
        self,
        actor: ActorContext,
        *,
        release_id: UUID | None = None,
        alias: str = "current",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Subject assertions across the release.

        This is the source-neutral replacement for the old MeSH term chart: MeSH
        is one vocabulary asserted by one source, not a field of the domain.
        """
        counts = await self._facet(actor, release_id=release_id, alias=alias, facet="subject_terms")
        return [{"term": key, "count": value} for key, value in Counter(counts).most_common(limit)]

    async def sources(
        self, actor: ActorContext, *, release_id: UUID | None = None, alias: str = "current"
    ) -> list[dict[str, Any]]:
        """Which connector contributed each included work, for source selection.

        The connector is read from the observation's parser id, which discovery
        writes as ``<connector_id>-record-parser``. Unique contribution per source
        is the number that decides whether a connector earns its place.
        """
        actor.require(Capability.VIEW_ANALYTICS)
        context = self._context
        resolved = await self._resolve(actor, release_id, alias)
        visible, _ = await self._retrieval.visible_work_ids(actor)
        visible_set = set(visible)
        counter: Counter[str] = Counter()
        unique: Counter[str] = Counter()
        async with context.uow_factory() as uow:
            observations = {
                observation.id: observation.parser_id.removesuffix("-record-parser")
                for observation in await uow.observations.list_observations(actor.workspace_id)
            }
            items = await uow.releases.list_items(actor.workspace_id, resolved)
            for item in items:
                if item.record.work_id not in visible_set:
                    continue
                assertions = await uow.observations.list_assertions(
                    actor.workspace_id, work_id=item.record.work_id
                )
                connectors = {
                    observations.get(assertion.source_observation_id, "unknown")
                    for assertion in assertions
                } or {"unknown"}
                for connector in connectors:
                    counter[connector] += 1
                if len(connectors) == 1:
                    unique[next(iter(connectors))] += 1
        return [
            {
                "connector_id": key,
                "works": value,
                "unique_contribution": unique.get(key, 0),
            }
            for key, value in counter.most_common()
        ]

    async def _facet(
        self,
        actor: ActorContext,
        *,
        release_id: UUID | None,
        alias: str,
        facet: str,
    ) -> dict[str, int]:
        actor.require(Capability.VIEW_ANALYTICS)
        from lab_assistant.application.reconciliation import canonical_metadata

        context = self._context
        resolved = await self._resolve(actor, release_id, alias)
        visible, _ = await self._retrieval.visible_work_ids(actor)
        visible_set = set(visible)
        counter: Counter[str] = Counter()
        async with context.uow_factory() as uow:
            items = await uow.releases.list_items(actor.workspace_id, resolved)
            for item in items:
                work_id = item.record.work_id
                if work_id not in visible_set:
                    continue
                assertions = await uow.observations.list_assertions(
                    actor.workspace_id, work_id=work_id
                )
                metadata = canonical_metadata(list(assertions))
                value = metadata.get(facet)
                if value is None:
                    continue
                if isinstance(value, list):
                    for entry in value:
                        counter[str(entry)] += 1
                else:
                    counter[str(value)] += 1
        return dict(counter)
