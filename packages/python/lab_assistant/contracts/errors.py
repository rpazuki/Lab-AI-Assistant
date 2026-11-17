"""Stable error and outcome taxonomy.

The legacy system collapsed distinguishable states into ``pending`` and
``failed``, and conflated "the source says nothing" with "we never looked".
Every taxonomy here exists because that conflation cost real information.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "AbstentionReason",
    "AcquisitionOutcome",
    "ErrorClass",
    "InstallationDenied",
    "LabAssistantError",
    "PolicyDenied",
    "ProblemDetails",
    "RetryEligibility",
    "SourceOutcome",
    "StateTransitionError",
    "ValidationFailure",
    "is_retryable",
]


class ErrorClass(StrEnum):
    """Why something failed, at the granularity retries care about."""

    RATE_LIMITED = "rate_limited"
    SOURCE_UNAVAILABLE = "source_unavailable"
    TIMEOUT = "timeout"
    AUTHENTICATION_REQUIRED = "authentication_required"
    ENTITLEMENT_REQUIRED = "entitlement_required"
    POLICY_BLOCKED = "policy_blocked"
    ROBOTS_BLOCKED = "robots_blocked"
    INVALID_QUERY = "invalid_query"
    INVALID_CONTENT = "invalid_content"
    IDENTITY_MISMATCH = "identity_mismatch"
    MALWARE_DETECTED = "malware_detected"
    TOO_LARGE = "too_large"
    NOT_FOUND = "not_found"
    #: Storage answered, and refused. Distinct from NOT_FOUND because a reader
    #: whose bucket permissions are wrong must not conclude the evidence is gone
    #: and reacquire it, and distinct from AUTHENTICATION_REQUIRED because the
    #: credential may be perfectly valid for a principal this object denies.
    STORAGE_DENIED = "storage_denied"
    #: The object exists in a cold class and is being restored. Not missing, not
    #: corrupt, not a reason to re-analyse: a named wait.
    STORAGE_RESTORE_PENDING = "storage_restore_pending"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_REFUSAL = "provider_refusal"
    SCHEMA_VIOLATION = "schema_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    CONFLICT = "conflict"
    INTERNAL = "internal"
    CANCELLED = "cancelled"


class RetryEligibility(StrEnum):
    RETRYABLE = "retryable"
    RETRYABLE_AFTER = "retryable_after"
    HUMAN_ACTION = "human_action"
    PERMANENT = "permanent"


_RETRY_TABLE: dict[ErrorClass, RetryEligibility] = {
    ErrorClass.RATE_LIMITED: RetryEligibility.RETRYABLE_AFTER,
    ErrorClass.SOURCE_UNAVAILABLE: RetryEligibility.RETRYABLE,
    ErrorClass.TIMEOUT: RetryEligibility.RETRYABLE,
    ErrorClass.PROVIDER_ERROR: RetryEligibility.RETRYABLE,
    ErrorClass.CONFLICT: RetryEligibility.RETRYABLE,
    ErrorClass.AUTHENTICATION_REQUIRED: RetryEligibility.HUMAN_ACTION,
    ErrorClass.STORAGE_DENIED: RetryEligibility.HUMAN_ACTION,
    ErrorClass.STORAGE_RESTORE_PENDING: RetryEligibility.RETRYABLE_AFTER,
    ErrorClass.ENTITLEMENT_REQUIRED: RetryEligibility.HUMAN_ACTION,
    ErrorClass.BUDGET_EXCEEDED: RetryEligibility.HUMAN_ACTION,
    ErrorClass.MALWARE_DETECTED: RetryEligibility.PERMANENT,
    ErrorClass.POLICY_BLOCKED: RetryEligibility.PERMANENT,
    ErrorClass.ROBOTS_BLOCKED: RetryEligibility.PERMANENT,
    ErrorClass.INVALID_QUERY: RetryEligibility.PERMANENT,
    ErrorClass.INVALID_CONTENT: RetryEligibility.PERMANENT,
    ErrorClass.IDENTITY_MISMATCH: RetryEligibility.PERMANENT,
    ErrorClass.TOO_LARGE: RetryEligibility.PERMANENT,
    ErrorClass.NOT_FOUND: RetryEligibility.PERMANENT,
    ErrorClass.PROVIDER_REFUSAL: RetryEligibility.PERMANENT,
    ErrorClass.SCHEMA_VIOLATION: RetryEligibility.PERMANENT,
    ErrorClass.INTERNAL: RetryEligibility.RETRYABLE,
    ErrorClass.CANCELLED: RetryEligibility.PERMANENT,
}


def is_retryable(error_class: ErrorClass) -> bool:
    return _RETRY_TABLE[error_class] in (
        RetryEligibility.RETRYABLE,
        RetryEligibility.RETRYABLE_AFTER,
    )


def retry_eligibility(error_class: ErrorClass) -> RetryEligibility:
    return _RETRY_TABLE[error_class]


class SourceOutcome(StrEnum):
    """Terminal state of one connector within one discovery run (lifecycle §4)."""

    SUCCEEDED = "succeeded"
    SUCCEEDED_EMPTY = "succeeded_empty"
    DEGRADED_PARTIAL = "degraded_partial"
    RATE_LIMITED_RETRYABLE = "rate_limited_retryable"
    AUTHENTICATION_REQUIRED = "authentication_required"
    POLICY_BLOCKED = "policy_blocked"
    SOURCE_UNAVAILABLE_RETRYABLE = "source_unavailable_retryable"
    INVALID_QUERY = "invalid_query"
    FAILED_PERMANENT = "failed_permanent"
    CANCELLED = "cancelled"

    @property
    def is_acceptable_terminal(self) -> bool:
        """Only these let a run freeze as complete without steward acceptance."""
        return self in (SourceOutcome.SUCCEEDED, SourceOutcome.SUCCEEDED_EMPTY)

    @property
    def is_terminal(self) -> bool:
        return self not in (
            SourceOutcome.RATE_LIMITED_RETRYABLE,
            SourceOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
        )


class AcquisitionOutcome(StrEnum):
    """Outcome of one acquisition attempt (lifecycle §7)."""

    ACQUIRED = "acquired"
    ALREADY_AVAILABLE = "already_available"
    NOT_FOUND = "not_found"
    METADATA_ONLY = "metadata_only"
    RATE_LIMITED_RETRYABLE = "rate_limited_retryable"
    SOURCE_UNAVAILABLE_RETRYABLE = "source_unavailable_retryable"
    AUTHENTICATION_REQUIRED = "authentication_required"
    ENTITLEMENT_REQUIRED = "entitlement_required"
    ROBOTS_OR_POLICY_BLOCKED = "robots_or_policy_blocked"
    LICENCE_NOT_PERMITTED = "licence_not_permitted"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_CONTENT = "invalid_content"
    MALWARE_QUARANTINED = "malware_quarantined"
    TOO_LARGE = "too_large"
    ASSISTED_REQUIRED = "assisted_required"
    FAILED_PERMANENT = "failed_permanent"
    CANCELLED = "cancelled"

    @property
    def produced_asset(self) -> bool:
        return self in (AcquisitionOutcome.ACQUIRED, AcquisitionOutcome.ALREADY_AVAILABLE)

    @property
    def needs_human(self) -> bool:
        return self in (
            AcquisitionOutcome.ASSISTED_REQUIRED,
            AcquisitionOutcome.AUTHENTICATION_REQUIRED,
            AcquisitionOutcome.ENTITLEMENT_REQUIRED,
        )


class AbstentionReason(StrEnum):
    """Why a template target has no reported value (lifecycle §9 step 5).

    Only ``REPORTED`` and ``NOT_REPORTED_AFTER_COMPLETE_SEARCH`` may support a
    positive or negative statement about source content.
    """

    REPORTED = "reported"
    NOT_REPORTED_AFTER_COMPLETE_SEARCH = "not_reported_after_complete_search"
    NOT_SEARCHED_BUDGET = "not_searched_budget"
    NOT_SEARCHED_POLICY = "not_searched_policy"
    SOURCE_UNAVAILABLE = "source_unavailable"
    PARSE_QUALITY_INSUFFICIENT = "parse_quality_insufficient"
    AMBIGUOUS_MULTIPLE_VALUES = "ambiguous_multiple_values"
    CONFLICTING_VALUES = "conflicting_values"
    EXTRACTOR_ABSTAINED = "extractor_abstained"
    NOT_APPLICABLE = "not_applicable"
    POLICY_REDACTED = "policy_redacted"

    @property
    def supports_source_statement(self) -> bool:
        return self in (
            AbstentionReason.REPORTED,
            AbstentionReason.NOT_REPORTED_AFTER_COMPLETE_SEARCH,
        )


class LabAssistantError(Exception):
    """Base class for domain-meaningful failures.

    ``error_class`` decides retry behaviour, so every raise site picks one
    deliberately rather than letting a generic exception become ``INTERNAL``.
    """

    error_class: ErrorClass = ErrorClass.INTERNAL
    http_status: int = 500

    def __init__(self, detail: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.context = context or {}

    @property
    def retry_eligibility(self) -> RetryEligibility:
        return retry_eligibility(self.error_class)

    def with_class(self, error_class: ErrorClass) -> LabAssistantError:
        """Attach a taxonomy class at the raise site.

        Adapters map an upstream failure onto the taxonomy in one line, rather
        than defining a subclass per upstream error code.
        """
        self.error_class = error_class
        return self

    def to_problem(self, *, instance: str | None = None) -> ProblemDetails:
        return ProblemDetails(
            type=f"https://lab-ai-assistant.invalid/problems/{self.error_class.value}",
            title=self.error_class.value.replace("_", " "),
            status=self.http_status,
            detail=self.detail,
            error_class=self.error_class,
            instance=instance,
            context=self.context,
        )


class ValidationFailure(LabAssistantError):
    error_class = ErrorClass.SCHEMA_VIOLATION
    http_status = 422


class PolicyDenied(LabAssistantError):
    """A policy decision denied the action.

    The message is deliberately free of confidential policy internals; the
    decision record carries the reasoning (connectors §9).
    """

    error_class = ErrorClass.POLICY_BLOCKED
    http_status = 403


class InstallationDenied(LabAssistantError):
    """This *deployment* may not perform the operation, whoever asked.

    Separate from `PolicyDenied`, which is about the caller. The distinction is
    the whole point: an admin on a reader installation is not short of a grant
    and will not get one by being promoted, because the machine is not the
    publisher. A client that could not tell these apart would offer the user a
    permissions dialog for something no permission can fix.

    501 rather than 403: the operation is not implemented *here*. The capability
    lives on another installation, and that is a property of the deployment
    rather than of the request.
    """

    error_class = ErrorClass.POLICY_BLOCKED
    http_status = 501


class StateTransitionError(LabAssistantError):
    error_class = ErrorClass.CONFLICT
    http_status = 409


class NotFound(LabAssistantError):
    error_class = ErrorClass.NOT_FOUND
    http_status = 404


class ConcurrencyConflict(LabAssistantError):
    """Optimistic concurrency failure on a reviewer edit."""

    error_class = ErrorClass.CONFLICT
    http_status = 409


class BudgetExceeded(LabAssistantError):
    error_class = ErrorClass.BUDGET_EXCEEDED
    http_status = 429


class ProblemDetails(BaseModel):
    """RFC 9457-shaped error envelope used by every API route."""

    type: str
    title: str
    status: int
    detail: str
    error_class: ErrorClass
    instance: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
