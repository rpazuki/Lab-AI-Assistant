"""Extraction contract: templates, proposals, and validation outcomes.

An extractor never writes a claim. It proposes one, and the proposal carries the
evidence pointer that mechanical validation will check. A proposal that fails
validation is retained as a failed attempt for diagnosis; it does not become a
claim awaiting scientific approval.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.errors import AbstentionReason

__all__ = [
    "ClaimProposal",
    "EvidenceRef",
    "ExtractionContext",
    "ExtractionRequest",
    "ExtractionResult",
    "Extractor",
    "ProposalSource",
    "TemplateCardinality",
    "TemplateTarget",
    "TemplateVersionSpec",
    "ValidationIssue",
    "ValidationOutcome",
]


class TemplateCardinality(StrEnum):
    PER_WORK = "per_work"
    PER_EXPERIMENT = "per_experiment"
    PER_SAMPLE = "per_sample"
    PER_CLAIM = "per_claim"
    PER_RELATION = "per_relation"


class ProposalSource(StrEnum):
    DETERMINISTIC_PARSER = "deterministic_parser"
    MODEL = "model"
    IMPORTED_DATASET = "imported_dataset"
    HUMAN = "human"
    LEGACY_IMPORT = "legacy_import"

    @property
    def requires_evidence_span(self) -> bool:
        """A human interpretation may stand without a span; nothing else may.

        It must then be typed as reviewer-authored interpretation and cannot
        masquerade as an extracted source fact (data model §10.4).
        """
        return self is not ProposalSource.HUMAN


class TemplateTarget(BaseModel):
    """One thing a template asks the extractor to find."""

    model_config = {"frozen": True}

    target_id: str
    predicate: str
    object_kind: str  # quantity | text | entity | boolean | interval
    description: str
    unit_expectation: str | None = None
    controlled_vocabulary: tuple[str, ...] = ()
    #: Sections/tables where the value is likely; a hint for the selector, never
    #: a limit on the exhaustive search.
    likely_sections: tuple[str, ...] = ()
    required: bool = False
    #: Whether this target is deterministic metadata that must come from
    #: resolved source assertions rather than from a model.
    deterministic_metadata: bool = False


class TemplateVersionSpec(BaseModel):
    """Immutable template definition (data model §12.1)."""

    model_config = {"frozen": True}

    template_id: str
    semantic_version: str
    title: str
    scientific_purpose: str
    exclusions: str = ""
    cardinality: TemplateCardinality = TemplateCardinality.PER_EXPERIMENT
    targets: tuple[TemplateTarget, ...] = ()
    evidence_standard: str = "exact span in focal content, normalisation version 1"
    reviewer_instructions: str = ""
    #: Long-form projection is canonical; wide is a convenience view.
    wide_projection_columns: tuple[str, ...] = ()
    schema_version: str = "1.0"

    @property
    def logical_key(self) -> str:
        """Template identity for cache keys: id *and* version, never version alone."""
        return f"{self.template_id}@{self.semantic_version}"

    def target(self, target_id: str) -> TemplateTarget | None:
        for target in self.targets:
            if target.target_id == target_id:
                return target
        return None


class EvidenceRef(BaseModel):
    """A pointer into a rendition, plus the quote the extractor believes is there."""

    model_config = {"frozen": True}

    node_id: str
    quote: str
    start_offset: int | None = None
    end_offset: int | None = None
    locator: dict[str, Any] = Field(default_factory=dict)


class ClaimProposal(BaseModel):
    """One proposed claim, before validation and review."""

    model_config = {"frozen": True}

    target_id: str
    predicate: str
    #: Local grouping label so several proposals can share one experiment.
    experiment_label: str | None = None
    subject_label: str | None = None
    object_kind: str = "text"
    value_verbatim: str | None = None
    value_normalised: Any | None = None
    unit_verbatim: str | None = None
    unit_canonical: str | None = None
    basis: str | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    polarity: str = "positive"
    abstention: AbstentionReason = AbstentionReason.REPORTED
    evidence: tuple[EvidenceRef, ...] = ()
    proposal_source: ProposalSource = ProposalSource.MODEL
    confidence_components: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class ValidationOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


class ValidationIssue(BaseModel):
    model_config = {"frozen": True}

    code: str
    detail: str
    target_id: str | None = None
    node_id: str | None = None


class ExtractionContext(BaseModel):
    """Everything the extractor is allowed to see, and nothing more."""

    model_config = {"frozen": True}

    workspace_id: UUID
    work_id: UUID
    rendition_id: UUID
    rendition_digest: str
    template: TemplateVersionSpec
    #: Ordered, bounded evidence windows with their node ids retained.
    selected_nodes: tuple[str, ...]
    #: Nodes eligible but not selected because the budget ran out. Non-empty
    #: means abstentions must be `not_searched_budget`, never `not_reported`.
    unsearched_nodes: tuple[str, ...] = ()
    evidence_text: str = ""
    #: Deterministic bibliographic metadata, resolved from source assertions.
    deterministic_metadata: dict[str, Any] = Field(default_factory=dict)
    prompt_bundle_id: str | None = None
    prompt_bundle_version: str | None = None
    allow_external_model: bool = False


class ExtractionRequest(BaseModel):
    model_config = {"frozen": True}

    context: ExtractionContext
    max_output_tokens: int = 4096


class ExtractionResult(BaseModel):
    model_config = {"frozen": True}

    proposals: tuple[ClaimProposal, ...] = ()
    #: Digest of the immutable stored request/response envelope pair.
    request_object_digest: str | None = None
    response_object_digest: str | None = None
    provider: str | None = None
    model: str | None = None
    model_revision: str | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False
    refused: bool = False


@runtime_checkable
class Extractor(Protocol):
    extractor_id: str
    extractor_version: str

    async def propose(self, request: ExtractionRequest) -> ExtractionResult: ...
