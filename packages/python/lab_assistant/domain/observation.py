"""Discovery provenance: runs, requests, observations, assertions.

Three boundaries, deliberately distinct:

* the **raw response** is the replay boundary — a parser fix is replayed from it;
* the **canonical record** is the parse boundary — one source record, versioned
  parser, addressable;
* the **assertion** is the interpretation boundary — one source saying one thing
  about one field at one time.

The legacy system wrote merged candidates into a file called "raw", which is why
a canonicalisation bug there could not be repaired without re-querying.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import canonical_json, fingerprint, sha256_bytes
from lab_assistant.contracts.errors import ErrorClass, SourceOutcome

__all__ = [
    "AssertionStatus",
    "DiscoveryRun",
    "DiscoveryRunStatus",
    "Predicate",
    "SourceAssertion",
    "SourceObservation",
    "SourceRequest",
    "connector_run_state",
    "run_fingerprint",
]


class Predicate(StrEnum):
    """What a source can assert about a record.

    Extending this list is a schema change with a migration, not a free-form
    string, because canonical metadata resolution is defined per predicate.
    """

    TITLE = "title"
    ABSTRACT = "abstract"
    IDENTIFIER = "identifier"
    RESOURCE_KIND = "resource_kind"
    AUTHOR = "author"
    JOURNAL = "journal"
    PUBLICATION_DATE = "publication_date"
    YEAR = "year"
    LANGUAGE = "language"
    LICENCE = "licence"
    ACCESS_STATUS = "access_status"
    FULL_TEXT_LOCATION = "full_text_location"
    SUBJECT_TERM = "subject_term"
    KEYWORD = "keyword"
    RELATION = "relation"
    RETRACTION = "retraction"
    CORRECTION = "correction"
    VERSION_LABEL = "version_label"
    PUBLISHER = "publisher"
    AFFILIATION = "affiliation"
    FUNDING = "funding"
    CITED_BY_COUNT = "cited_by_count"


class AssertionStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    QUARANTINED = "quarantined"


class DiscoveryRunStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    DEGRADED = "degraded"
    FROZEN = "frozen"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def can_feed_release(self) -> bool:
        """Only a frozen run may be a release input (principle P-9)."""
        return self is DiscoveryRunStatus.FROZEN


def run_fingerprint(
    *,
    scope_checksum: str,
    connector_versions: dict[str, str],
    starting_cursors: dict[str, str | None],
    orchestration_version: str,
) -> str:
    return fingerprint(
        kind="discovery_run",
        parts={
            "scope_checksum": scope_checksum,
            "connector_versions": connector_versions,
            "starting_cursors": starting_cursors,
            "orchestration_version": orchestration_version,
        },
    )


class SourceRequest(BaseModel):
    """One logical upstream request and the object holding its response."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    discovery_run_id: UUID
    connector_id: str
    connector_version: str
    request_fingerprint: str
    method: str
    canonical_url_redacted: str
    request_body_object_id: UUID | None = None
    requested_at: datetime
    completed_at: datetime | None = None
    response_status: int | None = None
    response_headers_allowed: dict[str, str] = Field(default_factory=dict)
    response_body_object_id: UUID | None = None
    response_sha256: str | None = None
    response_byte_length: int | None = None
    #: What ``response_body_object_id`` holds: the source's own bytes, or a
    #: system-built replay envelope. An auditor must not have to guess.
    response_body_kind: str = "upstream_bytes"
    #: The source's bytes when the stored body is an envelope. Separate object, so
    #: deterministic replay never costs provenance.
    upstream_body_object_id: UUID | None = None
    upstream_sha256: str | None = None
    source_cursor_before: str | None = None
    source_cursor_after: str | None = None
    declared_total: int | None = None
    record_count: int = 0
    attempt_number: int = 1
    outcome: SourceOutcome | None = None
    error_class: ErrorClass | None = None


