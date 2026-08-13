"""Citation lineage resolution.

    message citation -> evidence span -> rendition -> asset -> acquisition attempt
                     -> manifestation -> work -> source assertions

That chain is the minimum defensible provenance for a research answer. This
module resolves it from repository lookups and reports exactly where it breaks,
because a chain that silently stops one link short is worse than no chain.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = ["CitationChain", "LineageResolver"]


class CitationChain(BaseModel):
    """One fully or partially resolved citation."""

    model_config = {"frozen": True}

    handle: str
    evidence_span_id: UUID | None = None
    rendition_id: UUID | None = None
    asset_id: UUID | None = None
    asset_sha256: str | None = None
    acquisition_attempt_id: UUID | None = None
    manifestation_id: UUID | None = None
    work_id: UUID | None = None
    claim_revision_id: UUID | None = None
    release_id: UUID | None = None
    source_assertion_ids: tuple[UUID, ...] = ()
    quote_preview: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    #: Which link failed, if any.
    broken_at: str | None = None

    @property
    def complete(self) -> bool:
        return self.broken_at is None and None not in (
            self.evidence_span_id,
            self.rendition_id,
            self.asset_id,
            self.work_id,
        )


class LineageResolver:
    """Resolves chains through injected lookups, so it stays adapter-free."""

    def __init__(
        self,
        *,
        get_span: Callable[[UUID], Any | None],
        get_rendition: Callable[[UUID], Any | None],
        get_asset: Callable[[UUID], Any | None],
        get_attempt: Callable[[UUID], Any | None],
        get_work_assertions: Callable[[UUID], list[UUID]],
    ) -> None:
        self._get_span = get_span
        self._get_rendition = get_rendition
        self._get_asset = get_asset
        self._get_attempt = get_attempt
        self._get_work_assertions = get_work_assertions

    def resolve(
        self,
        *,
        handle: str,
        evidence_span_id: UUID | None,
        claim_revision_id: UUID | None = None,
        release_id: UUID | None = None,
    ) -> CitationChain:
        if evidence_span_id is None:
            return CitationChain(
                handle=handle,
                claim_revision_id=claim_revision_id,
                release_id=release_id,
                broken_at="evidence_span",
            )
        span = self._get_span(evidence_span_id)
        if span is None:
            return CitationChain(
                handle=handle, evidence_span_id=evidence_span_id, broken_at="evidence_span"
            )
        rendition = self._get_rendition(span.rendition_id)
        if rendition is None:
            return CitationChain(
                handle=handle,
                evidence_span_id=evidence_span_id,
                rendition_id=span.rendition_id,
                quote_preview=span.quote_preview,
                locator=span.locator,
                broken_at="rendition",
            )
        asset = self._get_asset(rendition.asset_id)
        if asset is None:
            return CitationChain(
                handle=handle,
                evidence_span_id=evidence_span_id,
                rendition_id=rendition.id,
                asset_id=rendition.asset_id,
                quote_preview=span.quote_preview,
                locator=span.locator,
                broken_at="asset",
            )
        attempt = (
            self._get_attempt(asset.created_from_attempt_id)
            if asset.created_from_attempt_id is not None
            else None
        )
        work_id = asset.work_id or rendition.work_id
        assertions = tuple(self._get_work_assertions(work_id)) if work_id else ()
        return CitationChain(
            handle=handle,
            evidence_span_id=evidence_span_id,
            rendition_id=rendition.id,
            asset_id=asset.id,
            asset_sha256=asset.sha256,
            acquisition_attempt_id=attempt.id if attempt is not None else None,
            manifestation_id=asset.manifestation_id,
            work_id=work_id,
            claim_revision_id=claim_revision_id,
            release_id=release_id,
            source_assertion_ids=assertions,
            quote_preview=span.quote_preview,
            locator=span.locator,
            broken_at=None if work_id is not None else "work",
        )
