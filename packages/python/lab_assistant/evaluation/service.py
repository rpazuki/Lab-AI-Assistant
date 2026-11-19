"""Evaluation against a real release: registered benchmarks, queued runs.

The runner in ``runner.py`` measures; this module decides what may be measured and
records the result. Three things it adds over the synthetic tool in
``tools/cli/run_benchmarks.py``:

* **Benchmarks are registered, not shipped.** A gold set built from licensed
  article text must not live in version control, so an operator registers it with
  this installation. A registered version is immutable: the same id and version
  with different content is refused, which is how "a changed gold label is a new
  version" is enforced rather than hoped for.
* **Relevant works are named the way people know them.** A case may list
  ``doi:10.1234/x``, ``pmid:123``, ``pmcid:PMC1`` or a work id. Each is resolved to
  the work this installation holds; one that resolves to nothing stays in the case
  as a miss and is reported, because a gold paper the corpus lacks is a recall
  failure, not a case to skip.
* **Runs are queued.** A generation run calls the model once per case, which does
  not fit in a web request. The run is recorded when it is requested and the
  evaluation worker completes it, so a queued or failed run is visible.

Runs execute as the member who requested them: retrieval honours their grants, so
two members can measure the same release differently, and the run records who.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import ValidationError

from lab_assistant.application.chat import ChatService
from lab_assistant.application.context import ActorContext, ServiceContext, resolve_actor
from lab_assistant.application.tasks import TaskOrchestrator
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import ConcurrencyConflict, NotFound, ValidationFailure
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.contracts.stage import StageCommand, StageName, StageResult, StageStatus
from lab_assistant.domain.identity import normalise_identifier
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.states import ReleaseState
from lab_assistant.domain.workspace import Capability, normalise_email
from lab_assistant.evaluation.benchmarks import Benchmark, PolicyProbe
from lab_assistant.evaluation.runner import EvaluationRun, EvaluationRunner
from lab_assistant.provenance.fingerprints import task_fingerprint

__all__ = [
    "EVALUABLE_STATES",
    "RUNNABLE_KINDS",
    "EvaluationHandler",
    "EvaluationService",
    "benchmark_json",
    "run_json",
]

IMPLEMENTATION_VERSION = "evaluation-1"

#: Kinds a registered benchmark may have: the ones the runner can execute against
#: a release. Discovery, identity, parsing and extraction sets measure stages that
#: are not a release, and are refused rather than accepted and never run.
RUNNABLE_KINDS: tuple[str, ...] = ("retrieval", "generation", "policy")

#: Releases with a searchable projection. A draft or validating release has none:
#: indexing runs after approval, so evaluation sits between approval and publish.
EVALUABLE_STATES: frozenset[ReleaseState] = frozenset(
    {
        ReleaseState.APPROVED,
        ReleaseState.PUBLISHING,
        ReleaseState.PUBLISHED,
        ReleaseState.SUPERSEDED,
    }
)

#: An upper bound on one benchmark, so a pasted file cannot queue an afternoon of
#: model calls by accident. A larger set is several versions or several ids.
MAX_CASES = 500

_SCHEMES = {scheme.value: scheme for scheme in IdentifierScheme}


class EvaluationService:
    def __init__(
        self, context: ServiceContext, orchestrator: TaskOrchestrator | None = None
    ) -> None:
        self._context = context
        self._orchestrator = orchestrator

    # ── benchmarks ──────────────────────────────────────────────────────────
    async def register_benchmark(
        self, actor: ActorContext, definition: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Register one benchmark version. Returns the row and whether it was new.

        Registering identical content again is a no-op, so an upload can be retried.
        """
        self._require(actor)
        benchmark = _parse_benchmark(definition)
        context = self._context
        async with context.uow_factory() as uow:
            existing = await uow.evaluations.get_benchmark(
                actor.workspace_id, benchmark.benchmark_id, benchmark.version
            )
            if existing is not None:
                if existing["checksum"] == benchmark.checksum:
                    return existing, False
                raise ConcurrencyConflict(
                    f"{benchmark.logical_key} is already registered with different content; "
                    "register the change as a new version",
                    context={"benchmark": benchmark.logical_key},
                )
            row_id = context.new_id()
            row = {
                "id": row_id,
                "workspace_id": actor.workspace_id,
                "benchmark_id": benchmark.benchmark_id,
                "version": benchmark.version,
                "kind": benchmark.kind,
                "checksum": benchmark.checksum,
                "sensitivity": benchmark.sensitivity,
                "definition": benchmark.model_dump(mode="json"),
                "registered_by": actor.user_id,
                "created_at": context.now(),
            }
            await uow.evaluations.add_benchmark(row)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.BENCHMARK_REGISTERED,
                    subject_type="benchmark",
                    subject_id=row_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    metadata={
                        "benchmark": benchmark.logical_key,
                        "checksum": benchmark.checksum,
                        "kind": benchmark.kind,
                        "sensitivity": benchmark.sensitivity,
                    },
                )
            )
            await uow.commit()
        return row, True

    async def list_benchmarks(self, actor: ActorContext) -> list[dict[str, Any]]:
        actor.require(Capability.RUN_EVALUATION)
        async with self._context.uow_factory() as uow:
            return await uow.evaluations.list_benchmarks(actor.workspace_id)

    async def get_benchmark(
        self, actor: ActorContext, *, benchmark_id: str, version: str
    ) -> dict[str, Any]:
        actor.require(Capability.RUN_EVALUATION)
        async with self._context.uow_factory() as uow:
            row = await uow.evaluations.get_benchmark(actor.workspace_id, benchmark_id, version)
        if row is None:
            raise NotFound(f"benchmark {benchmark_id}@{version} is not registered")
        return row

    # ── runs ────────────────────────────────────────────────────────────────
    async def request_run(
        self,
        actor: ActorContext,
        *,
        benchmark_id: str,
        version: str,
        release_id: UUID,
    ) -> dict[str, Any]:
        """Record a queued run and hand it to the evaluation worker."""
        self._require(actor)
        if self._orchestrator is None:
            raise RuntimeError("EvaluationService needs a task orchestrator to queue runs")
        benchmark_row = await self.get_benchmark(actor, benchmark_id=benchmark_id, version=version)
        benchmark = Benchmark.model_validate(benchmark_row["definition"])
        if benchmark.kind == "generation":
            actor.require(Capability.ASK)
            if benchmark.sensitivity == "restricted":
                # The questions and the retrieved evidence go to the model provider.
                # Restricted benchmark content never reaches an unapproved provider,
                # and this installation has no notion of an approved one for it.
                raise ValidationFailure(
                    "a restricted benchmark cannot run a generation evaluation: its "
                    "questions would be sent to the model provider",
                    context={"benchmark": benchmark.logical_key},
                )

        context = self._context
        async with context.uow_factory() as uow:
            release = await uow.releases.get(actor.workspace_id, release_id)
            if release is None:
                raise NotFound(f"release {release_id} not found")
            blocker = _release_blocker(release)
            if blocker:
                raise ValidationFailure(
                    f"release {release.name} {release.semantic_version} cannot be "
                    f"evaluated: {blocker}",
                    context={"release_id": str(release_id), "status": release.status.value},
                )
            run_id = context.new_id()
            run = {
                "id": run_id,
                "workspace_id": actor.workspace_id,
                "benchmark": benchmark.logical_key,
                "benchmark_checksum": benchmark.checksum,
                "release_id": release_id,
                "kind": benchmark.kind,
                "status": "queued",
                "requested_by": actor.user_id,
                "metrics": {},
                "gates": {},
                "failures": [],
                "per_case": [],
                "created_at": context.now(),
            }
            await uow.evaluations.add_run(run)
            await uow.audit.append(
                context.audit.build(
                    workspace_id=actor.workspace_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    action=AuditAction.EVALUATION_REQUESTED,
                    subject_type="evaluation_run",
                    subject_id=run_id,
                    previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                    metadata={
                        "benchmark": benchmark.logical_key,
                        "checksum": benchmark.checksum,
                        "release_id": str(release_id),
                    },
                )
            )
            await uow.commit()

        task = await self._orchestrator.submit(
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            stage=StageName.EVALUATION,
            inputs={"run_id": str(run["id"])},
            idempotency_key=f"evaluation:{run['id']}",
        )
        run["task_id"] = task.id
        async with context.uow_factory() as uow:
            await uow.evaluations.update_run(
                {"id": run["id"], "workspace_id": actor.workspace_id, "task_id": task.id}
            )
            await uow.commit()
        return run

    async def list_runs(
        self, actor: ActorContext, *, release_id: UUID | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        actor.require(Capability.RUN_EVALUATION)
        async with self._context.uow_factory() as uow:
            return await uow.evaluations.list_runs(
                actor.workspace_id, release_id=release_id, limit=limit
            )

    async def get_run(self, actor: ActorContext, *, run_id: UUID) -> dict[str, Any]:
        actor.require(Capability.RUN_EVALUATION)
        async with self._context.uow_factory() as uow:
            run = await uow.evaluations.get_run(actor.workspace_id, run_id)
        if run is None:
            raise NotFound(f"evaluation run {run_id} not found")
        return run

    def _require(self, actor: ActorContext) -> None:
        # Evaluation gates releases, so it belongs where releases are made. A reader
        # also runs no workers, and a run it queued would wait forever.
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.RUN_EVALUATION)


