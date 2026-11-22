"""The orthogonal state dimensions (lifecycle §16).

An item occupies several states at once. The UI may derive a single workflow
label; the database keeps the dimensions separate. One overloaded ``status``
column is how the legacy system ended up unable to distinguish "not acquired"
from "acquired but unparsed".
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AcquisitionState",
    "ExtractionState",
    "IdentityState",
    "ParseState",
    "ReleaseState",
    "RelevanceState",
    "ReviewState",
    "RightsState",
    "TaskState",
    "WorkflowLabel",
    "derive_workflow_label",
]


class IdentityState(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    CONFLICT = "conflict"
    MERGED = "merged"
    SPLIT = "split"


class RelevanceState(StrEnum):
    UNREVIEWED = "unreviewed"
    INCLUDED = "included"
    INCLUDED_UNCERTAIN = "included_uncertain"
    EXCLUDED = "excluded"

    @property
    def is_included(self) -> bool:
        return self in (RelevanceState.INCLUDED, RelevanceState.INCLUDED_UNCERTAIN)


class RightsState(StrEnum):
    UNKNOWN = "unknown"
    PERMITTED = "permitted"
    RESTRICTED = "restricted"
    PROHIBITED = "prohibited"
    EXPIRED = "expired"


class AcquisitionState(StrEnum):
    UNPLANNED = "unplanned"
    PLANNED = "planned"
    ACQUIRED = "acquired"
    ASSISTED = "assisted"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ParseState(StrEnum):
    NOT_STARTED = "not_started"
    READY = "ready"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"
    FAILED = "failed"

    @property
    def can_extract(self) -> bool:
        """Degraded renditions need an explicit reviewer acceptance first."""
        return self is ParseState.READY


class ExtractionState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    PROPOSED = "proposed"
    VALIDATED = "validated"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class ReviewState(StrEnum):
    UNREVIEWED = "unreviewed"
    ASSIGNED = "assigned"
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"
    DISPUTED = "disputed"
    DEFERRED = "deferred"

    @property
    def is_approved(self) -> bool:
        return self in (ReviewState.ACCEPTED, ReviewState.CORRECTED)


class ReleaseState(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    REJECTED = "rejected"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"

    @property
    def is_immutable(self) -> bool:
        return self in (
            ReleaseState.PUBLISHED,
            ReleaseState.SUPERSEDED,
            ReleaseState.WITHDRAWN,
        )

    @property
    def is_servable(self) -> bool:
        return self in (ReleaseState.PUBLISHED, ReleaseState.SUPERSEDED)


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    BLOCKED = "blocked"
    AWAITING_USER = "awaiting_user"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.DEAD_LETTER,
            TaskState.CANCELLED,
        )

    @property
    def is_settled(self) -> bool:
        """Nothing will run this task again without a person asking.

        Terminal, or finished with a degraded result. Not ``quarantined``, which is
        parked for someone to look at, nor ``awaiting_user``: both still expect a
        decision, so what they belong to is not finished.
        """
        return self.is_terminal or self is TaskState.DEGRADED

    @property
    def counts_as_failure(self) -> bool:
        """Waiting for a human is not a failure."""
        return self in (TaskState.FAILED, TaskState.DEAD_LETTER)


class WorkflowLabel(StrEnum):
    """A derived, human-facing summary. Never stored as the source of truth."""

    NEEDS_IDENTITY_DECISION = "needs_identity_decision"
    NEEDS_RELEVANCE_DECISION = "needs_relevance_decision"
    NEEDS_RIGHTS_DECISION = "needs_rights_decision"
    AWAITING_ACQUISITION = "awaiting_acquisition"
    NEEDS_ASSISTED_UPLOAD = "needs_assisted_upload"
    UNAVAILABLE = "unavailable"
    NEEDS_PARSE_REVIEW = "needs_parse_review"
    AWAITING_EXTRACTION = "awaiting_extraction"
    NEEDS_CLAIM_REVIEW = "needs_claim_review"
    RELEASE_READY = "release_ready"
    PUBLISHED = "published"


def derive_workflow_label(
    *,
    identity: IdentityState,
    relevance: RelevanceState,
    rights: RightsState,
    acquisition: AcquisitionState,
    parse: ParseState,
    extraction: ExtractionState,
    review: ReviewState,
    release: ReleaseState,
) -> WorkflowLabel:
    """Collapse the dimensions for display, in priority order."""
    if identity is IdentityState.CONFLICT:
        return WorkflowLabel.NEEDS_IDENTITY_DECISION
    if relevance is RelevanceState.UNREVIEWED:
        return WorkflowLabel.NEEDS_RELEVANCE_DECISION
    if rights in (RightsState.UNKNOWN, RightsState.EXPIRED):
        return WorkflowLabel.NEEDS_RIGHTS_DECISION
    if acquisition is AcquisitionState.ASSISTED:
        return WorkflowLabel.NEEDS_ASSISTED_UPLOAD
    if acquisition is AcquisitionState.UNAVAILABLE:
        return WorkflowLabel.UNAVAILABLE
    if acquisition in (AcquisitionState.UNPLANNED, AcquisitionState.PLANNED):
        return WorkflowLabel.AWAITING_ACQUISITION
    if parse in (ParseState.DEGRADED, ParseState.QUARANTINED, ParseState.FAILED):
        return WorkflowLabel.NEEDS_PARSE_REVIEW
    if extraction in (ExtractionState.PENDING, ExtractionState.NOT_APPLICABLE):
        return WorkflowLabel.AWAITING_EXTRACTION
    if not review.is_approved:
        return WorkflowLabel.NEEDS_CLAIM_REVIEW
    if release is ReleaseState.PUBLISHED:
        return WorkflowLabel.PUBLISHED
    return WorkflowLabel.RELEASE_READY
