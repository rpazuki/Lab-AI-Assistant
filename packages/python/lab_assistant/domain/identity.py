"""Work identity: a graph and a decision, never a column that magically agrees.

Two invariants are enforced here and cannot be configured away:

1. an automatic merge requires at least one exact **strong** identifier bridge;
2. a contradiction between strong identifiers produces ``identity_conflict``,
   not last-write-wins.

Title, author, and year similarity may *propose* a pair for review. It can never
satisfy requirement 1. The legacy system merged distinct DOIs on title
similarity; that is the specific failure these invariants exist to prevent.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.domain.states import IdentityState, RelevanceState

__all__ = [
    "COMPATIBLE_WORK_KINDS",
    "DuplicateProposal",
    "IdentityDecision",
    "IdentityDecisionType",
    "MergeAssessment",
    "RelationKind",
    "Work",
    "WorkIdentifier",
    "WorkKind",
    "WorkRelation",
    "assess_merge",
    "normalise_identifier",
    "normalise_title",
    "propose_duplicates",
    "title_similarity",
]


class WorkKind(StrEnum):
    ARTICLE = "article"
    PREPRINT = "preprint"
    DATASET = "dataset"
    SOFTWARE = "software"
    PROTOCOL = "protocol"
    THESIS = "thesis"
    REPORT = "report"
    BOOK_CHAPTER = "book_chapter"
    CONFERENCE_ITEM = "conference_item"
    INTERNAL_DOCUMENT = "internal_document"
    INVENTORY = "inventory"
    UNKNOWN = "unknown"


#: Kinds that may be merged into one work. A preprint and an article are
#: *related*, not identical: they get a relation, not a merge.
COMPATIBLE_WORK_KINDS: tuple[frozenset[WorkKind], ...] = (
    frozenset({WorkKind.ARTICLE, WorkKind.UNKNOWN}),
    frozenset({WorkKind.PREPRINT, WorkKind.UNKNOWN}),
    frozenset({WorkKind.DATASET, WorkKind.UNKNOWN}),
    frozenset({WorkKind.SOFTWARE, WorkKind.UNKNOWN}),
    frozenset({WorkKind.PROTOCOL, WorkKind.UNKNOWN}),
    frozenset({WorkKind.THESIS, WorkKind.UNKNOWN}),
    frozenset({WorkKind.REPORT, WorkKind.UNKNOWN}),
    frozenset({WorkKind.BOOK_CHAPTER, WorkKind.UNKNOWN}),
    frozenset({WorkKind.CONFERENCE_ITEM, WorkKind.UNKNOWN}),
    frozenset({WorkKind.INTERNAL_DOCUMENT, WorkKind.UNKNOWN}),
    frozenset({WorkKind.INVENTORY, WorkKind.UNKNOWN}),
)


class RelationKind(StrEnum):
    IS_VERSION_OF = "is_version_of"
    IS_PREPRINT_OF = "is_preprint_of"
    IS_SUPPLEMENT_TO = "is_supplement_to"
    IS_DATASET_FOR = "is_dataset_for"
    CORRECTS = "corrects"
    RETRACTS = "retracts"
    CITES = "cites"
    DUPLICATES = "duplicates"


class IdentityDecisionType(StrEnum):
    MERGE = "merge"
    SPLIT = "split"
    LINK_VERSION = "link_version"
    REJECT_LINK = "reject_link"
    RESOLVE_FIELD = "resolve_field"
    REOPEN = "reopen"
    SET_RELEVANCE = "set_relevance"


_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

#: Bumping this invalidates stored normalised values, so it is versioned.
IDENTIFIER_NORMALISATION_VERSION = "1"


def normalise_identifier(scheme: IdentifierScheme, value: str) -> str:
    """Normalise for matching without discarding the original form.

    The original display value stays on the ``WorkIdentifier`` row; only this
    output participates in uniqueness constraints.
    """
    text = unicodedata.normalize("NFC", value).strip()
    match scheme:
        case IdentifierScheme.DOI | IdentifierScheme.BIORXIV_DOI | IdentifierScheme.DATACITE_DOI:
            text = _DOI_PREFIX.sub("", text).strip().lower()
            # DOIs are case-insensitive in the suffix by registration practice.
        case IdentifierScheme.PMID:
            text = text.lower().removeprefix("pmid:").strip()
            text = text.lstrip("0") or "0"
        case IdentifierScheme.PMCID:
            text = text.upper().replace("PMC:", "").strip()
            if not text.startswith("PMC"):
                text = f"PMC{text.lstrip('PMC')}"
        case IdentifierScheme.ARXIV:
            text = text.lower().removeprefix("arxiv:").strip()
        case IdentifierScheme.OPENALEX:
            text = text.upper().rsplit("/", 1)[-1]
        case IdentifierScheme.TITLE_NORMALISED:
            text = normalise_title(text)
        case _:
            text = text.strip()
    if not text:
        raise ValueError(f"identifier {value!r} normalises to empty for scheme {scheme}")
    return text


def normalise_title(title: str) -> str:
    """Aggressively normalised title, for *proposals only*."""
    text = unicodedata.normalize("NFKD", title).casefold()
    text = _NON_WORD.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def title_similarity(left: str, right: str) -> float:
    """Token Jaccard similarity of normalised titles.

    Deliberately simple and deterministic. A cleverer measure would still be
    unable to satisfy the strong-identifier requirement, so cleverness here buys
    nothing except non-reproducibility.
    """
    left_tokens = set(normalise_title(left).split())
    right_tokens = set(normalise_title(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union


class WorkIdentifier(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    work_id: UUID
    scheme: IdentifierScheme
    value_normalized: str
    value_display: str
    #: Which source assertion produced this identifier.
    assertion_id: UUID | None = None
    confidence: float | None = None
    status: str = "active"
    normalisation_version: str = IDENTIFIER_NORMALISATION_VERSION

    @property
    def is_strong(self) -> bool:
        return self.scheme.is_strong

    @property
    def key(self) -> tuple[str, str]:
        return (self.scheme.value, self.value_normalized)


class Work(BaseModel):
    """Intentionally sparse. Canonical metadata is resolved from assertions."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    work_kind: WorkKind = WorkKind.UNKNOWN
    identity_status: IdentityState = IdentityState.UNRESOLVED
    relevance: RelevanceState = RelevanceState.UNREVIEWED
    canonical_metadata_version: int = 0
    retraction_status: str = "none"  # none | retracted | correction_issued
    correction_status: str = "none"
    merged_into_work_id: UUID | None = None
    created_at: datetime | None = None
    version: int = 1

    @property
    def is_active(self) -> bool:
        return self.merged_into_work_id is None and self.identity_status is not IdentityState.MERGED

    @property
    def is_retracted(self) -> bool:
        return self.retraction_status == "retracted"