class EvaluationHandler:
    """The ``evaluation`` stage: complete one queued run."""

    stage_name = StageName.EVALUATION
    implementation_version = IMPLEMENTATION_VERSION

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._runner = EvaluationRunner(context)

    def fingerprint(self, command: StageCommand) -> str:
        return task_fingerprint(command, implementation_version=self.implementation_version)

    async def execute(self, command: StageCommand) -> StageResult:
        context = self._context
        workspace_id = command.workspace_id
        run_id = UUID(str(command.inputs["run_id"]))
        async with context.uow_factory() as uow:
            run = await uow.evaluations.get_run(workspace_id, run_id)
            if run is None:
                raise NotFound(f"evaluation run {run_id} not found")
            if run.get("status") == "succeeded":
                return StageResult(stage=StageName.EVALUATION, status=StageStatus.SUCCEEDED_NOOP)
            benchmark_id, _, version = str(run["benchmark"]).rpartition("@")
            row = await uow.evaluations.get_benchmark(workspace_id, benchmark_id, version)
            if row is None:
                raise NotFound(f"benchmark {run['benchmark']} is not registered")
            await uow.evaluations.update_run(
                {"id": run_id, "workspace_id": workspace_id, "status": "running"}
            )
            await uow.commit()

        benchmark = Benchmark.model_validate(row["definition"])
        release_id = UUID(str(run["release_id"]))
        try:
            actor = await resolve_actor(
                context, user_id=command.actor_id, workspace_id=workspace_id
            )
            result = await self._measure(actor, benchmark, release_id)
        except Exception as exc:
            # Recorded on the run before the task engine decides whether to retry, so
            # the page shows why; a retry that succeeds overwrites it.
            async with context.uow_factory() as uow:
                await uow.evaluations.update_run(
                    {
                        "id": run_id,
                        "workspace_id": workspace_id,
                        "status": "failed",
                        "error_detail": f"{type(exc).__name__}: {exc}"[:2000],
                        "completed_at": context.now(),
                    }
                )
                await uow.commit()
            raise

        async with context.uow_factory() as uow:
            await uow.evaluations.update_run(
                {
                    "id": run_id,
                    "workspace_id": workspace_id,
                    "status": "succeeded",
                    "metrics": result.metrics,
                    "gates": result.gates,
                    "failures": list(result.failures),
                    "per_case": result.per_case,
                    "error_detail": None,
                    "completed_at": context.now(),
                }
            )
            await uow.audit.append(
                context.audit.build(
                    workspace_id=workspace_id,
                    actor_type=ActorType.WORKER,
                    actor_id=None,
                    action=AuditAction.EVALUATION_COMPLETED,
                    subject_type="evaluation_run",
                    subject_id=run_id,
                    previous_hash=await uow.audit.latest_hash(workspace_id),
                    reason_code="passed" if result.passed else "failed_gates",
                    metadata={
                        "benchmark": benchmark.logical_key,
                        "release_id": str(release_id),
                        "gates": result.gates,
                    },
                )
            )
            await uow.commit()
        return StageResult(
            stage=StageName.EVALUATION,
            status=StageStatus.SUCCEEDED,
            metrics=result.metrics,
            quality_gates=result.gates,
        )

    async def _measure(
        self, actor: ActorContext, benchmark: Benchmark, release_id: UUID
    ) -> EvaluationRun:
        if benchmark.kind == "policy":
            return await self._runner.run_policy_probes(
                benchmark=benchmark,
                release_id=release_id,
                actor_resolver=lambda probe: self._probe_actor(actor.workspace_id, probe),
            )
        resolved, unresolved = await self._resolve_relevant(actor.workspace_id, benchmark)
        if benchmark.kind == "generation":
            # A session of its own, pinned to the release under test, so the answers
            # are grounded in that release and not in whatever the alias serves.
            session = await ChatService(self._context).create_session(
                actor,
                title=f"Evaluation: {benchmark.logical_key}",
                pinned_release_id=release_id,
            )
            run = await self._runner.run_generation(
                actor, benchmark=resolved, release_id=release_id, session_id=session.id
            )
        else:
            run = await self._runner.run_retrieval(actor, benchmark=resolved, release_id=release_id)
        for case in run.per_case:
            missing = unresolved.get(str(case.get("case_id")))
            if missing:
                case["unresolved_relevant"] = missing
        return run

    async def _resolve_relevant(
        self, workspace_id: UUID, benchmark: Benchmark
    ) -> tuple[Benchmark, dict[str, list[str]]]:
        """Rewrite each relevant key to a work id this installation holds.

        A key that resolves to nothing is kept as written: it can never be
        retrieved, so it counts as a miss, and it is reported by case.
        """
        cases = []
        unresolved: dict[str, list[str]] = {}
        async with self._context.uow_factory() as uow:
            for case in benchmark.cases:
                relevant: list[str] = []
                for key in case.relevant:
                    work_id = await _resolve_key(uow, workspace_id, key)
                    if work_id is None:
                        unresolved.setdefault(case.case_id, []).append(key)
                        relevant.append(key)
                    else:
                        relevant.append(work_id)
                cases.append(case.model_copy(update={"relevant": tuple(relevant)}))
        return benchmark.model_copy(update={"cases": tuple(cases)}), unresolved

    async def _probe_actor(self, workspace_id: UUID, probe: PolicyProbe) -> ActorContext | None:
        """The probe's subject as this installation knows them, or ``None``.

        An unresolvable subject fails the probe in the runner: a probe that cannot
        name who it tests proves nothing.
        """
        async with self._context.uow_factory() as uow:
            user = await uow.users.get_by_email(normalise_email(probe.user_email))
            workspace = await uow.workspaces.get_by_slug(probe.workspace_slug)
        if user is None or workspace is None:
            return None
        return await resolve_actor(self._context, user_id=user.id, workspace_id=workspace.id)


