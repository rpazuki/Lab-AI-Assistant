"""Stage 0: scope and strategy.

A scope becomes runnable only when its gate passes: required fields validate,
every required connector compiles its query, gold-seed recall reaches the
workspace threshold, and the intended use is approved. Until then, discovery
refuses to start rather than producing a run whose inputs nobody agreed to.

A version is immutable once approved: discovery runs and releases name it. Until
then nothing refers to it, so a draft may be corrected in place or deleted.
Approved and retired versions refuse both; changing one means a new version.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.ports import UnitOfWork
from lab_assistant.contracts.connector import ConnectorOperation
from lab_assistant.contracts.errors import NotFound, StateTransitionError, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.scope import ApprovalState, ConnectorPlanEntry, ScopeVersion
from lab_assistant.domain.workspace import Capability

__all__ = ["ScopeGateReport", "ScopeService"]


@dataclass(frozen=True, slots=True)
class ScopeGateReport:
    """Why a scope may or may not run production discovery."""

    scope_version_id: UUID
    blockers: tuple[str, ...]
    compiled_connectors: tuple[str, ...]
    gold_seed_recall: float | None
    estimated_requests: int | None

    @property
    def passed(self) -> bool:
        return not self.blockers


class ScopeService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def create_version(
        self,
        actor: ActorContext,
        *,
        scope_id: UUID | None,
        name: str,
        scientific_objective: str,
        connector_plan: list[ConnectorPlanEntry],
        **fields: object,
    ) -> ScopeVersion:
        """Create the next immutable version of a scope."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.MANAGE_SCOPE)
        context = self._context
        async with context.uow_factory() as uow:
            resolved_scope_id = scope_id or context.new_id()
            previous = await uow.configs.latest_scope_version(actor.workspace_id, resolved_scope_id)
            version = (previous.version + 1) if previous else 1
            scope = ScopeVersion(
                id=context.new_id(),
                workspace_id=actor.workspace_id,
                scope_id=resolved_scope_id,
                version=version,
                name=name,
                scientific_objective=scientific_objective,
                connector_plan=tuple(connector_plan),
                authored_by=actor.user_id,
                created_at=context.now(),
                **fields,  # type: ignore[arg-type]
            )
            await uow.configs.add_scope_version(scope)
            await uow.commit()
        return scope

    async def update_draft(
        self,
        actor: ActorContext,
        *,
        scope_version_id: UUID,
        name: str,
        scientific_objective: str,
        connector_plan: list[ConnectorPlanEntry],
        **fields: object,
    ) -> ScopeVersion:
        """Correct an unapproved version in place: same id, same version number."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.MANAGE_SCOPE)
        async with self._context.uow_factory() as uow:
            current = await self._unapproved(uow, actor, scope_version_id, "edited")
            corrected = current.model_copy(
                update={
                    "name": name,
                    "scientific_objective": scientific_objective,
                    "connector_plan": tuple(connector_plan),
                    **fields,
                }
            )
            # Revalidate: model_copy does not, and the payload is stored as given.
            corrected = ScopeVersion.model_validate(corrected.model_dump())
            await uow.configs.add_scope_version(corrected)
            await uow.commit()
        return corrected

    async def delete_draft(self, actor: ActorContext, *, scope_version_id: UUID) -> None:
        """Delete an unapproved version, and record that it existed."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.MANAGE_SCOPE)
        context = self._context
        async with context.uow_factory() as uow:
            current = await self._unapproved(uow, actor, scope_version_id, "deleted")
            await uow.configs.delete_scope_version(actor.workspace_id, scope_version_id)
            event = context.audit.build(
                workspace_id=actor.workspace_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                action=AuditAction.SCOPE_DRAFT_DELETED,
                subject_type="scope_version",
                subject_id=current.id,
                previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                reason_code="draft_deleted",
                metadata={
                    "scope_id": str(current.scope_id),
                    "version": current.version,
                    "name": current.name,
                    "checksum": current.config_checksum,
                },
            )
            await uow.audit.append(event)
            await uow.commit()

    @staticmethod
    async def _unapproved(
        uow: UnitOfWork, actor: ActorContext, scope_version_id: UUID, verb: str
    ) -> ScopeVersion:
        scope = await uow.configs.get_scope_version(actor.workspace_id, scope_version_id)
        if scope is None:
            raise NotFound(f"scope version {scope_version_id} not found")
        if scope.approval_state in (ApprovalState.APPROVED, ApprovalState.RETIRED):
            raise StateTransitionError(
                f"an {scope.approval_state.value} scope version cannot be {verb}; "
                "save a new version instead"
            )
        return scope

    async def evaluate_gate(
        self,
        actor: ActorContext,
        *,
        scope_version_id: UUID,
        gold_seed_hits: int | None = None,
    ) -> ScopeGateReport:
        """Run the stage-0 gate without side effects."""
        context = self._context
        async with context.uow_factory() as uow:
            scope = await uow.configs.get_scope_version(actor.workspace_id, scope_version_id)
        if scope is None:
            raise NotFound(f"scope version {scope_version_id} not found")

        blockers: list[str] = []
        compiled: list[str] = []
        if not scope.connector_plan:
            blockers.append("scope has no connector plan")
        if not scope.inclusion_criteria:
            blockers.append("scope has no inclusion criteria")
        if not scope.intended_uses:
            blockers.append("scope does not state its intended uses")

        registry = context.connectors
        for entry in scope.connector_plan:
            if registry is None:
                blockers.append("no connector registry is configured")
                break
            try:
                connector = registry.get(entry.connector_id)
            except LookupError:
                blockers.append(f"connector {entry.connector_id} is not registered")
                continue
            if not connector.capabilities.supports(ConnectorOperation.DISCOVER):
                blockers.append(f"connector {entry.connector_id} cannot discover")
                continue
            try:
                await connector.compile_query(
                    {
                        "query": entry.query,
                        "parameters": entry.parameters,
                        "scope": scope.model_dump(mode="json"),
                    }
                )
            except Exception as exc:  # a compile failure is a scope defect
                blockers.append(f"connector {entry.connector_id} query does not compile: {exc}")
                continue
            compiled.append(entry.connector_id)

        recall: float | None = None
        if scope.gold_seed_identifiers:
            if gold_seed_hits is None:
                blockers.append("gold seed recall has not been measured")
            else:
                recall = gold_seed_hits / len(scope.gold_seed_identifiers)
                if recall < scope.gold_seed_recall_threshold:
                    blockers.append(
                        f"gold seed recall {recall:.2f} below threshold "
                        f"{scope.gold_seed_recall_threshold:.2f}"
                    )

        estimated = (
            sum((entry.max_pages or 10) for entry in scope.connector_plan)
            if scope.connector_plan
            else None
        )

        return ScopeGateReport(
            scope_version_id=scope_version_id,
            blockers=tuple(blockers),
            compiled_connectors=tuple(compiled),
            gold_seed_recall=recall,
            estimated_requests=estimated,
        )

    async def approve(
        self,
        actor: ActorContext,
        *,
        scope_version_id: UUID,
        gold_seed_hits: int | None = None,
    ) -> ScopeVersion:
        """Freeze and approve a scope version after its gate passes."""
        self._context.require_installation(InstallationCapability.CURATE_CORPUS)
        actor.require(Capability.MANAGE_SCOPE)
        report = await self.evaluate_gate(
            actor, scope_version_id=scope_version_id, gold_seed_hits=gold_seed_hits
        )
        if not report.passed:
            raise ValidationFailure(
                "scope gate failed", context={"blockers": list(report.blockers)}
            )
        context = self._context
        async with context.uow_factory() as uow:
            scope = await uow.configs.get_scope_version(actor.workspace_id, scope_version_id)
            if scope is None:
                raise NotFound(f"scope version {scope_version_id} not found")
            if scope.approval_state is ApprovalState.APPROVED:
                return scope
            if scope.approval_state is ApprovalState.RETIRED:
                raise StateTransitionError("a retired scope version cannot be approved")
            approved = scope.model_copy(
                update={
                    "approval_state": ApprovalState.APPROVED,
                    "reviewed_by": actor.user_id,
                    "frozen_at": context.now(),
                }
            )
            await uow.configs.add_scope_version(approved)
            event = context.audit.build(
                workspace_id=actor.workspace_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                action=AuditAction.SCOPE_APPROVED,
                subject_type="scope_version",
                subject_id=approved.id,
                previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                reason_code="gate_passed",
                metadata={
                    "checksum": approved.config_checksum,
                    "connectors": list(report.compiled_connectors),
                    "gold_seed_recall": report.gold_seed_recall,
                },
            )
            await uow.audit.append(event)
            await uow.commit()
        return approved