class WorkRelation(BaseModel):
    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    from_work_id: UUID
    to_work_id: UUID
    kind: RelationKind
    assertion_ids: tuple[UUID, ...] = ()
    decided_by: UUID | None = None
    created_at: datetime | None = None


class IdentityDecision(BaseModel):
    """A human (or explicit rule) decision about identity, kept forever."""

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    decision_type: IdentityDecisionType
    subject_work_ids: tuple[UUID, ...]
    supporting_assertion_ids: tuple[UUID, ...] = ()
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    reason_code: str = ""
    note: str | None = None
    decided_by: UUID | None = None
    decided_by_type: str = "human"  # human | automatic_rule
    decided_at: datetime | None = None


class MergeAssessment(BaseModel):
    """The full explanation of why a merge is or is not automatic."""

    model_config = {"frozen": True}

    allowed: bool
    reason_code: str
    bridging_identifiers: tuple[tuple[str, str], ...] = ()
    contradictions: tuple[tuple[str, str, str], ...] = ()
    incompatible_kinds: tuple[str, ...] = ()
    explanation: str = ""

    def as_decision_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "bridging_identifiers": [list(b) for b in self.bridging_identifiers],
            "contradictions": [list(c) for c in self.contradictions],
            "incompatible_kinds": list(self.incompatible_kinds),
            "explanation": self.explanation,
        }


def _kinds_compatible(left: WorkKind, right: WorkKind) -> bool:
    if left is right:
        return True
    return any({left, right} <= group for group in COMPATIBLE_WORK_KINDS)


