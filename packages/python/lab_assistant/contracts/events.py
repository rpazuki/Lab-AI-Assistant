"""Audit and outbox event contracts.

Audit events map onto PROV concepts: entities (observation, asset, rendition,
claim, release), activities (request, merge, parse, extract, review, publish),
and agents (user, connector, worker version, model provider). A full RDF export
is optional; retaining the lineage relationally is not.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

__all__ = [
    "ActorType",
    "AuditAction",
    "AuditEventRecord",
    "OutboxEvent",
]


class ActorType(StrEnum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    WORKER = "worker"
    CONNECTOR = "connector"
    MODEL_PROVIDER = "model_provider"
    SYSTEM = "system"


class AuditAction(StrEnum):
    # authentication and membership
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    SESSION_REVOKED = "session_revoked"
    #: A password was set by an operator; every earlier sign-in stops working.
    PASSWORD_CHANGED = "password_changed"
    INVITATION_CREATED = "invitation_created"
    INVITATION_ACCEPTED = "invitation_accepted"
    MEMBERSHIP_CHANGED = "membership_changed"
    GRANT_CHANGED = "grant_changed"
    #: An admin opened a member's conversation. Recorded on every read.
    MEMBER_CHAT_READ = "member_chat_read"
    #: A revoked or expired invitation was removed from the list.
    INVITATION_DELETED = "invitation_deleted"
    # discovery and identity
    SCOPE_APPROVED = "scope_approved"
    #: An unapproved scope version was deleted. Nothing had searched with it.
    SCOPE_DRAFT_DELETED = "scope_draft_deleted"
    #: A whole scope was deleted: every version, its runs, and the papers only it
    #: found. Refused once a release past draft, a question or an evaluation used it.
    SCOPE_DELETED = "scope_deleted"
    DISCOVERY_RUN_STARTED = "discovery_run_started"
    DISCOVERY_RUN_FROZEN = "discovery_run_frozen"
    #: A discovery run that contributed no candidates was deleted, with its pages.
    DISCOVERY_RUN_DELETED = "discovery_run_deleted"
    DEGRADED_SOURCE_ACCEPTED = "degraded_source_accepted"
    IDENTITY_DECIDED = "identity_decided"
    RELEVANCE_DECIDED = "relevance_decided"
    #: A steward applied an include or exclude rule to every matching candidate.
    RELEVANCE_BULK_DECIDED = "relevance_bulk_decided"
    #: A steward merged every duplicate pair a merge rule accepted.
    IDENTITIES_BULK_MERGED = "identities_bulk_merged"
    #: A reviewer marked a proposed duplicate pair as two different papers.
    DUPLICATE_DISMISSED = "duplicate_dismissed"
    #: A steward re-applied an approved scope version's screening rules.
    SCOPE_RULES_APPLIED = "scope_rules_applied"
    # rights and acquisition
    RIGHTS_DECIDED = "rights_decided"
    ACQUISITION_ATTEMPTED = "acquisition_attempted"
    #: Fetches waiting for a person were put back in the queue to try again.
    ACQUISITION_REQUEUED = "acquisition_requeued"
    #: A stored file was confirmed to be its paper, by identifier or by title.
    ASSET_IDENTITY_CONFIRMED = "asset_identity_confirmed"
    #: The parse-quality limits were changed, or degraded documents re-checked against them.
    PARSE_LIMITS_CHANGED = "parse_limits_changed"
    ABSTRACT_FALLBACK_CHANGED = "abstract_fallback_changed"
    ABSTRACT_DOCUMENTS_BUILT = "abstract_documents_built"
    ASSET_REGISTERED = "asset_registered"
    ASSET_QUARANTINED = "asset_quarantined"
    ASSISTED_UPLOAD_ACCEPTED = "assisted_upload_accepted"
    # parsing and extraction
    PARSE_COMPLETED = "parse_completed"
    RENDITION_ACCEPTED_DEGRADED = "rendition_accepted_degraded"
    EXTRACTION_COMPLETED = "extraction_completed"
    PROPOSAL_REJECTED = "proposal_rejected"
    #: An extraction run none of whose batches succeeded was deleted.
    EXTRACTION_RUN_DELETED = "extraction_run_deleted"
    #: A draft extraction template version nothing had used was deleted.
    EXTRACTION_TEMPLATE_DELETED = "extraction_template_deleted"
    # review and release
    CLAIM_REVIEWED = "claim_reviewed"
    RELEASE_DRAFTED = "release_drafted"
    #: A draft or rejected release, never approved, was deleted.
    RELEASE_DRAFT_DELETED = "release_draft_deleted"
    RELEASE_VALIDATED = "release_validated"
    RELEASE_APPROVED = "release_approved"
    RELEASE_PUBLISHED = "release_published"
    RELEASE_ROLLED_BACK = "release_rolled_back"
    RELEASE_WITHDRAWN = "release_withdrawn"
    BENCHMARK_REGISTERED = "benchmark_registered"
    #: A benchmark version no run had measured against was deleted.
    BENCHMARK_DELETED = "benchmark_deleted"
    EVALUATION_REQUESTED = "evaluation_requested"
    EVALUATION_COMPLETED = "evaluation_completed"
    EVALUATION_CANCELLED = "evaluation_cancelled"
    #: A failed or cancelled evaluation run was deleted.
    EVALUATION_RUN_DELETED = "evaluation_run_deleted"
    EVALUATION_REVIEW_ASSIGNED = "evaluation_review_assigned"
    EVALUATION_REVIEW_SUBMITTED = "evaluation_review_submitted"
    RELEASE_GATE_CHANGED = "release_gate_changed"
    #: A release was published although a required benchmark had not passed.
    RELEASE_GATE_OVERRIDDEN = "release_gate_overridden"
    # serving and export
    ANSWER_GENERATED = "answer_generated"
    CITATION_VALIDATION_FAILED = "citation_validation_failed"
    EXPORT_CREATED = "export_created"
    EXPORT_DOWNLOADED = "export_downloaded"
    ASSET_DOWNLOADED = "asset_downloaded"
    POLICY_DENIED = "policy_denied"
    # operations
    TASK_DEAD_LETTERED = "task_dead_lettered"
    OBJECT_INTEGRITY_FAILED = "object_integrity_failed"
    ADMIN_ACTION = "admin_action"
    #: A publisher drained for a handover, and the statement it signed off with.
    INSTALLATION_DRAINED = "installation_drained"
    #: The right to publish a library moved to a new epoch and owner.
    INSTALLATION_PROMOTED = "installation_promoted"
    #: Archive keys deleted under a reviewed retention plan.
    ARCHIVE_PURGED = "archive_purged"
    LEGACY_IMPORT_RUN = "legacy_import_run"


class AuditEventRecord(BaseModel):
    """Append-only audit row.

    ``integrity_hash`` chains to the previous event for the same workspace, so a
    deleted or reordered row is detectable.
    """

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID | None
    occurred_at: datetime
    actor_type: ActorType
    actor_id: UUID | None
    action: AuditAction
    subject_type: str
    subject_id: UUID | None
    run_id: UUID | None = None
    request_id: str | None = None
    reason_code: str | None = None
    before_object_id: UUID | None = None
    after_object_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    previous_integrity_hash: str | None = None
    integrity_hash: str = ""

    def compute_integrity_hash(self) -> str:
        return sha256_bytes(
            canonical_json(
                {
                    "id": self.id,
                    "workspace_id": self.workspace_id,
                    "occurred_at": self.occurred_at,
                    "actor_type": self.actor_type,
                    "actor_id": self.actor_id,
                    "action": self.action,
                    "subject_type": self.subject_type,
                    "subject_id": self.subject_id,
                    "run_id": self.run_id,
                    "reason_code": self.reason_code,
                    "metadata": self.metadata,
                    "previous": self.previous_integrity_hash,
                }
            )
        )

    def with_integrity(self, previous_hash: str | None) -> AuditEventRecord:
        candidate = self.model_copy(update={"previous_integrity_hash": previous_hash})
        return candidate.model_copy(update={"integrity_hash": candidate.compute_integrity_hash()})

    def verify(self) -> bool:
        return self.integrity_hash == self.compute_integrity_hash()


class OutboxEvent(BaseModel):
    """Transactional outbox row: a side effect registered with the write."""

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID | None
    topic: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    published_at: datetime | None = None
    attempts: int = 0
