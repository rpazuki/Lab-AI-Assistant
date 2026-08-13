"""Manifestations, acquisition, rights, and immutable assets.

An asset is bytes plus a digest plus the evidence that we were allowed to have
them. The gate ``asset_parse_blockers`` is the single place that decides whether
bytes may be processed; there is no other path into parsing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.errors import AcquisitionOutcome
from lab_assistant.contracts.policy import PolicyAction
from lab_assistant.domain.states import RightsState

__all__ = [
    "AcquisitionAttempt",
    "AcquisitionPlan",
    "AcquisitionRoute",
    "Asset",
    "MalwareScanStatus",
    "Manifestation",
    "ManifestationKind",
    "RightsDecision",
    "ValidationStatus",
    "asset_parse_blockers",
]


class ManifestationKind(StrEnum):
    VERSION_OF_RECORD = "version_of_record"
    ACCEPTED_MANUSCRIPT = "accepted_manuscript"
    PREPRINT = "preprint"
    PMC_XML = "pmc_xml"
    PUBLISHER_PDF = "publisher_pdf"
    SUPPLEMENT = "supplement"
    DATASET_FILE = "dataset_file"
    LOCAL_DOCUMENT = "local_document"
    ABSTRACT_ONLY = "abstract_only"
    UNKNOWN = "unknown"


class AcquisitionRoute(StrEnum):
    """Route classes from lifecycle §6. Order expresses preference, not permission."""

    OPEN_REPOSITORY = "open_repository"
    OPEN_ACCESS_API = "open_access_api"
    LICENSED_TDM = "licensed_tdm"
    INSTITUTIONAL_BULK = "institutional_bulk"
    USER_ASSISTED = "user_assisted"
    METADATA_ONLY = "metadata_only"
    PROHIBITED = "prohibited"

    @property
    def is_automatic(self) -> bool:
        return self in (
            AcquisitionRoute.OPEN_REPOSITORY,
            AcquisitionRoute.OPEN_ACCESS_API,
            AcquisitionRoute.LICENSED_TDM,
            AcquisitionRoute.INSTITUTIONAL_BULK,
        )


class MalwareScanStatus(StrEnum):
    NOT_SCANNED = "not_scanned"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED_WITH_EXCEPTION = "skipped_with_exception"
    SCANNER_UNAVAILABLE = "scanner_unavailable"


class ValidationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class Manifestation(BaseModel):
    """A retrievable version or file role of a work."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    work_id: UUID
    manifestation_kind: ManifestationKind = ManifestationKind.UNKNOWN
    version_label: str | None = None
    publication_state: str | None = None
    declared_mime: str | None = None
    language: str | None = None
    relation_assertion_ids: tuple[UUID, ...] = ()
    status: str = "active"
    created_at: datetime | None = None


class RightsDecision(BaseModel):
    """Action-level permission with its evidence and expiry."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    subject_type: str  # manifestation | asset | work | collection
    subject_id: UUID
    policy_version_id: UUID | None = None
    licence_expression: str | None = None
    access_basis: str | None = None
    allowed_actions: frozenset[PolicyAction] = frozenset()
    restrictions: tuple[str, ...] = ()
    restricted_to_group_ids: tuple[UUID, ...] = ()
    evidence_assertion_ids: tuple[UUID, ...] = ()
    decided_by_type: str = "automatic_rule"  # automatic_rule | human_steward | policy_import
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    review_due_at: datetime | None = None
    status: RightsState = RightsState.UNKNOWN
    note: str | None = None

    def permits(self, action: PolicyAction, *, now: datetime | None = None) -> bool:
        if self.status in (RightsState.PROHIBITED, RightsState.UNKNOWN):
            return False
        if now is not None and self.review_due_at is not None and now >= self.review_due_at:
            return False
        return action in self.allowed_actions

    def is_expired(self, now: datetime) -> bool:
        return self.review_due_at is not None and now >= self.review_due_at


class AcquisitionPlan(BaseModel):
    """A frozen plan: which manifestations, which routes, under which policy."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    work_id: UUID
    manifestation_ids: tuple[UUID, ...] = ()
    route_order: tuple[AcquisitionRoute, ...] = ()
    rights_policy_version_id: UUID | None = None
    requested_actions: frozenset[PolicyAction] = frozenset()
    credential_names: tuple[str, ...] = ()
    allow_assisted: bool = True
    requested_by: UUID | None = None
    approved_by: UUID | None = None
    created_at: datetime | None = None
    status: str = "planned"
    version: int = 1