def assess_merge(
    *,
    left: Work,
    right: Work,
    left_identifiers: Sequence[WorkIdentifier],
    right_identifiers: Sequence[WorkIdentifier],
    confidence_threshold: float = 1.0,
) -> MergeAssessment:
    """Decide whether two works may be merged automatically.

    All five requirements from data model §7.4 are checked, and the returned
    assessment is what gets persisted as the decision explanation. There is no
    code path that merges without one.
    """
    if left.workspace_id != right.workspace_id:
        return MergeAssessment(
            allowed=False,
            reason_code="cross_workspace",
            explanation="works belong to different workspaces",
        )

    left_active = [i for i in left_identifiers if i.status == "active"]
    right_active = [i for i in right_identifiers if i.status == "active"]

    left_strong = {i.key for i in left_active if i.is_strong}
    right_strong = {i.key for i in right_active if i.is_strong}
    bridges = tuple(sorted(left_strong & right_strong))

    # A contradiction is the same *strong scheme* holding different values.
    contradictions: list[tuple[str, str, str]] = []
    for scheme in {s for s, _ in left_strong} & {s for s, _ in right_strong}:
        left_values = {v for s, v in left_strong if s == scheme}
        right_values = {v for s, v in right_strong if s == scheme}
        if left_values != right_values and not (left_values & right_values):
            contradictions.append(
                (scheme, ",".join(sorted(left_values)), ",".join(sorted(right_values)))
            )

    if contradictions:
        return MergeAssessment(
            allowed=False,
            reason_code="strong_identifier_contradiction",
            bridging_identifiers=bridges,
            contradictions=tuple(sorted(contradictions)),
            explanation=(
                "at least one strong identifier scheme holds different values on each side; "
                "this is an identity conflict for a human decision"
            ),
        )

    if not bridges:
        weak_only = bool(
            {i.key for i in left_active if not i.is_strong}
            & {i.key for i in right_active if not i.is_strong}
        )
        return MergeAssessment(
            allowed=False,
            reason_code="weak_evidence_only" if weak_only else "no_identifier_bridge",
            explanation=(
                "no exact strong identifier is shared; title or author similarity can only "
                "propose a review"
            ),
        )

    if not _kinds_compatible(left.work_kind, right.work_kind):
        return MergeAssessment(
            allowed=False,
            reason_code="incompatible_resource_kind",
            bridging_identifiers=bridges,
            incompatible_kinds=(left.work_kind.value, right.work_kind.value),
            explanation="resource kinds are related but distinct; link them instead of merging",
        )

    confidence = min(
        [i.confidence for i in list(left_active) + list(right_active) if i.confidence is not None]
        or [1.0]
    )
    if confidence < confidence_threshold:
        return MergeAssessment(
            allowed=False,
            reason_code="below_confidence_threshold",
            bridging_identifiers=bridges,
            explanation=(
                f"identifier confidence {confidence} below threshold {confidence_threshold}"
            ),
        )

    return MergeAssessment(
        allowed=True,
        reason_code="strong_identifier_bridge",
        bridging_identifiers=bridges,
        explanation=(
            "exact strong identifier bridge with no contradiction and compatible resource kinds"
        ),
    )


class DuplicateProposal(BaseModel):
    """A candidate pair for human review. Never applied automatically."""

    model_config = {"frozen": True}

    left_work_id: UUID
    right_work_id: UUID
    similarity: float
    features: dict[str, Any] = Field(default_factory=dict)
    reason_code: str = "title_author_year_similarity"


def propose_duplicates(
    candidates: Iterable[tuple[UUID, str, int | None, str | None]],
    *,
    threshold: float = 0.85,
) -> tuple[DuplicateProposal, ...]:
    """Propose near-duplicate pairs from (work_id, title, year, first_author).

    Output feeds a review queue. ``assess_merge`` still refuses these pairs
    unless a strong identifier bridge appears, which is the point.
    """
    items = list(candidates)
    proposals: list[DuplicateProposal] = []
    for index, (left_id, left_title, left_year, left_author) in enumerate(items):
        for right_id, right_title, right_year, right_author in items[index + 1 :]:
            similarity = title_similarity(left_title, right_title)
            if similarity < threshold:
                continue
            year_gap = (
                abs(left_year - right_year)
                if left_year is not None and right_year is not None
                else None
            )
            if year_gap is not None and year_gap > 2:
                continue
            proposals.append(
                DuplicateProposal(
                    left_work_id=left_id,
                    right_work_id=right_id,
                    similarity=round(similarity, 4),
                    features={
                        "year_gap": year_gap,
                        "first_author_match": (
                            None
                            if left_author is None or right_author is None
                            else left_author.casefold() == right_author.casefold()
                        ),
                    },
                )
            )
    return tuple(proposals)


def resolve_canonical_field(
    assertions: Sequence[Mapping[str, Any]],
    *,
    authority_order: Sequence[str],
) -> tuple[Any, list[UUID]]:
    """Resolve one canonical field value, returning the value *and its sources*.

    Field-level provenance is the audit finding H-2 fix: "which source asserted
    this title, using which connector version" must always be answerable.
    """
    if not assertions:
        return None, []
    ranked = sorted(
        assertions,
        key=lambda a: (
            authority_order.index(a.get("source_priority_class", ""))
            if a.get("source_priority_class", "") in authority_order
            else len(authority_order),
            -(a.get("confidence") or 0.0),
            str(a.get("asserted_at_source") or ""),
        ),
    )
    winner = ranked[0]
    value = winner.get("normalised_value_json", winner.get("value_json"))
    supporting = [
        a["id"]
        for a in ranked
        if (a.get("normalised_value_json", a.get("value_json"))) == value and "id" in a
    ]
    return value, supporting
