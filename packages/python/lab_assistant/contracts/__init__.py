"""Contracts: schemas and taxonomies shared by every layer.

Contracts may import the standard library and pydantic. Nothing else from the
project, and no external SDK.
"""

from lab_assistant.contracts.canonical import (
    canonical_json,
    fingerprint,
    sha256_bytes,
    sha256_text,
)
from lab_assistant.contracts.errors import (
    AbstentionReason,
    AcquisitionOutcome,
    ErrorClass,
    LabAssistantError,
    PolicyDenied,
    ProblemDetails,
    RetryEligibility,
    SourceOutcome,
    StateTransitionError,
    ValidationFailure,
)
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    PolicyDecision,
    SubjectRef,
)
from lab_assistant.contracts.stage import (
    StageCommand,
    StageHandler,
    StageName,
    StageResult,
)

__all__ = [
    "AbstentionReason",
    "AcquisitionOutcome",
    "Effect",
    "ErrorClass",
    "LabAssistantError",
    "PolicyAction",
    "PolicyDecision",
    "PolicyDenied",
    "ProblemDetails",
    "RetryEligibility",
    "SourceOutcome",
    "StageCommand",
    "StageHandler",
    "StageName",
    "StageResult",
    "StateTransitionError",
    "SubjectRef",
    "ValidationFailure",
    "canonical_json",
    "fingerprint",
    "sha256_bytes",
    "sha256_text",
]
