"""Chat sessions, messages, citations, and feedback.

The familiar researcher experience is preserved — sessions list, restore, rename,
delete, auto-title, feedback — and every answer additionally records the release,
retrieval profile, policy snapshot, prompt bundle, model revision, and the
citation handles the server supplied (feature-parity register, delivery §5).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "ChatMessage",
    "ChatSession",
    "Citation",
    "CitationStatus",
    "Feedback",
    "MessageRole",
    "UsageTotals",
    "derive_session_title",
]

_TITLE_MAX = 60


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class CitationStatus(StrEnum):
    VALID = "valid"
    #: The model returned a handle the server never supplied. The answer is
    #: flagged, not failed.
    NOT_SUPPLIED = "not_supplied"
    #: The span exists but its release or rights state changed since generation.
    SUPERSEDED = "superseded"
    RIGHTS_WITHDRAWN = "rights_withdrawn"
    #: Imported legacy citation that cannot be resolved to a verified span.
    LEGACY_UNVERIFIED = "legacy_unverified"


def derive_session_title(first_message: str) -> str:
    """Auto-title a session from its first message.

    Kept because users rely on it (feature-parity register: "auto-generated
    session title from first message", previously untracked in the plan).
    """
    collapsed = " ".join(first_message.split())
    if not collapsed:
        return "New session"
    if len(collapsed) <= _TITLE_MAX:
        return collapsed
    cut = collapsed[:_TITLE_MAX].rsplit(" ", 1)[0]
    return f"{cut or collapsed[:_TITLE_MAX]}…"


class Citation(BaseModel):
    model_config = {"frozen": True}

    handle: str
    evidence_span_id: UUID | None = None
    claim_revision_id: UUID | None = None
    work_id: UUID | None = None
    rendition_id: UUID | None = None
    release_id: UUID | None = None
    status: CitationStatus = CitationStatus.VALID
    display_label: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None


class ChatSession(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    user_id: UUID
    title: str | None = None
    #: Pinning a release makes a conversation reproducible; None follows the alias.
    pinned_release_id: UUID | None = None
    retrieval_profile_id: str = "default"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    version: int = 1

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class ChatMessage(BaseModel):
    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    session_id: UUID
    role: MessageRole
    content: str
    #: Everything needed to reproduce or audit this answer.
    release_id: UUID | None = None
    retrieval_profile_version: str | None = None
    query_classification: str | None = None
    rewritten_queries: tuple[str, ...] = ()
    retrieved_span_ids: tuple[UUID, ...] = ()
    retrieved_claim_revision_ids: tuple[UUID, ...] = ()
    retrieval_scores: dict[str, float] = Field(default_factory=dict)
    policy_snapshot_id: UUID | None = None
    prompt_bundle_id: str | None = None
    prompt_bundle_version: str | None = None
    provider: str | None = None
    llm_model: str | None = None
    model_revision: str | None = None
    citations: tuple[Citation, ...] = ()
    citation_validation_passed: bool = True
    unsupported_citation_handles: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    latency_ms: int = 0
    created_at: datetime | None = None

    @property
    def has_flagged_citations(self) -> bool:
        return bool(self.unsupported_citation_handles) or any(
            citation.status is not CitationStatus.VALID for citation in self.citations
        )


class UsageTotals(BaseModel):
    """One member's chat activity in a workspace, summed over every session.

    Deleted sessions still count: deleting a conversation removes it from view,
    not from the token allowance it used, or deleting would reset the allowance.
    """

    model_config = {"frozen": True}

    sessions: int = 0
    questions: int = 0
    answers: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_active_at: datetime | None = None
    avg_latency_ms: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Feedback(BaseModel):
    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    message_id: UUID
    user_id: UUID
    rating: int
    comment: str | None = None
    flagged_citation_handles: tuple[str, ...] = ()
    created_at: datetime | None = None
