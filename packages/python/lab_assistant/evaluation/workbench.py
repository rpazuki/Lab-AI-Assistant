"""The evaluation workbench: cancel, compare, gate releases, review by hand.

``service.py`` registers benchmarks and queues runs; this module is what people do
with the runs afterwards.

* **Cancel** stops a queued or running run. Its task is cancelled so no worker
  picks it up, and a worker already measuring it finds the run cancelled when it
  finishes and leaves it so.
* **Compare** puts two runs of the same benchmark side by side: every metric with
  its difference, every gate, and the cases whose measurements changed. Runs of
  different benchmarks are refused, because their cases are different questions.
* **Release gates** name the benchmark versions a release must pass before it is
  published. The check itself is ``application/release_gates.py``, beside
  publishing.
* **Human review** samples cases of a generation run and assigns them to
  reviewers, who grade each answer on the predecessor's form: five 1-5 scores,
  issue flags, confidence, a comment, a suggested answer. The grades are kept
  beside the automatic metrics and summarised per run; they never overwrite a
  measured number.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.release_gates import evaluation_gate_status
from lab_assistant.contracts.errors import (
    NotFound,
    PolicyDenied,
    StateTransitionError,
    ValidationFailure,
)
from lab_assistant.contracts.events import ActorType, AuditAction
from lab_assistant.domain.installation import InstallationCapability
from lab_assistant.domain.workspace import Capability

__all__ = [
    "REVIEW_FLAGS",
    "REVIEW_SCORES",
    "EvaluationWorkbench",
    "review_json",
]

#: The predecessor's review form: each a 1-5 score.
REVIEW_SCORES: tuple[str, ...] = (
    "correctness",
    "completeness",
    "citation_support",
    "grounding",
    "usefulness",
)
#: Issues a reviewer can flag on an answer.
REVIEW_FLAGS: tuple[str, ...] = (
    "hallucination",
    "citation_issue",
    "corpus_gap",
    "retrieval_issue",
    "generation_issue",
)

#: Cases one assignment request may sample, so a click cannot hand out thousands.
MAX_SAMPLE = 200

_CANCELLABLE = frozenset({"queued", "running"})
#: A run with no result to rely on. A succeeded run is evidence, even one that
#: failed its gates: it is what a release's gate check and a comparison read.
_DELETABLE = frozenset({"failed", "cancelled"})


def _sample_order(run_id: UUID, case_id: str) -> str:
    """A stable pseudo-random order: the same run always samples the same cases."""
    return hashlib.sha256(f"{run_id}:{case_id}".encode()).hexdigest()


def review_json(row: dict[str, Any], case: dict[str, Any] | None = None) -> dict[str, Any]:
    def iso(value: Any) -> str | None:
        return value.isoformat() if hasattr(value, "isoformat") else value

    body: dict[str, Any] = {
        "id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "case_id": row["case_id"],
        "assigned_to": str(row["assigned_to"]),
        "assigned_by": str(row["assigned_by"]) if row.get("assigned_by") else None,
        "status": row.get("status", "assigned"),
        "scores": row.get("scores") or {},
        "flags": list(row.get("flags") or []),
        "reviewer_confidence": row.get("reviewer_confidence"),
        "comment": row.get("comment"),
        "suggested_answer": row.get("suggested_answer"),
        "created_at": iso(row.get("created_at")),
        "submitted_at": iso(row.get("submitted_at")),
    }
    if case is not None:
        body["case"] = case
    return body


class EvaluationWorkbench:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    def _require(self, actor: ActorContext) -> None:
        self._context.require_installation(InstallationCapability.PUBLISH_RELEASES)
        actor.require(Capability.RUN_EVALUATION)

    async def _run(self, uow: Any, actor: ActorContext, run_id: UUID) -> dict[str, Any]:
        run = await uow.evaluations.get_run(actor.workspace_id, run_id)
        if run is None:
            raise NotFound(f"evaluation run {run_id} not found")
        return run

    async def _audit(
        self,
        uow: Any,
        actor: ActorContext,
        *,
        action: AuditAction,
        subject_type: str,
        subject_id: UUID | None,
        metadata: dict[str, Any] | None = None,
        reason_code: str | None = None,
    ) -> None:
        await uow.audit.append(
            self._context.audit.build(
                workspace_id=actor.workspace_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                previous_hash=await uow.audit.latest_hash(actor.workspace_id),
                reason_code=reason_code,
                metadata=metadata or {},
            )
        )

    # ── cancel ──────────────────────────────────────────────────────────────
    async def cancel_run(self, actor: ActorContext, *, run_id: UUID) -> dict[str, Any]:
        self._require(actor)
        context = self._context
        async with context.uow_factory() as uow:
            run = await self._run(uow, actor, run_id)
            status = run.get("status", "succeeded")
            if status not in _CANCELLABLE:
                raise StateTransitionError(
                    f"a {status} run cannot be cancelled", context={"status": status}
                )
            if run.get("task_id"):
                task = await uow.tasks.get(actor.workspace_id, UUID(str(run["task_id"])))
                if task is not None and not task.state.is_terminal:
                    task.cancel(now=context.now())
                    await uow.tasks.update(task)
            await uow.evaluations.update_run(
                {
                    "id": run_id,
                    "workspace_id": actor.workspace_id,
                    "status": "cancelled",
                    "completed_at": context.now(),
                }
            )
            await self._audit(
                uow,
                actor,
                action=AuditAction.EVALUATION_CANCELLED,
                subject_type="evaluation_run",
                subject_id=run_id,
                metadata={"previous_status": status, "benchmark": run.get("benchmark")},
            )
            await uow.commit()
            return await self._run(uow, actor, run_id)

    # ── delete ──────────────────────────────────────────────────────────────
    async def delete_run(self, actor: ActorContext, *, run_id: UUID) -> None:
        """Delete a failed or cancelled run that no release and no reviewer used."""
        self._require(actor)
        context = self._context
        async with context.uow_factory() as uow:
            run = await self._run(uow, actor, run_id)
            status = run.get("status", "succeeded")
            if status not in _DELETABLE:
                raise StateTransitionError(
                    f"a {status} run cannot be deleted; only a failed or cancelled one can",
                    context={"status": status},
                )
            if await uow.evaluations.list_reviews(actor.workspace_id, run_id=run_id):
                raise StateTransitionError("answers from this run were assigned for review")
            releases = await uow.releases.list_all_releases(actor.workspace_id, limit=100_000)
            if any(str(release.evaluation_run_id) == str(run_id) for release in releases):
                raise StateTransitionError("a release was approved on this run")
            task_ids: list[UUID] = []
            if run.get("task_id"):
                task = await uow.tasks.get(actor.workspace_id, UUID(str(run["task_id"])))
                if task is not None:
                    if not task.state.is_settled:
                        raise StateTransitionError("this run's task has not finished yet")
                    task_ids.append(task.id)
            await uow.tasks.delete_tasks(actor.workspace_id, task_ids)
            await uow.evaluations.delete_run(actor.workspace_id, run_id)
            await self._audit(
                uow,
                actor,
                action=AuditAction.EVALUATION_RUN_DELETED,
                subject_type="evaluation_run",
                subject_id=run_id,
                reason_code="run_deleted",
                metadata={"status": status, "benchmark": run.get("benchmark")},
            )
            await uow.commit()

    async def delete_benchmark(
        self, actor: ActorContext, *, benchmark_id: str, version: str
    ) -> None:
        """Delete a benchmark version no run measured against and no gate requires.

        Versions are immutable so that a run's checksum always names what it was
        measured against; with no run, there is nothing for the version to explain.
        """
        self._require(actor)
        context = self._context
        key = f"{benchmark_id}@{version}"
        async with context.uow_factory() as uow:
            benchmark = await uow.evaluations.get_benchmark(
                actor.workspace_id, benchmark_id, version
            )
            if benchmark is None:
                raise NotFound(f"benchmark {key} not found")
            runs = await uow.evaluations.list_runs(
                actor.workspace_id, benchmark_id=benchmark_id, limit=100_000
            )
            if any(run.get("benchmark") == key for run in runs):
                raise StateTransitionError(
                    "evaluation runs measured against this version; it cannot be deleted"
                )
            if any(
                gate["benchmark_id"] == benchmark_id and gate["version"] == version
                for gate in await uow.evaluations.list_release_gates(actor.workspace_id)
            ):
                raise StateTransitionError(
                    "publishing requires this version; remove it from the release gates first"
                )
            await uow.evaluations.delete_benchmark(actor.workspace_id, benchmark_id, version)
            await self._audit(
                uow,
                actor,
                action=AuditAction.BENCHMARK_DELETED,
                subject_type="benchmark",
                subject_id=UUID(str(benchmark["id"])) if benchmark.get("id") else None,
                reason_code="benchmark_deleted",
                metadata={"benchmark": key, "checksum": benchmark.get("checksum")},
            )
            await uow.commit()

    # ── compare ─────────────────────────────────────────────────────────────
    async def compare_runs(
        self, actor: ActorContext, *, base_id: UUID, candidate_id: UUID
    ) -> dict[str, Any]:
        actor.require(Capability.RUN_EVALUATION)
        async with self._context.uow_factory() as uow:
            base = await self._run(uow, actor, base_id)
            candidate = await self._run(uow, actor, candidate_id)
        base_id_part = str(base["benchmark"]).rpartition("@")[0]
        candidate_id_part = str(candidate["benchmark"]).rpartition("@")[0]
        if base_id_part != candidate_id_part:
            raise ValidationFailure(
                "only runs of the same benchmark can be compared: their cases are the same "
                "questions",
                context={"base": base["benchmark"], "candidate": candidate["benchmark"]},
            )
        for run in (base, candidate):
            if run.get("status", "succeeded") != "succeeded":
                raise ValidationFailure(
                    f"run {run['id']} is {run.get('status')}; only finished runs compare"
                )

        def numbers(values: dict[str, Any]) -> dict[str, float]:
            return {
                key: float(value)
                for key, value in values.items()
                if isinstance(value, int | float) and not isinstance(value, bool)
            }

        base_metrics, candidate_metrics = (
            numbers(base["metrics"] or {}),
            numbers(candidate["metrics"] or {}),
        )
        metrics = [
            {
                "name": name,
                "base": base_metrics.get(name),
                "candidate": candidate_metrics.get(name),
                "delta": (
                    round(candidate_metrics[name] - base_metrics[name], 6)
                    if name in base_metrics and name in candidate_metrics
                    else None
                ),
            }
            for name in sorted(set(base_metrics) | set(candidate_metrics))
        ]
        base_gates, candidate_gates = base.get("gates") or {}, candidate.get("gates") or {}
        gates = [
            {"name": name, "base": base_gates.get(name), "candidate": candidate_gates.get(name)}
            for name in sorted(set(base_gates) | set(candidate_gates))
        ]

        def by_case(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
            return {str(case.get("case_id")): case for case in run.get("per_case") or []}

        base_cases, candidate_cases = by_case(base), by_case(candidate)
        cases: list[dict[str, Any]] = []
        for case_id in sorted(set(base_cases) | set(candidate_cases)):
            left, right = base_cases.get(case_id), candidate_cases.get(case_id)
            if left is None or right is None:
                cases.append(
                    {
                        "case_id": case_id,
                        "only_in": "candidate" if left is None else "base",
                        "changes": {},
                    }
                )
                continue
            changes = {
                field: {"base": left.get(field), "candidate": right.get(field)}
                for field in sorted(set(left) | set(right))
                if field not in ("case_id", "question", "answer")
                and isinstance(left.get(field, right.get(field)), int | float | bool | list)
                and left.get(field) != right.get(field)
            }
            if changes:
                cases.append({"case_id": case_id, "only_in": None, "changes": changes})
        return {
            "base": {
                "id": str(base["id"]),
                "benchmark": base["benchmark"],
                "release_id": str(base.get("release_id")),
            },
            "candidate": {
                "id": str(candidate["id"]),
                "benchmark": candidate["benchmark"],
                "release_id": str(candidate.get("release_id")),
            },
            "same_version": base["benchmark"] == candidate["benchmark"],
            "metrics": metrics,
            "gates": gates,
            "changed_cases": cases,
            "unchanged_cases": len(set(base_cases) & set(candidate_cases))
            - sum(1 for case in cases if case["only_in"] is None),
        }

    # ── release gates ───────────────────────────────────────────────────────
    async def list_release_gates(self, actor: ActorContext) -> list[dict[str, Any]]:
        actor.require(Capability.RUN_EVALUATION)
        async with self._context.uow_factory() as uow:
            return await uow.evaluations.list_release_gates(actor.workspace_id)

    async def set_release_gate(
        self, actor: ActorContext, *, benchmark_id: str, version: str | None
    ) -> list[dict[str, Any]]:
        """Require a benchmark version for publishing, or stop requiring it (``None``)."""
        self._require(actor)
        actor.require(Capability.APPROVE_RELEASE)
        context = self._context
        async with context.uow_factory() as uow:
            if version is None:
                await uow.evaluations.clear_release_gate(actor.workspace_id, benchmark_id)
            else:
                if (
                    await uow.evaluations.get_benchmark(actor.workspace_id, benchmark_id, version)
                    is None
                ):
                    raise NotFound(f"benchmark {benchmark_id}@{version} is not registered")
                await uow.evaluations.set_release_gate(
                    {
                        "workspace_id": actor.workspace_id,
                        "benchmark_id": benchmark_id,
                        "version": version,
                        "set_by": actor.user_id,
                        "set_at": context.now(),
                    }
                )
            await self._audit(
                uow,
                actor,
                action=AuditAction.RELEASE_GATE_CHANGED,
                subject_type="benchmark",
                subject_id=None,
                reason_code="required" if version else "no_longer_required",
                metadata={"benchmark_id": benchmark_id, "version": version},
            )
            await uow.commit()
            return await uow.evaluations.list_release_gates(actor.workspace_id)

    async def gate_status(self, actor: ActorContext, *, release_id: UUID) -> list[dict[str, Any]]:
        actor.require(Capability.RUN_EVALUATION)
        async with self._context.uow_factory() as uow:
            statuses = await evaluation_gate_status(uow, actor.workspace_id, release_id)
        return [status.as_dict() for status in statuses]

    # ── human review ────────────────────────────────────────────────────────
    async def assign_reviews(
        self,
        actor: ActorContext,
        *,
        run_id: UUID,
        reviewer_ids: list[UUID],
        sample_size: int,
    ) -> list[dict[str, Any]]:
        """Sample cases of a finished generation run and share them among reviewers.

        The sample is a stable function of the run, so asking again adds the next
        cases rather than reshuffling, and a case already assigned to a reviewer is
        not assigned to them twice.
        """
        self._require(actor)
        if not reviewer_ids:
            raise ValidationFailure("choose at least one reviewer")
        if not 1 <= sample_size <= MAX_SAMPLE:
            raise ValidationFailure(f"sample between 1 and {MAX_SAMPLE} cases")
        context = self._context
        async with context.uow_factory() as uow:
            run = await self._run(uow, actor, run_id)
            if run.get("kind") != "generation" or run.get("status") != "succeeded":
                raise ValidationFailure("only a finished generation run has answers to review")
            for reviewer in reviewer_ids:
                membership = await uow.workspaces.get_membership(actor.workspace_id, reviewer)
                if membership is None or not membership.has(Capability.REVIEW_CLAIMS):
                    raise ValidationFailure(
                        f"member {reviewer} cannot review: they need the reviewer role or above"
                    )
            existing = {
                (row["case_id"], str(row["assigned_to"]))
                for row in await uow.evaluations.list_reviews(actor.workspace_id, run_id=run_id)
            }
            assigned_cases = {case_id for case_id, _ in existing}
            cases = sorted(
                (str(case["case_id"]) for case in run.get("per_case") or []),
                key=lambda case_id: _sample_order(run_id, case_id),
            )
            fresh = [case_id for case_id in cases if case_id not in assigned_cases][:sample_size]
            created: list[dict[str, Any]] = []
            for index, case_id in enumerate(fresh):
                reviewer = reviewer_ids[index % len(reviewer_ids)]
                if (case_id, str(reviewer)) in existing:
                    continue
                row = {
                    "id": context.new_id(),
                    "workspace_id": actor.workspace_id,
                    "run_id": run_id,
                    "case_id": case_id,
                    "assigned_to": reviewer,
                    "assigned_by": actor.user_id,
                    "status": "assigned",
                    "scores": {},
                    "flags": [],
                    "created_at": context.now(),
                }
                await uow.evaluations.add_review(row)
                created.append(row)
            await self._audit(
                uow,
                actor,
                action=AuditAction.EVALUATION_REVIEW_ASSIGNED,
                subject_type="evaluation_run",
                subject_id=run_id,
                metadata={"cases": len(created), "reviewers": len(reviewer_ids)},
            )
            await uow.commit()
        return created

    async def my_reviews(self, actor: ActorContext) -> list[dict[str, Any]]:
        """The caller's assignments, each with the question, answer and metrics."""
        actor.require(Capability.REVIEW_CLAIMS)
        async with self._context.uow_factory() as uow:
            rows = await uow.evaluations.list_reviews(actor.workspace_id, assigned_to=actor.user_id)
            runs: dict[str, dict[str, Any]] = {}
            items = []
            for row in rows:
                key = str(row["run_id"])
                if key not in runs:
                    runs[key] = await self._run(uow, actor, UUID(key))
                items.append(review_json(row, _case_of(runs[key], row["case_id"])))
        # Unfinished first; within each, in the order they were assigned.
        return sorted(items, key=lambda item: item["status"] == "submitted")

    async def submit_review(
        self,
        actor: ActorContext,
        *,
        review_id: UUID,
        scores: dict[str, int],
        flags: list[str],
        reviewer_confidence: int | None,
        comment: str | None,
        suggested_answer: str | None,
    ) -> dict[str, Any]:
        actor.require(Capability.REVIEW_CLAIMS)
        unknown = sorted(set(scores) - set(REVIEW_SCORES))
        if unknown:
            raise ValidationFailure(f"unknown scores: {', '.join(unknown)}")
        if any(not 1 <= int(value) <= 5 for value in scores.values()):
            raise ValidationFailure("every score is between 1 and 5")
        if "correctness" not in scores:
            raise ValidationFailure("correctness is the one score every review needs")
        bad_flags = sorted(set(flags) - set(REVIEW_FLAGS))
        if bad_flags:
            raise ValidationFailure(f"unknown flags: {', '.join(bad_flags)}")
        if reviewer_confidence is not None and not 1 <= reviewer_confidence <= 5:
            raise ValidationFailure("confidence is between 1 and 5")
        context = self._context
        async with context.uow_factory() as uow:
            row = await uow.evaluations.get_review(actor.workspace_id, review_id)
            if row is None:
                raise NotFound(f"review {review_id} not found")
            if str(row["assigned_to"]) != str(actor.user_id):
                raise PolicyDenied("this review is assigned to someone else")
            update = {
                "id": review_id,
                "workspace_id": actor.workspace_id,
                "status": "submitted",
                "scores": {key: int(value) for key, value in scores.items()},
                "flags": sorted(set(flags)),
                "reviewer_confidence": reviewer_confidence,
                "comment": (comment or "").strip() or None,
                "suggested_answer": (suggested_answer or "").strip() or None,
                "submitted_at": context.now(),
            }
            await uow.evaluations.update_review(update)
            await self._audit(
                uow,
                actor,
                action=AuditAction.EVALUATION_REVIEW_SUBMITTED,
                subject_type="evaluation_review",
                subject_id=review_id,
                metadata={"run_id": str(row["run_id"]), "case_id": row["case_id"]},
            )
            await uow.commit()
            stored = await uow.evaluations.get_review(actor.workspace_id, review_id)
            run = await self._run(uow, actor, UUID(str(row["run_id"])))
        assert stored is not None
        return review_json(stored, _case_of(run, stored["case_id"]))

    async def run_reviews(self, actor: ActorContext, *, run_id: UUID) -> dict[str, Any]:
        """Every review of a run, and what the submitted ones say together."""
        actor.require(Capability.RUN_EVALUATION)
        async with self._context.uow_factory() as uow:
            run = await self._run(uow, actor, run_id)
            rows = await uow.evaluations.list_reviews(actor.workspace_id, run_id=run_id)
        submitted = [row for row in rows if row.get("status") == "submitted"]
        means: dict[str, float | None] = {}
        for score in REVIEW_SCORES:
            values = [row["scores"][score] for row in submitted if score in (row["scores"] or {})]
            means[score] = round(sum(values) / len(values), 3) if values else None
        flag_counts = {
            flag: sum(1 for row in submitted if flag in (row.get("flags") or []))
            for flag in REVIEW_FLAGS
        }
        return {
            "run_id": str(run_id),
            "assigned": len(rows),
            "submitted": len(submitted),
            "mean_scores": means,
            "flag_counts": flag_counts,
            "items": [review_json(row, _case_of(run, row["case_id"])) for row in rows],
        }


def _case_of(run: dict[str, Any], case_id: str) -> dict[str, Any] | None:
    return next(
        (case for case in run.get("per_case") or [] if str(case.get("case_id")) == case_id), None
    )
