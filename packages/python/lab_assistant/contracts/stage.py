"""The shared stage contract (lifecycle §2).

Every lifecycle stage is a handler over immutable inputs. A handler must be safe
under at-least-once execution: the task engine may deliver the same command
twice, and the second delivery must produce one logical result.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field

from lab_assistant.contracts.errors import ErrorClass, RetryEligibility

__all__ = [
    "StageCommand",
    "StageHandler",
    "StageName",
    "StageResult",
    "StageStatus",
    "TaskRequest",
]


class StageName(StrEnum):
    SCOPE_FREEZE = "scope_freeze"
    DISCOVERY = "discovery"
    RECONCILIATION = "reconciliation"
    ACQUISITION_PLANNING = "acquisition_planning"
    ACQUISITION = "acquisition"
    PARSING = "parsing"
    EXTRACTION = "extraction"
    REVIEW = "review"
    EXPORT = "export"
    RELEASE_ASSEMBLY = "release_assembly"
    INDEXING = "indexing"
    EVALUATION = "evaluation"
    PUBLICATION = "publication"
    REFRESH = "refresh"

    @property
    def worker_class(self) -> str:
        """Which worker deployment runs this stage.

        Two deployments, not six (scale §3.2): network-bound and compute-bound.
        Per-stage concurrency caps, not deployment count, are the mechanism that
        stops a long provider call starving another queue.
        """
        network = {
            StageName.DISCOVERY,
            StageName.ACQUISITION,
            StageName.EXTRACTION,
        }
        return "network" if self in network else "compute"


class StageStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SUCCEEDED_NOOP = "succeeded_noop"
    DEGRADED = "degraded"
    AWAITING_USER = "awaiting_user"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_success(self) -> bool:
        return self in (StageStatus.SUCCEEDED, StageStatus.SUCCEEDED_NOOP)


class StageCommand(BaseModel):
    """Immutable input to one stage execution."""

    model_config = {"frozen": True}

    stage: StageName
    workspace_id: UUID
    #: Who or what asked for this work. A worker acting on its own is the
    #: workspace service account, never an anonymous actor.
    actor_id: UUID
    #: Immutable input references: aggregate ids and object digests.
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: Config/policy version ids in force for this execution.
    config_version_ids: dict[str, UUID] = Field(default_factory=dict)
    policy_version_id: UUID | None = None
    run_id: UUID | None = None
    idempotency_key: str | None = None
    deadline: datetime | None = None
    #: Token/request budget for stages that call an external provider.
    budget: dict[str, int] = Field(default_factory=dict)
    correlation_id: str | None = None


class TaskRequest(BaseModel):
    """A follow-on task a handler asks the orchestrator to schedule."""

    model_config = {"frozen": True}

    stage: StageName
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: Tasks that must reach a successful terminal state first.
    depends_on: tuple[UUID, ...] = ()
    not_before: datetime | None = None
    idempotency_key: str | None = None


class StageResult(BaseModel):
    """Immutable output of one stage execution."""

    model_config = {"frozen": True}

    stage: StageName
    status: StageStatus
    #: Immutable objects created by this execution (content-addressed keys).
    created_object_ids: tuple[UUID, ...] = ()
    #: Aggregates changed, with the version observed after the change.
    changed_aggregates: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    quality_gates: dict[str, bool] = Field(default_factory=dict)
    next_tasks: tuple[TaskRequest, ...] = ()
    human_action_required: str | None = None
    error_class: ErrorClass | None = None
    error_detail: str | None = None
    retry_eligibility: RetryEligibility | None = None
    retry_after_seconds: int | None = None
    audit_event_ids: tuple[UUID, ...] = ()

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(name for name, passed in sorted(self.quality_gates.items()) if not passed)


@runtime_checkable
class StageHandler(Protocol):
    """Contract every stage implementation satisfies."""

    stage_name: StageName
    implementation_version: str

    def fingerprint(self, command: StageCommand) -> str:
        """Stable digest of everything that determines this execution's output."""
        ...

    async def execute(self, command: StageCommand) -> StageResult: ...