async def _resolve_key(uow: Any, workspace_id: UUID, key: str) -> str | None:
    try:
        return str(UUID(key))
    except ValueError:
        pass
    scheme_name, separator, value = key.partition(":")
    scheme = _SCHEMES.get(scheme_name.strip().lower())
    if not separator or scheme is None or not value.strip():
        return None
    work = await uow.works.find_by_identifier(
        workspace_id, scheme=scheme.value, value_normalized=normalise_identifier(scheme, value)
    )
    return str(work.id) if work is not None else None


def _parse_benchmark(definition: dict[str, Any]) -> Benchmark:
    try:
        benchmark = Benchmark.model_validate(definition)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or 'benchmark'}: {error['msg']}"
            for error in exc.errors()[:20]
        ]
        raise ValidationFailure(
            "the benchmark file is not valid", context={"problems": problems}
        ) from exc
    if benchmark.kind not in RUNNABLE_KINDS:
        raise ValidationFailure(
            f"kind {benchmark.kind!r} cannot be run against a release; "
            f"use one of {', '.join(RUNNABLE_KINDS)}",
            context={"kind": benchmark.kind},
        )
    if benchmark.kind == "policy":
        if not benchmark.probes:
            raise ValidationFailure("a policy benchmark needs at least one probe")
    elif not any(case.question for case in benchmark.cases):
        raise ValidationFailure(f"a {benchmark.kind} benchmark needs at least one question")
    if len(benchmark.cases) + len(benchmark.probes) > MAX_CASES:
        raise ValidationFailure(
            f"a benchmark may hold at most {MAX_CASES} cases and probes; split it",
            context={"cases": len(benchmark.cases), "probes": len(benchmark.probes)},
        )
    return benchmark


