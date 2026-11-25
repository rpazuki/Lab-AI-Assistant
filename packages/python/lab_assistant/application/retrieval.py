"""Release-aware retrieval with policy before scoring.

Order of operations, and it is the whole security property:

1. resolve the release from the alias or the caller's pin;
2. compute the set of works the caller may query **before** any search runs;
3. search lexical, vector, and structured claim projections within that set;
4. fuse with reciprocal rank fusion, optionally rerank;
5. filter again after ranking, as defence in depth;
6. mint opaque citation handles — the only identifiers the generator may return.

Counts and analytics use the same filter. A user must not be able to infer the
existence of a record they cannot open, including from a total.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.pagination import iter_pages
from lab_assistant.contracts.canonical import sha256_text
from lab_assistant.contracts.errors import NotFound
from lab_assistant.contracts.models import EvidenceBlock
from lab_assistant.contracts.policy import PolicyAction, ResourceRef, ResourceType
from lab_assistant.domain.assets import is_abstract
from lab_assistant.domain.states import RelevanceState

__all__ = [
    "RETRIEVAL_PROFILE_VERSION",
    "RetrievalPlan",
    "RetrievalResult",
    "RetrievalService",
    "reciprocal_rank_fusion",
]

RETRIEVAL_PROFILE_VERSION = "default/1"


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]], *, k: int = 60
) -> dict[str, float]:
    """Standard RRF. Deterministic and independent of score scales."""
    scores: dict[str, float] = {}
    for ranking in ranked_lists:
        for position, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position)
    return scores


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    """What the planner decided to do, recorded on the answer."""

    classification: str
    use_lexical: bool
    use_vector: bool
    use_claims: bool
    rewritten_queries: tuple[str, ...]
    release_id: UUID
    profile_version: str = RETRIEVAL_PROFILE_VERSION


@dataclass(slots=True)
class RetrievalResult:
    plan: RetrievalPlan
    evidence: list[EvidenceBlock] = field(default_factory=list)
    #: handle -> provenance needed to store the citation and resolve it later.
    handle_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    denied_count: int = 0
    candidate_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.evidence and not self.claims


def _rights_withhold(row, now) -> bool:
    """A work whose rights were revoked, or whose permission has lapsed."""
    if {"prohibited", "expired"} & set(row.rights_states):
        return True
    due = row.earliest_review_due
    if due is None:
        return False
    if due.tzinfo is None:  # SQLite hands timestamps back naive; they were stored as UTC
        from datetime import UTC

        due = due.replace(tzinfo=UTC)
    return now >= due


#: Page size for the corpus-wide visibility scan. One page is one query; the number
#: trades round trips against how much of a page sits in memory at once.
_VISIBILITY_PAGE_SIZE = 500

_STRUCTURED_HINTS = (
    "how much",
    "what yield",
    "titre",
    "titer",
    "g/l",
    "percent",
    "temperature",
    "highest",
    "compare",
    "which strain",
)


class RetrievalService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def classify(self, query: str) -> tuple[str, bool, bool, bool]:
        """Classify a query into retrieval paths.

        Transparent rules rather than a model: the classification is recorded on
        the answer, so it has to be explainable.
        """
        lowered = query.casefold()
        structured = any(hint in lowered for hint in _STRUCTURED_HINTS)
        if structured:
            return "structured_and_evidence", True, True, True
        return "evidence", True, True, False

    async def visible_work_ids(
        self, actor: ActorContext, *, action: PolicyAction = PolicyAction.QUERY
    ) -> tuple[list[UUID], int]:
        """Works this caller may query, computed before any scoring.

        Every work in the workspace is considered, page by page. A fixed limit here
        would be an access-control filter that silently stops being complete once a
        corpus outgrows it — and it would fail *open* for retrieval (works beyond
        the limit vanish for everyone) while looking like a performance setting.
        """
        context = self._context
        policy_input = actor.policy_input(now=context.now())
        allowed: list[UUID] = []
        denied = 0
        async with context.uow_factory() as uow:

            async def page(after: UUID | None, size: int):
                return await uow.works.list_work_visibility(
                    actor.workspace_id, limit=size, after_id=after
                )

            now = context.now()
            async for row in iter_pages(page, page_size=_VISIBILITY_PAGE_SIZE):
                if not row.is_active or row.relevance == RelevanceState.EXCLUDED.value:
                    continue
                if _rights_withhold(row, now):
                    # Revoked or lapsed: withheld before scoring, and counted,
                    # whoever is asking and whatever groups say.
                    denied += 1
                    continue
                resource = ResourceRef(
                    resource_type=ResourceType.WORK,
                    resource_id=row.work_id,
                    restricted_to_groups=row.restricted_to_groups,
                    sensitivity=row.sensitivity,
                )
                if context.policy_engine.allows(policy_input, action=action, resource=resource):
                    allowed.append(row.work_id)
                else:
                    denied += 1
        return allowed, denied

    async def retrieve(
        self,
        actor: ActorContext,
        *,
        query: str,
        release_id: UUID | None = None,
        alias: str = "current",
        top_k: int | None = None,
    ) -> RetrievalResult:
        context = self._context
        index = context.require_search_index()
        settings = context.settings

        resolved_release = release_id
        if resolved_release is None:
            async with context.uow_factory() as uow:
                search_alias = await uow.releases.get_alias(actor.workspace_id, alias)
            if search_alias is None or search_alias.release_id is None:
                raise NotFound(f"no release is published on alias {alias!r}")
            resolved_release = search_alias.release_id

        classification, use_lexical, use_vector, use_claims = self.classify(query)
        plan = RetrievalPlan(
            classification=classification,
            use_lexical=use_lexical,
            use_vector=use_vector and context.embedding_provider is not None,
            use_claims=use_claims,
            rewritten_queries=(query,),
            release_id=resolved_release,
        )

        allowed_work_ids, denied = await self.visible_work_ids(actor)
        if not allowed_work_ids:
            return RetrievalResult(plan=plan, denied_count=denied)

        lexical_rows: list[dict[str, Any]] = []
        vector_rows: list[dict[str, Any]] = []
        if plan.use_lexical:
            lexical_rows = await index.lexical_search(
                actor.workspace_id,
                resolved_release,
                query=query,
                top_k=settings.retrieval_lexical_top_k,
                work_ids=allowed_work_ids,
            )
        if plan.use_vector:
            embedder = context.require_embeddings()
            vector = await embedder.embed_query(query)
            vector_rows = await index.vector_search(
                actor.workspace_id,
                resolved_release,
                profile_id=embedder.profile.profile_id,
                vector=vector,
                top_k=settings.retrieval_vector_top_k,
                work_ids=allowed_work_ids,
            )

        by_key: dict[str, dict[str, Any]] = {}
        for row in [*lexical_rows, *vector_rows]:
            by_key.setdefault(row["chunk_key"], row)
        fused = reciprocal_rank_fusion(
            [
                [row["chunk_key"] for row in lexical_rows],
                [row["chunk_key"] for row in vector_rows],
            ]
        )
        ranked_keys = sorted(fused, key=lambda key: (-fused[key], key))

        # Post-ranking filter: defence in depth against a projection that
        # contains a row it should not.
        policy_input = actor.policy_input(now=context.now())
        allowed_set = set(allowed_work_ids)
        final_keys: list[str] = []
        for key in ranked_keys:
            row = by_key[key]
            work_id = UUID(str(row["work_id"]))
            if work_id not in allowed_set:
                denied += 1
                continue
            if not context.policy_engine.allows(
                policy_input,
                action=PolicyAction.QUERY,
                resource=ResourceRef(resource_type=ResourceType.WORK, resource_id=work_id),
            ):
                denied += 1
                continue
            final_keys.append(key)

        limit = top_k or settings.retrieval_final_top_k
        result = RetrievalResult(
            plan=plan,
            candidate_count=len(by_key),
            denied_count=denied,
            scores={key: round(fused[key], 6) for key in final_keys[:limit]},
        )

        abstract_renditions: dict[str, bool] = {}
        async with context.uow_factory() as uow:
            for key in final_keys[:limit]:
                row = by_key[key]
                work_id = UUID(str(row["work_id"]))
                work_meta = await self._work_label(uow, actor.workspace_id, work_id)
                abstract_only = await _from_abstract(
                    uow, actor.workspace_id, row.get("rendition_id"), abstract_renditions
                )
                display_restricted = not context.policy_engine.allows(
                    policy_input,
                    action=PolicyAction.DISPLAY_FULL_CONTENT,
                    resource=ResourceRef(resource_type=ResourceType.WORK, resource_id=work_id),
                )
                span_id = await self._span_for_row(uow, actor.workspace_id, row)
                handle = _mint_handle(release_id=resolved_release, chunk_key=key, span_id=span_id)
                text = row["content"]
                if display_restricted:
                    text = _bounded_excerpt(text)
                result.evidence.append(
                    EvidenceBlock(
                        handle=handle,
                        text=text,
                        work_title=work_meta.get("title"),
                        work_year=work_meta.get("year"),
                        source_label=work_meta.get("source_label"),
                        locator=_locator_label(row),
                        display_restricted=display_restricted,
                        abstract_only=abstract_only,
                    )
                )
                result.handle_index[handle] = {
                    "chunk_key": key,
                    "work_id": str(work_id),
                    "rendition_id": row.get("rendition_id"),
                    "evidence_span_id": str(span_id) if span_id else None,
                    "release_id": str(resolved_release),
                    "score": result.scores.get(key),
                    "node_ids": row.get("node_ids", []),
                    "display_label": _locator_label(row),
                    "abstract_only": abstract_only,
                }

            if plan.use_claims:
                claim_rows = await index.claim_search(
                    actor.workspace_id,
                    resolved_release,
                    top_k=limit,
                    # Policy before scoring, for structured claims exactly as for
                    # evidence chunks. The post-filter below stays as defence in
                    # depth rather than as the only filter.
                    work_ids=allowed_work_ids,
                )
                for claim_row in claim_rows:
                    work_id = UUID(str(claim_row["work_id"]))
                    if work_id not in allowed_set:
                        denied += 1
                        continue
                    result.claims.append(claim_row)
                    span_ids = claim_row.get("evidence_span_ids") or []
                    span_id = UUID(str(span_ids[0])) if span_ids else None
                    handle = _mint_handle(
                        release_id=resolved_release,
                        chunk_key=f"claim:{claim_row['revision_id']}",
                        span_id=span_id,
                    )
                    result.handle_index[handle] = {
                        "claim_revision_id": str(claim_row["revision_id"]),
                        "work_id": str(work_id),
                        "evidence_span_id": str(span_id) if span_id else None,
                        "release_id": str(resolved_release),
                        "display_label": f"{claim_row['predicate']} claim",
                    }
                    claim_row["handle"] = handle

        result.denied_count = denied
        return result

    async def _work_label(self, uow, workspace_id: UUID, work_id: UUID) -> dict[str, Any]:
        from lab_assistant.application.reconciliation import canonical_metadata

        assertions = await uow.observations.list_assertions(workspace_id, work_id=work_id)
        metadata = canonical_metadata(list(assertions))
        identifiers = await uow.works.list_identifiers(workspace_id, work_id)
        label = None
        for identifier in identifiers:
            if identifier.scheme.is_strong:
                label = f"{identifier.scheme.value}:{identifier.value_display}"
                break
        year = metadata.get("year")
        title = metadata.get("title")
        if title is None and not assertions:
            # A reader has no source assertions, only the reconciliation the
            # snapshot carried (`application/archive_import.py`).
            imported = await uow.imports.get_work_metadata(workspace_id, work_id)
            if imported is not None:
                title = imported.get("title")
                year = imported.get("publication_year")
        return {
            "title": title,
            "year": int(year) if isinstance(year, int) else None,
            "source_label": label,
        }

    async def _span_for_row(self, uow, workspace_id: UUID, row: dict[str, Any]) -> UUID | None:
        """Find the evidence span covering a chunk's first node, if one exists.

        Retrieval citations resolve to verified spans; a chunk without one cites
        its rendition node and is flagged rather than presented as evidence.
        """
        rendition_id = row.get("rendition_id")
        node_ids = row.get("node_ids") or []
        if not rendition_id or not node_ids:
            return None
        spans = await uow.renditions.list_spans(workspace_id, UUID(str(rendition_id)))
        for span in spans:
            if span.node_id in node_ids:
                return span.id
        return None


def _mint_handle(*, release_id: UUID, chunk_key: str, span_id: UUID | None) -> str:
    """Opaque, server-minted citation handle.

    Not a database id: a handle leaks nothing, and an invented handle cannot
    accidentally resolve to a real row.
    """
    digest = sha256_text(f"{release_id}|{chunk_key}|{span_id or ''}")
    return f"E{digest[:10]}"


def _locator_label(row: dict[str, Any]) -> str:
    parts: list[str] = []
    if row.get("section_label"):
        parts.append(str(row["section_label"]))
    if row.get("page") is not None:
        parts.append(f"p.{row['page']}")
    if row.get("chunk_type") == "table_row":
        parts.append("table row")
    return " · ".join(parts) or "document"


def _bounded_excerpt(text: str, *, limit: int = 240) -> str:
    """Bounded excerpt for material the caller may not see in full."""
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}…"


async def _from_abstract(
    uow: Any, workspace_id: UUID, rendition_id: Any, seen: dict[str, bool]
) -> bool:
    """Whether a retrieved row comes from an abstract standing in for a full text."""
    if not rendition_id:
        return False
    key = str(rendition_id)
    if key not in seen:
        rendition = await uow.renditions.get_rendition(workspace_id, UUID(key))
        asset = (
            await uow.assets.get_asset(workspace_id, rendition.asset_id)
            if rendition is not None
            else None
        )
        seen[key] = asset is not None and is_abstract(asset)
    return seen[key]
