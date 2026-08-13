"""Stage 1: discovery.

Order of operations per page, and the order matters:

1. take a distributed request lease (shared, not per-process);
2. build and fingerprint the canonical request;
3. store request metadata **before** transmission;
4. execute with connector-specific pagination and cursor rules;
5. store the response envelope **before** parsing it;
6. verify the checksum and register the immutable object;
7. parse observations with a versioned parser;
8. emit field-level assertions and advance the cursor;
9. persist page failures rather than logging and continuing.

A run cannot freeze as complete while a required connector is degraded, and a
partial run can be inspected but never becomes a release input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.rate_limit import RateLimiter, budget_key, host_of
from lab_assistant.contracts.connector import (
    BodyKind,
    CompiledQuery,
    Cursor,
    DiscoveryRequest,
    RawPage,
)
from lab_assistant.contracts.errors import (
    ErrorClass,
    LabAssistantError,
    NotFound,
    SourceOutcome,
    ValidationFailure,
)
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import (
    StageCommand,
    StageName,
    StageResult,
    StageStatus,
    TaskRequest,
)
from lab_assistant.domain.observation import (
    ConnectorRunState,
    DiscoveryRun,
    DiscoveryRunStatus,
    Predicate,
    SourceAssertion,
    SourceObservation,
    SourceRequest,
    run_fingerprint,
)
from lab_assistant.domain.scope import ScopeVersion
from lab_assistant.domain.workspace import Capability
from lab_assistant.provenance.fingerprints import source_request_fingerprint

__all__ = ["DiscoveryHandler", "DiscoveryService", "cursor_lineage_key"]

IMPLEMENTATION_VERSION = "discovery/1"


def cursor_lineage_key(
    *,
    workspace_id: UUID,
    scope_version_id: UUID,
    connector_id: str,
    connector_version: str,
    query_fingerprint: str,
    cursor_semantics_version: str,
) -> str:
    """Checkpoints are keyed by everything that changes their meaning.

    The predecessor keyed PubMed checkpoints by year alone, so a changed query
    resumed someone else's cursor. Changing any component here starts a new
    lineage instead.
    """
    return "|".join(
        [
            str(workspace_id),
            str(scope_version_id),
            f"{connector_id}@{connector_version}",
            query_fingerprint,
            f"semantics={cursor_semantics_version}",
        ]
    )


@dataclass(frozen=True, slots=True)
class _ParsedRecord:
    locator: str
    canonical: dict[str, Any]
    assertions: tuple[tuple[Predicate, Any, str], ...]


class DiscoveryService:
    """Creates and freezes discovery runs."""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def start_run(
        self, actor: ActorContext, *, scope_version_id: UUID
    ) -> tuple[DiscoveryRun, tuple[TaskRequest, ...]]:
        actor.require(Capability.RUN_DISCOVERY)
        context = self._context
        registry = context.require_connectors()
        async with context.uow_factory() as uow:
            scope = await uow.configs.get_scope_version(actor.workspace_id, scope_version_id)
            if scope is None:
                raise NotFound(f"scope version {scope_version_id} not found")
            if not scope.is_approved:
                raise ValidationFailure(
                    "scope version is not approved; production discovery is gated on approval"
                )

            connector_versions: dict[str, str] = {}
            starting_cursors: dict[str, str | None] = {}
            states: list[ConnectorRunState] = []
            tasks: list[TaskRequest] = []

            for entry in scope.connector_plan:
                connector = registry.get(entry.connector_id)
                compiled = await connector.compile_query(
                    {
                        "query": entry.query,
                        "parameters": entry.parameters,
                        "scope": scope.model_dump(mode="json"),
                    }
                )
                connector_versions[entry.connector_id] = connector.capabilities.connector_version
                lineage = cursor_lineage_key(
                    workspace_id=actor.workspace_id,
                    scope_version_id=scope_version_id,
                    connector_id=entry.connector_id,
                    connector_version=connector.capabilities.connector_version,
                    query_fingerprint=compiled.query_fingerprint,
                    cursor_semantics_version="1",
                )
                cursor_position = await uow.observations.get_cursor(
                    actor.workspace_id, lineage_key=lineage
                )
                starting_cursors[entry.connector_id] = cursor_position
                states.append(
                    ConnectorRunState(
                        connector_id=entry.connector_id,
                        connector_version=connector.capabilities.connector_version,
                        required=entry.required,
                        query_fingerprint=compiled.query_fingerprint,
                        cursor_after=cursor_position,
                    )
                )
                tasks.append(
                    TaskRequest(
                        stage=StageName.DISCOVERY,
                        inputs={
                            "scope_version_id": str(scope_version_id),
                            "connector_id": entry.connector_id,
                        },
                    )
                )

            run = DiscoveryRun(
                id=context.new_id(),
                workspace_id=actor.workspace_id,
                scope_version_id=scope_version_id,
                status=DiscoveryRunStatus.RUNNING,
                run_fingerprint=run_fingerprint(
                    scope_checksum=scope.config_checksum,
                    connector_versions=connector_versions,
                    starting_cursors=starting_cursors,
                    orchestration_version=IMPLEMENTATION_VERSION,
                ),
                requested_by=actor.user_id,
                started_at=context.now(),
                connector_states=tuple(states),
            )
            await uow.observations.add_run(run)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.DISCOVERY_RUN_STARTED,
                    subject_type="discovery_run",
                    subject_id=run.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    run_id=run.id,
                    metadata={
                        "scope_version_id": str(scope_version_id),
                        "run_fingerprint": run.run_fingerprint,
                        "connectors": sorted(connector_versions),
                    },
                )
            )
            await uow.commit()

        tasks = [
            TaskRequest(
                stage=task.stage,
                inputs={**task.inputs, "discovery_run_id": str(run.id)},
            )
            for task in tasks
        ]
        return run, tuple(tasks)

    async def freeze_run(
        self, actor: ActorContext, *, run_id: UUID, force_accept_degraded: bool = False
    ) -> tuple[DiscoveryRun, tuple[TaskRequest, ...]]:
        """Freeze a run, or refuse with the list of blockers.

        Returns the frozen run and the reconciliation task it authorises. A run
        that cannot freeze authorises nothing.
        """
        actor.require(Capability.RUN_DISCOVERY)
        context = self._context
        async with context.uow_factory() as uow:
            run = await uow.observations.get_run(actor.workspace_id, run_id)
            if run is None:
                raise NotFound(f"discovery run {run_id} not found")
            if force_accept_degraded:
                run.connector_states = tuple(
                    state
                    if state.is_acceptable
                    else state.model_copy(update={"accepted_degraded_by": actor.user_id})
                    for state in run.connector_states
                )
            blockers = run.freeze_blockers()
            if blockers:
                run.status = DiscoveryRunStatus.DEGRADED
                run.degraded_reason = "; ".join(blockers)
                await uow.observations.update_run(run)
                await uow.commit()
                raise ValidationFailure(
                    "discovery run cannot freeze as complete",
                    context={"blockers": list(blockers)},
                )
            run.status = DiscoveryRunStatus.FROZEN
            run.frozen_at = context.now()
            run.completed_at = run.completed_at or run.frozen_at
            run.degraded_reason = None
            run.metrics = {
                **run.metrics,
                "records_seen": sum(s.records_seen for s in run.connector_states),
                "pages_fetched": sum(s.pages_fetched for s in run.connector_states),
                "connectors": len(run.connector_states),
                "degraded_accepted": sum(
                    1 for s in run.connector_states if s.accepted_degraded_by is not None
                ),
            }
            await uow.observations.update_run(run)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.DISCOVERY_RUN_FROZEN,
                    subject_type="discovery_run",
                    subject_id=run.id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    run_id=run.id,
                    metadata=dict(run.metrics),
                )
            )
            if force_accept_degraded:
                await uow.audit.append(
                    context.audit.build(
                        workspace_id=actor.workspace_id,
                        actor_type=ActorType.USER,
                        actor_id=actor.user_id,
                        action=AuditAction.DEGRADED_SOURCE_ACCEPTED,
                        subject_type="discovery_run",
                        subject_id=run.id,
                        previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                        run_id=run.id,
                        reason_code="steward_accepted_degraded",
                    )
                )
            await uow.commit()
        return run, (
            TaskRequest(
                stage=StageName.RECONCILIATION, inputs={"discovery_run_id": str(run.id)}
            ),
        )


class DiscoveryHandler:
    """Stage handler: one connector, one run, page by page."""

    stage_name = StageName.DISCOVERY
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def fingerprint(self, command: StageCommand) -> str:
        from lab_assistant.provenance.fingerprints import task_fingerprint

        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        context = self._context
        workspace_id = command.workspace_id
        connector_id = str(command.inputs["connector_id"])
        run_id = UUID(str(command.inputs["discovery_run_id"]))
        registry = context.require_connectors()

        async with context.uow_factory() as uow:
            run = await uow.observations.get_run(workspace_id, run_id)
            if run is None:
                raise NotFound(f"discovery run {run_id} not found")
            scope = await uow.configs.get_scope_version(workspace_id, run.scope_version_id)
            if scope is None:
                raise NotFound(f"scope version {run.scope_version_id} not found")

        entry = scope.connector_entry(connector_id)
        if entry is None:
            raise ValidationFailure(f"connector {connector_id} is not in this scope's plan")

        connector = registry.get(connector_id)
        compiled = await connector.compile_query(
            {
                "query": entry.query,
                "parameters": entry.parameters,
                "scope": scope.model_dump(mode="json"),
            }
        )
        lineage = cursor_lineage_key(
            workspace_id=workspace_id,
            scope_version_id=scope.id,
            connector_id=connector_id,
            connector_version=connector.capabilities.connector_version,
            query_fingerprint=compiled.query_fingerprint,
            cursor_semantics_version="1",
        )

        async with context.uow_factory() as uow:
            position = await uow.observations.get_cursor(workspace_id, lineage_key=lineage)

        cursor = Cursor(
            connector_id=connector_id,
            query_fingerprint=compiled.query_fingerprint,
            position=position,
        )
        request = DiscoveryRequest(
            compiled_query=compiled,
            cursor=cursor,
            page_size=entry.page_size,
            max_pages=entry.max_pages,
            request_identity=context.settings.connector_request_identity,
        )

        pages_fetched = 0
        records_seen = 0
        declared_total: int | None = None
        outcome = SourceOutcome.SUCCEEDED
        error_class: ErrorClass | None = None
        degraded_reason: str | None = None
        retry_after: int | None = None
        parse_failures = 0
        last_cursor = cursor

        limiter = RateLimiter(context)
        rate_key = budget_key(
            connector_id=connector_id,
            host=host_of(getattr(connector, "base_url", None)),
        )
        try:
            # One budgeted request per page, shared across every worker process.
            # Acquiring once per run would not be a rate limit; a run is many
            # requests, and requests are what the source counts.
            async for page in limiter.throttle(
                connector.discover(request),
                key=rate_key,
                policy=connector.capabilities.rate_policy,
            ):
                pages_fetched += 1
                declared_total = page.declared_total or declared_total
                records, failures = await self._store_page(
                    command=command,
                    run=run,
                    scope=scope,
                    connector_id=connector_id,
                    connector_version=connector.capabilities.connector_version,
                    compiled=compiled,
                    page=page,
                )
                records_seen += records
                parse_failures += failures
                last_cursor = page.cursor_after
                async with context.uow_factory() as uow:
                    await uow.observations.set_cursor(
                        workspace_id,
                        lineage_key=lineage,
                        position=page.cursor_after.position,
                        exhausted=page.cursor_after.exhausted,
                    )
                    await uow.commit()
                if entry.max_pages is not None and pages_fetched >= entry.max_pages:
                    break
            if records_seen == 0:
                outcome = SourceOutcome.SUCCEEDED_EMPTY
        except LabAssistantError as exc:
            error_class = exc.error_class
            degraded_reason = exc.detail
            outcome = _outcome_for_error(exc.error_class)
            retry_after = exc.context.get("retry_after_seconds") if exc.context else None
        except Exception as exc:  # unexpected: record it, never silently continue
            error_class = ErrorClass.INTERNAL
            degraded_reason = f"{type(exc).__name__}: {exc}"
            outcome = SourceOutcome.DEGRADED_PARTIAL

        if parse_failures and outcome is SourceOutcome.SUCCEEDED:
            failure_rate = parse_failures / max(1, records_seen + parse_failures)
            if failure_rate > 0.05:
                outcome = SourceOutcome.DEGRADED_PARTIAL
                degraded_reason = f"parser failure rate {failure_rate:.3f}"

        async with context.uow_factory() as uow:
            run = await uow.observations.get_run(workspace_id, run_id)
            assert run is not None
            updated_states = []
            for state in run.connector_states:
                if state.connector_id != connector_id:
                    updated_states.append(state)
                    continue
                updated_states.append(
                    state.model_copy(
                        update={
                            "pages_fetched": state.pages_fetched + pages_fetched,
                            "records_seen": state.records_seen + records_seen,
                            "declared_total": declared_total,
                            "outcome": outcome,
                            "error_class": error_class,
                            "degraded_reason": degraded_reason,
                            "cursor_after": last_cursor.position,
                        }
                    )
                )
            run.connector_states = tuple(updated_states)
            required_unacceptable = any(
                state.required and not state.is_acceptable for state in run.connector_states
            )
            source_failed = outcome is not SourceOutcome.SUCCEEDED and outcome.is_terminal
            if source_failed and required_unacceptable:
                run.status = DiscoveryRunStatus.DEGRADED
                run.degraded_reason = degraded_reason
            await uow.observations.update_run(run)
            await uow.commit()

        status = (
            StageStatus.SUCCEEDED
            if outcome.is_acceptable_terminal
            else (StageStatus.DEGRADED if outcome.is_terminal else StageStatus.BLOCKED)
        )
        return StageResult(
            stage=StageName.DISCOVERY,
            status=status,
            metrics={
                "connector_id": connector_id,
                "pages_fetched": pages_fetched,
                "records_seen": records_seen,
                "declared_total": declared_total,
                "parse_failures": parse_failures,
                "outcome": outcome.value,
            },
            quality_gates={
                "pagination_reconciled": declared_total is None or records_seen >= declared_total,
                "source_terminal_acceptable": outcome.is_acceptable_terminal,
            },
            error_class=error_class,
            error_detail=degraded_reason,
            retry_after_seconds=retry_after,
            # Reconciliation is deliberately *not* scheduled here. One connector
            # finishing says nothing about the others, and reconciling early would
            # resolve identity against a partial observation set — then be skipped
            # as a duplicate task when the remaining sources arrived.
            # DiscoveryService.freeze_run schedules it once the run is complete.
        )

    async def _store_page(
        self,
        *,
        command: StageCommand,
        run: DiscoveryRun,
        scope: ScopeVersion,
        connector_id: str,
        connector_version: str,
        compiled: CompiledQuery,
        page: RawPage,
    ) -> tuple[int, int]:
        """Persist one page: request row, raw object, observations, assertions."""
        context = self._context
        workspace_id = command.workspace_id
        response = page.response

        # 1. Store bytes before parsing, so a parser fix never re-queries a source.
        #    When the connector produced an envelope (two upstream calls behind one
        #    page, say), the source's own bytes are stored as their own object: replay
        #    determinism and provenance are different requirements and both are met.
        stored = await context.object_store.put(
            response.body,
            workspace_id=workspace_id,
            object_class="source_response",
            media_type=response.headers_allowed.get("content-type", "application/json"),
        )
        upstream_stored = None
        if response.body_kind is BodyKind.REPLAY_ENVELOPE and response.upstream_body:
            upstream_stored = await context.object_store.put(
                response.upstream_body,
                workspace_id=workspace_id,
                object_class="source_response_upstream",
                media_type=response.headers_allowed.get("content-type", "application/json"),
            )

        request_fp = source_request_fingerprint(
            connector_id=connector_id,
            connector_version=connector_version,
            method=response.method,
            canonical_url=response.url_redacted,
            parameters=compiled.parameters,
            cursor_position=page.cursor_before.position,
            cursor_semantics_version=page.cursor_before.cursor_semantics_version,
        )

        parsed = _parse_records(page, connector_id=connector_id)
        failures = sum(1 for record in parsed if record.canonical.get("_parse_error"))

        async with context.uow_factory() as uow:
            existing = await uow.observations.find_request_by_fingerprint(workspace_id, request_fp)
            if existing is not None and existing.response_sha256 == stored.sha256:
                # At-least-once delivery replayed this page; one logical result.
                return existing.record_count, 0

            source_request = SourceRequest(
                id=context.new_id(),
                workspace_id=workspace_id,
                discovery_run_id=run.id,
                connector_id=connector_id,
                connector_version=connector_version,
                request_fingerprint=request_fp,
                method=response.method,
                canonical_url_redacted=response.url_redacted,
                requested_at=response.requested_at,
                completed_at=response.completed_at,
                response_status=response.status,
                response_headers_allowed=response.headers_allowed,
                response_body_object_id=stored.id,
                response_sha256=stored.sha256,
                response_byte_length=stored.byte_length,
                response_body_kind=response.body_kind.value,
                upstream_body_object_id=upstream_stored.id if upstream_stored else None,
                upstream_sha256=upstream_stored.sha256 if upstream_stored else None,
                source_cursor_before=page.cursor_before.position,
                source_cursor_after=page.cursor_after.position,
                declared_total=page.declared_total,
                record_count=len(parsed),
                outcome=SourceOutcome.SUCCEEDED,
            )
            await uow.observations.add_request(source_request)

            assertions: list[SourceAssertion] = []
            for record in parsed:
                canonical_bytes = SourceObservation.record_digest(record.canonical)
                canonical_object = await context.object_store.put(
                    _canonical_bytes(record.canonical),
                    workspace_id=workspace_id,
                    object_class="canonical_record",
                    media_type="application/json",
                )
                observation = SourceObservation(
                    id=context.new_id(),
                    workspace_id=workspace_id,
                    source_request_id=source_request.id,
                    source_record_locator=record.locator,
                    parser_id=f"{connector_id}-record-parser",
                    parser_version="1",
                    schema_version="1.0",
                    canonical_record_object_id=canonical_object.id,
                    canonical_record_sha256=canonical_bytes,
                    observed_at=response.completed_at,
                    parse_status="failed" if record.canonical.get("_parse_error") else "parsed",
                )
                await uow.observations.add_observation(observation)
                for predicate, value, authority in record.assertions:
                    assertions.append(
                        SourceAssertion(
                            id=context.new_id(),
                            workspace_id=workspace_id,
                            source_observation_id=observation.id,
                            subject_hint=record.locator,
                            predicate=predicate,
                            value_json=value,
                            normalised_value_json=_normalise_assertion(predicate, value),
                            source_priority_class=authority,
                            asserted_at_source=response.completed_at,
                        )
                    )
            if assertions:
                await uow.observations.add_assertions(assertions)
            await uow.commit()

        return len(parsed), failures


def _canonical_bytes(record: dict[str, Any]) -> bytes:
    from lab_assistant.contracts.canonical import canonical_json

    return canonical_json(record)


def _outcome_for_error(error_class: ErrorClass) -> SourceOutcome:
    mapping = {
        ErrorClass.RATE_LIMITED: SourceOutcome.RATE_LIMITED_RETRYABLE,
        ErrorClass.SOURCE_UNAVAILABLE: SourceOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
        ErrorClass.TIMEOUT: SourceOutcome.SOURCE_UNAVAILABLE_RETRYABLE,
        ErrorClass.AUTHENTICATION_REQUIRED: SourceOutcome.AUTHENTICATION_REQUIRED,
        ErrorClass.POLICY_BLOCKED: SourceOutcome.POLICY_BLOCKED,
        ErrorClass.ROBOTS_BLOCKED: SourceOutcome.POLICY_BLOCKED,
        ErrorClass.INVALID_QUERY: SourceOutcome.INVALID_QUERY,
        ErrorClass.CANCELLED: SourceOutcome.CANCELLED,
    }
    return mapping.get(error_class, SourceOutcome.FAILED_PERMANENT)


def _normalise_assertion(predicate: Predicate, value: Any) -> Any:
    """Normalise an assertion value without discarding the original."""
    if predicate is Predicate.IDENTIFIER and isinstance(value, dict):
        from lab_assistant.contracts.connector import IdentifierScheme
        from lab_assistant.domain.identity import normalise_identifier

        try:
            scheme = IdentifierScheme(str(value.get("scheme")))
        except ValueError:
            return None
        try:
            return {
                "scheme": scheme.value,
                "value": normalise_identifier(scheme, str(value.get("value", ""))),
            }
        except ValueError:
            return None
    if predicate is Predicate.TITLE and isinstance(value, str):
        return " ".join(value.split())
    if predicate in (Predicate.YEAR, Predicate.CITED_BY_COUNT) and isinstance(value, str):
        return int(value) if value.isdigit() else None
    return None


def _parse_records(page: RawPage, *, connector_id: str) -> list[_ParsedRecord]:
    """Turn a connector's canonical page payload into records and assertions.

    Connectors return already-shaped canonical records in ``page.response.body``
    as JSON: ``{"records": [{"locator": ..., "assertions": [...]}, ...]}``. The
    shaping lives in the connector because query languages and field semantics
    are source-specific; the *storage* of raw bytes happened before this point,
    so a bug here is replayable.
    """
    import json

    try:
        payload = json.loads(page.response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [
            _ParsedRecord(
                locator=f"{connector_id}:unparseable",
                canonical={"_parse_error": str(exc)},
                assertions=(),
            )
        ]

    records: list[_ParsedRecord] = []
    for raw in payload.get("records", []):
        locator = str(raw.get("locator") or raw.get("id") or "")
        assertions: list[tuple[Predicate, Any, str]] = []
        for item in raw.get("assertions", []):
            try:
                predicate = Predicate(str(item["predicate"]))
            except (KeyError, ValueError):
                continue
            assertions.append(
                (
                    predicate,
                    item.get("value"),
                    str(item.get("authority", "aggregator")),
                )
            )
        records.append(_ParsedRecord(locator=locator, canonical=raw, assertions=tuple(assertions)))
    return records