class AcquisitionAttempt(BaseModel):
    """Every attempt, not only the final result (audit finding H-5)."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    acquisition_plan_id: UUID
    manifestation_id: UUID | None
    connector_id: str
    route: AcquisitionRoute
    attempt_number: int = 1
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    outcome: AcquisitionOutcome | None = None
    http_status: int | None = None
    retry_after_seconds: int | None = None
    source_url_redacted: str | None = None
    response_object_id: UUID | None = None
    asset_id: UUID | None = None
    error_class: str | None = None
    note: str | None = None
    worker_version: str = "unknown"


class Asset(BaseModel):
    """Immutable acquired bytes.

    ``object_id`` points at content-addressed storage; ``sha256`` is the address.
    There is no filesystem path on this record, because a path is a
    per-topology address and not an identity.
    """

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    sha256: str
    byte_length: int
    object_id: UUID
    media_type_detected: str
    original_filename_safe: str | None = None
    malware_scan_status: MalwareScanStatus = MalwareScanStatus.NOT_SCANNED
    validation_status: ValidationStatus = ValidationStatus.PENDING
    created_from_attempt_id: UUID | None = None
    manifestation_id: UUID | None = None
    work_id: UUID | None = None
    rights_decision_id: UUID | None = None
    identity_verified: bool = False
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def asset_parse_blockers(
    asset: Asset,
    *,
    rights: RightsDecision | None,
    permitted_media_types: frozenset[str],
    now: datetime,
    require_malware_scan: bool,
) -> tuple[str, ...]:
    """Why ``asset`` may not enter parsing (data model §8.5).

    ``require_malware_scan`` is the proportionality lever from scale §3.2:
    mandatory for uploads and non-allowlisted hosts, and skippable with a
    *recorded* exception for open-access repository fetches. A skipped scan is a
    recorded status, never an absent one.
    """
    blockers: list[str] = []
    if asset.validation_status is ValidationStatus.QUARANTINED:
        blockers.append("asset is quarantined")
    elif asset.validation_status is ValidationStatus.FAILED:
        blockers.append("asset failed validation")
    if asset.media_type_detected not in permitted_media_types:
        blockers.append(f"media type {asset.media_type_detected} is not permitted")
    if require_malware_scan:
        if asset.malware_scan_status is MalwareScanStatus.NOT_SCANNED:
            blockers.append("malware scan has not run")
        elif asset.malware_scan_status in (
            MalwareScanStatus.FAILED,
            MalwareScanStatus.SCANNER_UNAVAILABLE,
        ):
            blockers.append(f"malware scan status is {asset.malware_scan_status.value}")
    elif asset.malware_scan_status is MalwareScanStatus.FAILED:
        blockers.append("malware scan failed")
    if rights is None:
        blockers.append("no rights decision recorded")
    else:
        if rights.subject_id not in (asset.id, asset.manifestation_id, asset.work_id):
            blockers.append("rights decision does not cover this asset")
        if not rights.permits(PolicyAction.PROCESS_LOCAL, now=now):
            blockers.append("rights decision does not permit local processing")
        if rights.is_expired(now):
            blockers.append("rights decision is past its review date")
    if not asset.identity_verified:
        blockers.append("asset identity has not been confirmed against the intended work")
    return tuple(blockers)
