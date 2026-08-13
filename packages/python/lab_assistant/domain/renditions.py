"""Parse runs, renditions, and evidence spans.

Re-parsing creates a *new* rendition and *new* spans. It never retargets an old
citation, because a published answer's citation must keep meaning exactly what it
meant when the answer was generated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import fingerprint, sha256_text
from lab_assistant.contracts.parsing import (
    ParseQualityMetrics,
    ParserProfile,
    RenditionDocument,
)
from lab_assistant.domain.normalisation import (
    SPAN_NORMALISATION_VERSION,
    SpanVerification,
    verify_quote,
)
from lab_assistant.domain.states import ParseState

__all__ = [
    "EvidenceSpan",
    "ParseRun",
    "Rendition",
    "build_evidence_span",
    "parse_run_fingerprint",
    "rendition_quality_blockers",
]


def parse_run_fingerprint(
    *,
    asset_sha256: str,
    parser_id: str,
    parser_version: str,
    profile: ParserProfile,
) -> str:
    """Everything that changes a rendition's content."""
    return fingerprint(
        kind="parse_run",
        parts={
            "asset_sha256": asset_sha256,
            "parser": f"{parser_id}@{parser_version}",
            "profile": f"{profile.profile_id}@{profile.profile_version}",
            "ocr": {
                "enabled": profile.ocr_enabled,
                "engine": profile.ocr_engine,
                "language": profile.ocr_language,
            },
            "normalisation_version": profile.normalisation_version,
        },
    )


class ParseRun(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    asset_id: UUID
    parser_id: str
    parser_version: str
    parser_profile_version_id: UUID | None = None
    input_sha256: str = ""
    run_fingerprint: str = ""
    status: str = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    quality_metrics: ParseQualityMetrics = ParseQualityMetrics()
    log_object_id: UUID | None = None


class Rendition(BaseModel):
    """Metadata about a rendition; the tree itself lives in object storage."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    parse_run_id: UUID
    asset_id: UUID
    work_id: UUID | None = None
    format: str = "structured/1.0"
    schema_version: str = "1.0"
    object_id: UUID | None = None
    sha256: str = ""
    language: str | None = None
    page_count: int = 0
    section_count: int = 0
    table_count: int = 0
    figure_count: int = 0
    quality_status: ParseState = ParseState.NOT_STARTED
    accepted_degraded_by: UUID | None = None
    accepted_degraded_reason: str | None = None
    supersedes_rendition_id: UUID | None = None
    created_at: datetime | None = None
    version: int = 1

    @property
    def is_extraction_eligible(self) -> bool:
        """Ready, or degraded with an explicit recorded reviewer acceptance."""
        if self.quality_status.can_extract:
            return True
        return self.quality_status is ParseState.DEGRADED and self.accepted_degraded_by is not None


class EvidenceSpan(BaseModel):
    """A verified pointer into an immutable rendition.

    Creation is only possible through :func:`build_evidence_span`, which performs
    the verification. A span row therefore always means "this text was there".
    """

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    rendition_id: UUID
    node_id: str
    start_offset: int
    end_offset: int
    quote_sha256: str
    quote_preview: str
    normalisation_version: str = SPAN_NORMALISATION_VERSION
    locator: dict[str, Any] = Field(default_factory=dict)
    context_object_id: UUID | None = None
    is_focal: bool = True
    created_at: datetime | None = None

    @property
    def length(self) -> int:
        return self.end_offset - self.start_offset


def build_evidence_span(
    *,
    span_id: UUID,
    workspace_id: UUID,
    rendition_id: UUID,
    rendition: RenditionDocument,
    node_id: str,
    quote: str,
    expected_start: int | None = None,
    preview_chars: int = 240,
    created_at: datetime | None = None,
) -> tuple[EvidenceSpan | None, SpanVerification]:
    """Verify ``quote`` against ``node_id`` and build a span if it holds.

    Returns ``(None, verification)`` on failure so the caller can record the
    failure code on the rejected proposal.
    """
    node_text = rendition.text_of(node_id)
    if node_text is None:
        return None, SpanVerification(
            verified=False,
            failure_code="node_not_found",
            detail=f"node {node_id!r} does not exist in this rendition",
        )

    verification = verify_quote(node_text=node_text, quote=quote, expected_start=expected_start)
    if not verification.verified:
        return None, verification

    node = rendition.node_by_id(node_id)
    cell = rendition.cell_by_id(node_id) if node is None else None
    coordinates = node.coordinates if node is not None else (cell.coordinates if cell else None)
    is_focal = node.is_focal if node is not None else True

    assert verification.raw_start is not None and verification.raw_end is not None
    locator: dict[str, Any] = {"node_id": node_id}
    if coordinates is not None:
        locator.update(
            {
                key: value
                for key, value in coordinates.model_dump(mode="json").items()
                if value is not None
            }
        )
    if cell is not None:
        locator.update({"row": cell.row, "column": cell.column, "is_header": cell.is_header})

    span = EvidenceSpan(
        id=span_id,
        workspace_id=workspace_id,
        rendition_id=rendition_id,
        node_id=node_id,
        start_offset=verification.raw_start,
        end_offset=verification.raw_end,
        quote_sha256=sha256_text(quote),
        quote_preview=quote[:preview_chars],
        locator=locator,
        is_focal=is_focal,
        created_at=created_at,
    )
    return span, verification


def rendition_quality_blockers(
    metrics: ParseQualityMetrics, profile: ParserProfile
) -> tuple[str, ...]:
    """Which parse-profile thresholds this rendition fails (quality §2.4)."""
    blockers: list[str] = []
    if metrics.page_coverage < profile.min_page_coverage:
        blockers.append(
            f"page coverage {metrics.page_coverage:.3f} below {profile.min_page_coverage}"
        )
    if metrics.duplicate_paragraph_ratio > profile.max_duplicate_paragraph_ratio:
        blockers.append(
            f"duplicate paragraph ratio {metrics.duplicate_paragraph_ratio:.3f} above "
            f"{profile.max_duplicate_paragraph_ratio}"
        )
    if metrics.replacement_char_rate > profile.max_replacement_char_rate:
        blockers.append(
            f"replacement character rate {metrics.replacement_char_rate:.4f} above "
            f"{profile.max_replacement_char_rate}"
        )
    if metrics.characters < profile.min_characters:
        blockers.append(f"only {metrics.characters} characters extracted")
    if profile.require_reference_separation and not metrics.reference_separation:
        blockers.append("reference section was not separated from focal content")
    if metrics.embedded_identifier_match is False:
        blockers.append("embedded identifier does not match the intended work")
    return tuple(blockers)
