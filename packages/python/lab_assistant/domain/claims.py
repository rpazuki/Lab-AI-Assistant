"""Entities, experiments, claims, revisions, and reviews.

One work reports zero, one, or many experiments, and each experiment carries many
measurements. A spreadsheet row is a projection of this graph, never its storage.

Numeric normalisation keeps verbatim and parsed forms apart, records the basis
(per dry cell weight, per litre, per gram substrate), and never compares two
measurements merely because their display units look similar.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.errors import AbstentionReason
from lab_assistant.contracts.extraction import ProposalSource
from lab_assistant.domain.states import ReviewState

__all__ = [
    "Claim",
    "ClaimRevision",
    "Entity",
    "EntityType",
    "EpistemicStatus",
    "Experiment",
    "NumericValue",
    "Review",
    "ReviewDecision",
    "claim_release_blockers",
    "parse_numeric",
]


class EntityType(StrEnum):
    ORGANISM = "organism"
    STRAIN = "strain"
    GENE = "gene"
    PROTEIN = "protein"
    COMPOUND = "compound"
    MATERIAL = "material"
    INSTRUMENT = "instrument"
    ASSAY = "assay"
    UNIT = "unit"
    INSTITUTION = "institution"
    AUTHOR = "author"
    CONDITION = "condition"
    OTHER = "other"


class EpistemicStatus(StrEnum):
    ASSERTED_BY_SOURCE = "asserted_by_source"
    REVIEWER_INTERPRETATION = "reviewer_interpretation"
    ABSENT_FROM_SOURCE = "absent_from_source"
    UNDETERMINED = "undetermined"


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    CORRECT = "correct"
    REJECT = "reject"
    DEFER = "defer"
    REQUEST_REPARSE = "request_reparse"
    DISPUTE = "dispute"


class Entity(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    entity_type: EntityType
    preferred_label: str
    normalised_value: str
    ontology_namespace: str | None = None
    ontology_id: str | None = None
    resolution_status: str = "unresolved"
    created_at: datetime | None = None


class Experiment(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    work_id: UUID
    rendition_id: UUID | None = None
    local_label: str
    experiment_type: str | None = None
    description_claim_id: UUID | None = None
    extraction_run_id: UUID | None = None
    review_status: ReviewState = ReviewState.UNREVIEWED
    created_at: datetime | None = None
    version: int = 1


class NumericValue(BaseModel):
    """Parsed numeric value with its basis and uncertainty."""

    model_config = {"frozen": True}

    verbatim: str
    value: Decimal | None = None
    low: Decimal | None = None
    high: Decimal | None = None
    unit_verbatim: str | None = None
    unit_canonical: str | None = None
    basis: str | None = None
    denominator: str | None = None
    uncertainty: str | None = None
    significant_figures: int | None = None
    detection_limit: str | None = None
    conversion_library: str | None = None
    conversion_formula: str | None = None

    @property
    def is_interval(self) -> bool:
        return self.low is not None and self.high is not None

    def comparable_with(self, other: NumericValue) -> bool:
        """Two measurements compare only when unit *and* basis agree."""
        return (
            self.unit_canonical is not None
            and self.unit_canonical == other.unit_canonical
            and self.basis == other.basis
            and self.denominator == other.denominator
        )


def parse_numeric(text: str) -> NumericValue | None:
    """Parse a scalar or interval from verbatim text, conservatively.

    Returns ``None`` rather than guessing. A number the parser cannot read is an
    ``ambiguous_multiple_values`` or reviewer decision, not a silent zero.
    """
    cleaned = text.strip().replace("−", "-").replace(",", "")
    for separator in ("–", "-", " to "):
        if separator in cleaned[1:]:
            left, _, right = cleaned[1:].partition(separator)
            left = cleaned[0] + left
            try:
                low, high = Decimal(left.strip()), Decimal(right.strip())
            except (InvalidOperation, ValueError):
                continue
            if low > high:
                low, high = high, low
            return NumericValue(verbatim=text, low=low, high=high)
    try:
        return NumericValue(verbatim=text, value=Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None


class ClaimRevision(BaseModel):
    """One version of a claim's value, with its evidence and review state."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    claim_id: UUID
    revision_number: int
    value: dict[str, Any] = Field(default_factory=dict)
    evidence_span_ids: tuple[UUID, ...] = ()
    proposal_source: ProposalSource = ProposalSource.MODEL
    extractor_fingerprint: str | None = None
    validation_status: str = "pending"  # pending | valid | invalid
    validation_issues: tuple[str, ...] = ()
    review_status: ReviewState = ReviewState.UNREVIEWED
    confidence_components: dict[str, Any] = Field(default_factory=dict)
    abstention_reason: AbstentionReason = AbstentionReason.REPORTED
    epistemic_status: EpistemicStatus = EpistemicStatus.ASSERTED_BY_SOURCE
    created_by: UUID | None = None
    created_at: datetime | None = None
    supersedes_revision_id: UUID | None = None

    @property
    def is_valid(self) -> bool:
        return self.validation_status == "valid"

    def evidence_required(self) -> bool:
        """Whether this revision must carry at least one verified span.

        A reviewer-authored interpretation may stand without a span, but only
        while it is typed as an interpretation. An abstention needs no span
        because it asserts nothing about a value.
        """
        if self.epistemic_status is EpistemicStatus.REVIEWER_INTERPRETATION:
            return False
        if self.abstention_reason is not AbstentionReason.REPORTED:
            return False
        return self.proposal_source.requires_evidence_span


