"""Immutable scope and configuration versions.

Editing a scope creates another version. A run references a version, so a run's
inputs cannot change under it after the fact.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes, stable_number

__all__ = [
    "ApprovalState",
    "ConfigKind",
    "ConfigVersion",
    "ConnectorPlanEntry",
    "RelevancePolicy",
    "ScopeVersion",
]


class ApprovalState(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    RETIRED = "retired"


class ConfigKind(StrEnum):
    CONNECTOR = "connector"
    PARSER_PROFILE = "parser_profile"
    EXTRACTION_TEMPLATE = "extraction_template"
    PROMPT_BUNDLE = "prompt_bundle"
    RETRIEVAL_PROFILE = "retrieval_profile"
    RIGHTS_POLICY = "rights_policy"
    EMBEDDING_PROFILE = "embedding_profile"
    EVALUATION_BENCHMARK = "evaluation_benchmark"
    BRANDING = "branding"
    WORKSPACE_PROFILE = "workspace_profile"


class ConnectorPlanEntry(BaseModel):
    """One source in a scope's strategy, with its own query.

    Query languages and coverage differ per source, so there is no single query
    string in a scope.
    """

    model_config = {"frozen": True}

    connector_id: str
    connector_version: str
    query: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: A required connector that ends degraded blocks the run from freezing.
    required: bool = True
    page_size: int = 50
    max_pages: int | None = None


class RelevancePolicy(BaseModel):
    model_config = {"frozen": True}

    #: Score at or above which a candidate is included without review.
    include_threshold: float = 0.75
    #: Score below which a candidate is excluded without review.
    exclude_threshold: float = 0.25
    #: What happens between the thresholds. Unknown relevance is never silently
    #: included, which was audit finding H-3.
    uncertain_action: str = "review"
    allow_included_uncertain: bool = False


class ScopeVersion(BaseModel):
    """An immutable, approvable statement of what a collection is for."""

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID
    scope_id: UUID
    version: int
    name: str
    #: The purpose: what belongs and what does not, as prose for people. Never
    #: matched as text; what decides papers is the screening rules below.
    scientific_objective: str
    date_from: date | None = None
    date_to: date | None = None
    languages: tuple[str, ...] = ("en",)
    resource_kinds: tuple[str, ...] = ("article",)
    #: Screening concepts (``domain/screening.py``): one per entry, alternatives
    #: separated by ``|``. The share present in a paper is its relevance score.
    concepts: tuple[str, ...] = ()
    #: Words or phrases whose presence excludes a paper outright.
    exclusion_terms: tuple[str, ...] = ()
    controlled_terms: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    connector_plan: tuple[ConnectorPlanEntry, ...] = ()
    relevance_policy: RelevancePolicy = RelevancePolicy()
    intended_uses: tuple[str, ...] = ()
    gold_seed_identifiers: tuple[str, ...] = ()
    known_negative_identifiers: tuple[str, ...] = ()
    #: Minimum gold-seed recall the scope must reach before production discovery.
    gold_seed_recall_threshold: float = 0.9
    approval_state: ApprovalState = ApprovalState.DRAFT
    authored_by: UUID | None = None
    reviewed_by: UUID | None = None
    created_at: datetime | None = None
    frozen_at: datetime | None = None

    @field_validator("version")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("scope version starts at 1")
        return value

    @property
    def required_connector_ids(self) -> tuple[str, ...]:
        return tuple(entry.connector_id for entry in self.connector_plan if entry.required)

    @property
    def config_checksum(self) -> str:
        """Digest over everything that changes discovery behaviour."""
        document: dict[str, Any] = {
            "scope_id": self.scope_id,
            "version": self.version,
            # What the version is for: a change to it is a different version.
            "purpose": self.scientific_objective,
            "intended_uses": self.intended_uses,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "languages": self.languages,
            "resource_kinds": self.resource_kinds,
            "concepts": self.concepts,
            "exclusion_terms": self.exclusion_terms,
            "controlled_terms": self.controlled_terms,
            "connector_plan": [e.model_dump(mode="python") for e in self.connector_plan],
            "relevance_policy": {
                key: stable_number(value) if isinstance(value, float) else value
                for key, value in self.relevance_policy.model_dump(mode="python").items()
            },
        }
        return sha256_bytes(canonical_json(document))

    @property
    def is_approved(self) -> bool:
        return self.approval_state is ApprovalState.APPROVED

    def connector_entry(self, connector_id: str) -> ConnectorPlanEntry | None:
        for entry in self.connector_plan:
            if entry.connector_id == connector_id:
                return entry
        return None


class ConfigVersion(BaseModel):
    """Generic immutable configuration object.

    Secrets are referenced by secret-manager key name and never appear in
    ``payload``.
    """

    model_config = {"frozen": True}

    id: UUID
    workspace_id: UUID | None
    kind: ConfigKind
    logical_id: str
    semantic_version: str
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = "1.0"
    status: ApprovalState = ApprovalState.DRAFT
    secret_names: tuple[str, ...] = ()
    created_at: datetime | None = None
    created_by: UUID | None = None

    @property
    def checksum(self) -> str:
        return sha256_bytes(canonical_json(self.payload))

    @property
    def logical_key(self) -> str:
        return f"{self.kind.value}:{self.logical_id}@{self.semantic_version}"

    @field_validator("payload")
    @classmethod
    def _no_secret_values(cls, payload: dict[str, Any]) -> dict[str, Any]:
        banned = {"password", "api_key", "secret", "token", "bearer"}
        for key in payload:
            if key.lower() in banned:
                raise ValueError(
                    f"config payload must not contain secret value under {key!r}; "
                    "reference a secret name instead"
                )
        return payload