def _release_blocker(release: Any) -> str | None:
    if release.status not in EVALUABLE_STATES:
        return f"it is {release.status.value}; evaluate after approval, once indexing has finished"
    missing = [
        profile
        for profile in release.embedding_profile_ids
        if profile not in release.projection_ready_profiles
    ]
    if missing:
        return f"indexing has not finished ({', '.join(sorted(missing))})"
    return None


# ── JSON shapes, shared by the API ──────────────────────────────────────────
def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def benchmark_json(row: dict[str, Any], *, include_definition: bool = False) -> dict[str, Any]:
    definition = row["definition"]
    body: dict[str, Any] = {
        "benchmark_id": row["benchmark_id"],
        "version": row["version"],
        "key": f"{row['benchmark_id']}@{row['version']}",
        "kind": row["kind"],
        "description": definition.get("description", ""),
        "sensitivity": row["sensitivity"],
        "blinded": bool(definition.get("blinded", False)),
        "checksum": row["checksum"],
        "cases": len(definition.get("cases", [])),
        "probes": len(definition.get("probes", [])),
        "thresholds": definition.get("thresholds", {}),
        "registered_by": _str_or_none(row.get("registered_by")),
        "created_at": _iso(row.get("created_at")),
    }
    if include_definition:
        body["definition"] = definition
    return body


def run_json(row: dict[str, Any], *, include_cases: bool = False) -> dict[str, Any]:
    gates = row.get("gates") or {}
    status = row.get("status", "succeeded")
    body: dict[str, Any] = {
        "id": str(row["id"]),
        "benchmark": row["benchmark"],
        "benchmark_checksum": row["benchmark_checksum"],
        "release_id": _str_or_none(row.get("release_id")),
        "kind": row.get("kind"),
        "status": status,
        # Passing needs gates and all of them true: a run that measured nothing has
        # not passed, whatever its absent gates would say.
        "passed": status == "succeeded" and bool(gates) and all(gates.values()),
        "metrics": row.get("metrics") or {},
        "gates": gates,
        "failures": list(row.get("failures") or []),
        "task_id": _str_or_none(row.get("task_id")),
        "requested_by": _str_or_none(row.get("requested_by")),
        "error_detail": row.get("error_detail"),
        "created_at": _iso(row.get("created_at")),
        "completed_at": _iso(row.get("completed_at")),
    }
    if include_cases:
        body["per_case"] = row.get("per_case") or []
    return body