class Claim(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    work_id: UUID
    experiment_id: UUID | None = None
    subject_entity_id: UUID | None = None
    subject_label: str | None = None
    predicate: str
    object_kind: str = "text"
    object_value: dict[str, Any] = Field(default_factory=dict)
    unit_entity_id: UUID | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    polarity: str = "positive"
    epistemic_status: EpistemicStatus = EpistemicStatus.ASSERTED_BY_SOURCE
    extraction_run_id: UUID | None = None
    current_revision_id: UUID | None = None
    template_id: str | None = None
    template_version: str | None = None
    target_id: str | None = None
    lifecycle_status: str = "proposed"  # proposed | active | withdrawn
    created_at: datetime | None = None
    version: int = 1


class Review(BaseModel):
    """Immutable review decision. A correction creates a new revision *and* review."""

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    subject_type: str  # claim_revision | candidate | rendition | release
    subject_id: UUID
    #: The exact revision reviewed. A review of "the claim" would be ambiguous
    #: the moment a second revision existed.
    revision_id: UUID | None = None
    assignment_id: UUID | None = None
    decision: ReviewDecision
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    reason_code: str = ""
    comment: str | None = None
    reviewer_id: UUID
    reviewed_at: datetime | None = None
    policy_version_id: UUID | None = None


def claim_release_blockers(
    revision: ClaimRevision,
    *,
    verified_span_ids: frozenset[UUID],
    require_review: bool,
) -> tuple[str, ...]:
    """Why a claim revision may not enter a published claim release.

    This is the mechanical part of the E8 gate: accepted machine claims have 100
    percent valid evidence, and only approved revisions are release-eligible.
    """
    blockers: list[str] = []
    if not revision.is_valid:
        blockers.append(f"validation status is {revision.validation_status}")
    if require_review and not revision.review_status.is_approved:
        blockers.append(f"review status is {revision.review_status.value}")
    if revision.evidence_required():
        if not revision.evidence_span_ids:
            blockers.append("no evidence span attached")
        else:
            missing = [
                str(span_id)
                for span_id in revision.evidence_span_ids
                if span_id not in verified_span_ids
            ]
            if missing:
                blockers.append(f"evidence spans not verified: {', '.join(sorted(missing))}")
    if (
        revision.abstention_reason is not AbstentionReason.REPORTED
        and not revision.abstention_reason.supports_source_statement
        and revision.value.get("asserts_absence")
    ):
        blockers.append(
            f"abstention {revision.abstention_reason.value} cannot support a statement "
            "about source content"
        )
    return tuple(blockers)