class SourceObservation(BaseModel):
    """One record parsed out of one response."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    source_request_id: UUID
    source_record_locator: str
    parser_id: str
    parser_version: str
    schema_version: str
    canonical_record_object_id: UUID | None = None
    canonical_record_sha256: str | None = None
    observed_at: datetime
    parse_status: str = "parsed"

    @staticmethod
    def record_digest(record: dict[str, Any]) -> str:
        return sha256_bytes(canonical_json(record))


class SourceAssertion(BaseModel):
    """One field-level statement by one source.

    ``subject_hint`` is what the source thinks the record is about (usually a
    normalised identifier). It is a hint precisely because identity has not been
    decided yet.
    """

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    source_observation_id: UUID
    subject_hint: str
    predicate: Predicate
    value_json: Any
    normalised_value_json: Any | None = None
    confidence: float | None = None
    #: Field-authority class used by resolution policy, e.g. "doi_registry",
    #: "biomedical_index", "aggregator", "full_text_asset".
    source_priority_class: str = "aggregator"
    asserted_at_source: datetime | None = None
    valid_from_source: datetime | None = None
    status: AssertionStatus = AssertionStatus.ACTIVE
    work_id: UUID | None = None


class ConnectorRunState(BaseModel):
    """Per-connector progress within one discovery run."""

    model_config = {"validate_assignment": True}

    connector_id: str
    connector_version: str
    required: bool
    query_fingerprint: str
    pages_fetched: int = 0
    records_seen: int = 0
    declared_total: int | None = None
    outcome: SourceOutcome | None = None
    error_class: ErrorClass | None = None
    degraded_reason: str | None = None
    accepted_degraded_by: UUID | None = None
    cursor_after: str | None = None

    @property
    def is_acceptable(self) -> bool:
        if self.outcome is None:
            return False
        if self.outcome.is_acceptable_terminal:
            return True
        return self.accepted_degraded_by is not None

    @property
    def pagination_reconciled(self) -> bool:
        """Declared totals must reconcile with what we actually stored.

        An upstream's paging contract is not the one you assumed; when the
        source declares a total, we check it.
        """
        if self.declared_total is None:
            return True
        return self.records_seen >= self.declared_total


def connector_run_state(entry_connector_id: str, **kwargs: Any) -> ConnectorRunState:
    return ConnectorRunState(connector_id=entry_connector_id, **kwargs)


class DiscoveryRun(BaseModel):
    """One execution of a scope version against connector versions and cursors."""

    model_config = {"validate_assignment": True}

    id: UUID
    workspace_id: UUID
    scope_version_id: UUID
    status: DiscoveryRunStatus = DiscoveryRunStatus.PLANNED
    run_fingerprint: str
    requested_by: UUID
    started_at: datetime | None = None
    completed_at: datetime | None = None
    frozen_at: datetime | None = None
    connector_states: tuple[ConnectorRunState, ...] = ()
    degraded_reason: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    version: int = 1

    def state_for(self, connector_id: str) -> ConnectorRunState | None:
        for state in self.connector_states:
            if state.connector_id == connector_id:
                return state
        return None

    def freeze_blockers(self) -> tuple[str, ...]:
        """Why this run may not freeze as complete (lifecycle §4 quality gate)."""
        blockers: list[str] = []
        for state in self.connector_states:
            if state.outcome is None:
                blockers.append(f"{state.connector_id}: no terminal outcome")
                continue
            if state.required and not state.is_acceptable:
                blockers.append(
                    f"{state.connector_id}: {state.outcome.value} not accepted by a steward"
                )
            if not state.pagination_reconciled:
                blockers.append(
                    f"{state.connector_id}: pagination integrity "
                    f"({state.records_seen} of declared {state.declared_total})"
                )
        if not self.connector_states:
            blockers.append("no connector was executed")
        return tuple(blockers)

    @property
    def can_freeze(self) -> bool:
        return not self.freeze_blockers()
