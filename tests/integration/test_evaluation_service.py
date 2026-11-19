"""Evaluation against a real release: registration, queued runs, the worker.

Seeds the same two-workspace deployment the synthetic benchmark tool uses, so the
policy probes name real accounts, and drives runs through the task queue the way
the evaluation worker does. Loading the tool by path keeps `lab_assistant.testing`
out of the evaluation layer, as ``test_benchmark_runner`` explains.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest

from lab_assistant.adapters.models.fake import FakeLLMProvider
from lab_assistant.application.chat import ChatService
from lab_assistant.application.releases import ReleaseService
from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import (
    ConcurrencyConflict,
    InstallationDenied,
    PolicyDenied,
    StateTransitionError,
    ValidationFailure,
)
from lab_assistant.domain.identity import WorkIdentifier
from lab_assistant.domain.installation import InstallationRole
from lab_assistant.domain.states import ReleaseState
from lab_assistant.domain.workspace import Role
from lab_assistant.evaluation.benchmarks import Benchmark
from lab_assistant.evaluation.runner import EvaluationRunner
from lab_assistant.evaluation.service import EvaluationService, run_json
from lab_assistant.evaluation.workbench import EvaluationWorkbench
from lab_assistant.testing import build_harness, make_actor

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "benchmarks" / "synthetic"


def _load_tool() -> ModuleType:
    path = ROOT / "tools" / "cli" / "run_benchmarks.py"
    spec = importlib.util.spec_from_file_location("lab_benchmark_tool_for_service", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _synthetic(name: str) -> dict[str, Any]:
    return json.loads((SYNTHETIC / name).read_text(encoding="utf-8"))


def _retrieval_benchmark(relevant: list[str], *, version: str = "1.0.0") -> dict[str, Any]:
    return {
        "benchmark_id": "lab-retrieval",
        "version": version,
        "kind": "retrieval",
        "description": "Real-corpus retrieval set, named by identifiers.",
        "origin": "written for this test",
        "licence": "internal",
        "permitted_use": "testing",
        "sensitivity": "internal",
        "cases": [
            {
                "case_id": "lipid-yield",
                "question": "What lipid yield was reported for the engineered strain?",
                "relevant": relevant,
            }
        ],
        "thresholds": {"recall@5": 0.5},
    }


@pytest.fixture
async def stack() -> dict[str, Any]:
    tool = _load_tool()
    deployment = tool.Deployment()
    await deployment.seed()
    harness = deployment.harness
    service = EvaluationService(harness.context, harness.orchestrator)
    return {
        "deployment": deployment,
        "harness": harness,
        "service": service,
        "steward": deployment.steward_actor,
        "release_id": deployment.release_id,
        "yeast_id": deployment.corpus.work_ids[tool.PUBLIC_TITLE],
    }


async def _run(stack: dict[str, Any], definition: dict[str, Any]) -> dict[str, Any]:
    service, steward = stack["service"], stack["steward"]
    row, _ = await service.register_benchmark(steward, definition)
    queued = await service.request_run(
        steward,
        benchmark_id=row["benchmark_id"],
        version=row["version"],
        release_id=stack["release_id"],
    )
    assert queued["status"] == "queued"
    assert queued["task_id"] is not None
    await stack["harness"].run_all_tasks()
    return run_json(await service.get_run(steward, run_id=queued["id"]), include_cases=True)


class TestRegistration:
    async def test_a_version_registers_once_and_identical_content_is_a_no_op(self, stack) -> None:
        service, steward = stack["service"], stack["steward"]
        definition = _retrieval_benchmark([str(stack["yeast_id"])])
        _, created = await service.register_benchmark(steward, definition)
        _, again = await service.register_benchmark(steward, definition)
        assert (created, again) == (True, False)
        assert len(await service.list_benchmarks(steward)) == 1

    async def test_a_changed_gold_label_must_be_a_new_version(self, stack) -> None:
        service, steward = stack["service"], stack["steward"]
        await service.register_benchmark(steward, _retrieval_benchmark([str(stack["yeast_id"])]))
        with pytest.raises(ConcurrencyConflict, match="new version"):
            await service.register_benchmark(steward, _retrieval_benchmark(["doi:10.1/other"]))
        _, created = await service.register_benchmark(
            steward, _retrieval_benchmark(["doi:10.1/other"], version="1.1.0")
        )
        assert created

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"kind": "discovery"}, "cannot be run against a release"),
            ({"cases": [{"case_id": "empty"}]}, "at least one question"),
            ({"version": None}, "not valid"),
        ],
    )
    async def test_a_benchmark_that_cannot_run_is_refused(self, stack, change, message) -> None:
        definition = {**_retrieval_benchmark([]), **change}
        with pytest.raises(ValidationFailure, match=message):
            await stack["service"].register_benchmark(stack["steward"], definition)

    async def test_a_researcher_may_not_register(self, stack) -> None:
        with pytest.raises(PolicyDenied):
            await stack["service"].register_benchmark(
                stack["deployment"].outsider_actor, _retrieval_benchmark([])
            )

    async def test_a_reader_installation_runs_no_evaluation(self) -> None:
        reader = build_harness(installation_role=InstallationRole.READER)
        actor = make_actor(
            user_id=reader.context.new_id(), workspace_id=reader.context.new_id(), role=Role.ADMIN
        )
        with pytest.raises(InstallationDenied):
            await EvaluationService(reader.context).register_benchmark(
                actor, _retrieval_benchmark([])
            )


class TestRuns:
    async def test_relevant_works_resolve_by_identifier_and_misses_are_reported(
        self, stack
    ) -> None:
        harness, steward = stack["harness"], stack["steward"]
        async with harness.context.uow_factory() as uow:
            await uow.works.add_identifier(
                WorkIdentifier(
                    id=harness.context.new_id(),
                    workspace_id=steward.workspace_id,
                    work_id=stack["yeast_id"],
                    scheme=IdentifierScheme.DOI,
                    value_normalized="10.1234/yeast",
                    value_display="10.1234/Yeast",
                )
            )
            await uow.commit()

        run = await _run(stack, _retrieval_benchmark(["DOI:10.1234/Yeast", "pmid:999999"]))

        assert run["status"] == "succeeded", run["error_detail"]
        assert run["metrics"]["cases"] == 1
        # The DOI found the work it names; the PMID names nothing this corpus holds
        # and stays in the case as a miss, reported rather than dropped.
        assert run["metrics"]["recall@5"] == 0.5
        assert run["per_case"][0]["unresolved_relevant"] == ["pmid:999999"]
        assert run["gates"] == {"retrieval:recall@5": True, "retrieval:cases_executed": True}
        assert run["passed"] is True
        assert run["completed_at"] is not None

    async def test_policy_probes_resolve_real_accounts(self, stack) -> None:
        run = await _run(stack, _synthetic("policy-probes.json"))
        assert run["status"] == "succeeded", run["error_detail"]
        assert run["metrics"] == {"probes": 2, "leaks": 0}
        assert run["passed"] is True

    async def test_an_unknown_probe_subject_fails_the_run(self, stack) -> None:
        definition = _synthetic("policy-probes.json")
        definition["probes"][0]["user_email"] = "nobody@lab.invalid"
        run = await _run(stack, definition)
        assert run["status"] == "succeeded"
        assert run["passed"] is False
        assert any("could not be resolved" in failure for failure in run["failures"])

    async def test_a_generation_run_answers_in_a_session_pinned_to_the_release(self, stack) -> None:
        run = await _run(stack, _synthetic("generation-citations.json"))
        assert run["status"] == "succeeded", run["error_detail"]
        assert run["metrics"]["cases"] == 2
        async with stack["harness"].context.uow_factory() as uow:
            sessions = await uow.chat.list_sessions(
                stack["steward"].workspace_id, stack["steward"].user_id
            )
        evaluation = [s for s in sessions if (s.title or "").startswith("Evaluation:")]
        assert [s.pinned_release_id for s in evaluation] == [stack["release_id"]]

    async def test_restricted_questions_never_go_to_the_model(self, stack) -> None:
        service, steward = stack["service"], stack["steward"]
        definition = {**_synthetic("generation-citations.json"), "sensitivity": "restricted"}
        row, _ = await service.register_benchmark(steward, definition)
        with pytest.raises(ValidationFailure, match="model provider"):
            await service.request_run(
                steward,
                benchmark_id=row["benchmark_id"],
                version=row["version"],
                release_id=stack["release_id"],
            )

    async def test_a_release_without_a_projection_cannot_be_evaluated(self, stack) -> None:
        harness, service, steward = stack["harness"], stack["service"], stack["steward"]
        async with harness.context.uow_factory() as uow:
            release = await uow.releases.get(steward.workspace_id, stack["release_id"])
            release.status = ReleaseState.DRAFT
            await uow.releases.update(release)
            await uow.commit()
        row, _ = await service.register_benchmark(
            steward, _retrieval_benchmark([str(stack["yeast_id"])])
        )
        with pytest.raises(ValidationFailure, match="after approval"):
            await service.request_run(
                steward,
                benchmark_id=row["benchmark_id"],
                version=row["version"],
                release_id=stack["release_id"],
            )
        assert await service.list_runs(steward) == []

    async def test_the_run_executes_as_the_member_who_asked(self, stack) -> None:
        """Retrieval honours the requester's grants, so the run records who."""
        steward = stack["steward"]
        run = await _run(stack, _retrieval_benchmark([str(stack["yeast_id"])]))
        assert run["requested_by"] == str(steward.user_id)


class _AddsAFigure(FakeLLMProvider):
    """Answers from the evidence, then states a titre no source reports."""

    def __init__(self, *, cite: bool) -> None:
        super().__init__()
        self.cite = cite

    def _compose(self, request):  # type: ignore[no-untyped-def]
        answer = super()._compose(request)
        handle = f" [{request.evidence[0].handle}]" if self.cite and request.evidence else ""
        return f"{answer} The titre reached 9.75 g/L{handle}."


class TestGenerationMetrics:
    """The scores measure the answer against what the model was given.

    They used to compare the answer with itself, so they read 1.0 whatever the
    model said. Each test here is a way an answer can be wrong that the old
    scores could not see.
    """

    QUESTION: ClassVar[dict[str, Any]] = {
        "benchmark_id": "gen",
        "version": "1.0.0",
        "kind": "generation",
        "description": "one question",
        "origin": "test",
        "licence": "internal",
        "permitted_use": "testing",
        "cases": [{"case_id": "yield", "question": "What lipid yield was reported?"}],
    }

    async def _run_with(self, stack, provider) -> dict[str, Any]:
        context = replace(stack["harness"].context, llm_provider=provider)
        runner = EvaluationRunner(context)
        steward = stack["steward"]
        session = await ChatService(context).create_session(
            steward, pinned_release_id=stack["release_id"]
        )
        run = await runner.run_generation(
            steward,
            benchmark=Benchmark.model_validate(self.QUESTION),
            release_id=stack["release_id"],
            session_id=session.id,
        )
        return {"metrics": run.metrics, "case": run.per_case[0], "gates": run.gates}

    async def test_a_grounded_answer_scores_full_marks(self, stack) -> None:
        result = await self._run_with(stack, FakeLLMProvider())
        # The answer repeats figures such as 0.21 and 30 from its evidence, and its
        # handles hold digits too: none of those may count against it.
        assert result["metrics"]["faithfulness_proxy"] == 1.0
        assert result["metrics"]["citation_validity"] == 1.0
        assert result["case"]["unsupported_numbers"] == []

    async def test_an_invented_citation_lowers_validity(self, stack) -> None:
        result = await self._run_with(stack, FakeLLMProvider(invent_handle="Edeadbeef00"))
        case = result["case"]
        assert case["unsupported"] == ["Edeadbeef00"]
        # Two real citations and one invented: two thirds valid, not zero, not one.
        assert case["citation_validity"] == pytest.approx(2 / 3, abs=1e-3)
        assert result["gates"]["generation:no_unsupported_citations"] is False

    async def test_a_figure_the_cited_evidence_lacks_is_unfaithful(self, stack) -> None:
        result = await self._run_with(stack, _AddsAFigure(cite=True))
        case = result["case"]
        assert case["unsupported_numbers"] == ["9.75"]
        assert 0 < case["faithfulness_proxy"] < 1
        # The citation itself is real: this answer is unfaithful, not mis-cited.
        assert case["citation_validity"] == 1.0

    async def test_an_uncited_figure_is_still_checked(self, stack) -> None:
        cited = await self._run_with(stack, _AddsAFigure(cite=True))
        uncited = await self._run_with(stack, _AddsAFigure(cite=False))
        assert uncited["case"]["unsupported_numbers"] == ["9.75"]
        assert uncited["case"]["faithfulness_proxy"] == cited["case"]["faithfulness_proxy"]


# ── the workbench: cancel, compare, release gates, human review ────────────
async def _queue(stack: dict[str, Any], definition: dict[str, Any]) -> dict[str, Any]:
    service, steward = stack["service"], stack["steward"]
    row, _ = await service.register_benchmark(steward, definition)
    return await service.request_run(
        steward,
        benchmark_id=row["benchmark_id"],
        version=row["version"],
        release_id=stack["release_id"],
    )


class TestCancel:
    async def test_a_queued_run_is_cancelled_and_the_worker_leaves_it(self, stack) -> None:
        workbench = EvaluationWorkbench(stack["harness"].context)
        queued = await _queue(stack, _retrieval_benchmark([str(stack["yeast_id"])]))
        cancelled = await workbench.cancel_run(stack["steward"], run_id=queued["id"])
        assert cancelled["status"] == "cancelled"

        await stack["harness"].run_all_tasks()
        run = await stack["service"].get_run(stack["steward"], run_id=queued["id"])
        assert run["status"] == "cancelled"
        assert run["metrics"] == {}

    async def test_a_finished_run_cannot_be_cancelled(self, stack) -> None:
        run = await _run(stack, _retrieval_benchmark([str(stack["yeast_id"])]))
        with pytest.raises(StateTransitionError, match="succeeded run"):
            await EvaluationWorkbench(stack["harness"].context).cancel_run(
                stack["steward"], run_id=run["id"]
            )


class TestCompare:
    async def test_two_runs_of_one_benchmark_compare_case_by_case(self, stack) -> None:
        found = await _run(stack, _retrieval_benchmark([str(stack["yeast_id"])]))
        missed = await _run(
            stack, _retrieval_benchmark(["doi:10.1/not-in-the-corpus"], version="1.0.1")
        )
        report = await EvaluationWorkbench(stack["harness"].context).compare_runs(
            stack["steward"], base_id=found["id"], candidate_id=missed["id"]
        )
        assert report["same_version"] is False
        recall = next(item for item in report["metrics"] if item["name"] == "recall@5")
        assert recall["base"] == 1.0 and recall["candidate"] == 0.0 and recall["delta"] == -1.0
        [case] = report["changed_cases"]
        assert case["case_id"] == "lipid-yield"
        assert case["changes"]

    async def test_different_benchmarks_are_not_compared(self, stack) -> None:
        retrieval = await _run(stack, _retrieval_benchmark([str(stack["yeast_id"])]))
        generation = await _run(stack, _synthetic("generation-citations.json"))
        with pytest.raises(ValidationFailure, match="same benchmark"):
            await EvaluationWorkbench(stack["harness"].context).compare_runs(
                stack["steward"], base_id=retrieval["id"], candidate_id=generation["id"]
            )


class TestReleaseGate:
    async def test_publishing_needs_a_passing_run_of_a_required_benchmark(self, stack) -> None:
        steward, release_id = stack["steward"], stack["release_id"]
        workbench = EvaluationWorkbench(stack["harness"].context)
        definition = _retrieval_benchmark([str(stack["yeast_id"])])
        row, _ = await stack["service"].register_benchmark(steward, definition)
        await workbench.set_release_gate(
            steward, benchmark_id=row["benchmark_id"], version=row["version"]
        )
        releases = ReleaseService(stack["harness"].context)
        with pytest.raises(ValidationFailure, match="required benchmark") as refused:
            await releases.publish(steward, release_id=release_id)
        assert refused.value.context["blockers"] == [
            "evaluation:lab-retrieval@1.0.0 has not passed"
        ]
        [status] = await workbench.gate_status(steward, release_id=release_id)
        assert status["passed"] is False

        run = await _run(stack, definition)
        assert run["passed"] is True
        [status] = await workbench.gate_status(steward, release_id=release_id)
        assert status == {**status, "passed": True, "run_id": run["id"]}

    async def test_only_an_admin_overrides_and_the_override_is_audited(self, stack) -> None:
        steward, release_id = stack["steward"], stack["release_id"]
        workbench = EvaluationWorkbench(stack["harness"].context)
        row, _ = await stack["service"].register_benchmark(
            steward, _retrieval_benchmark([str(stack["yeast_id"])])
        )
        await workbench.set_release_gate(
            steward, benchmark_id=row["benchmark_id"], version=row["version"]
        )
        data_steward = make_actor(
            user_id=stack["deployment"].outsider_actor.user_id,
            workspace_id=steward.workspace_id,
            role=Role.DATA_STEWARD,
        )
        releases = ReleaseService(stack["harness"].context)
        with pytest.raises(PolicyDenied, match="only an admin"):
            await releases.publish(data_steward, release_id=release_id, override_reason="urgent")

        # The seeded release is already published; an approved one is what a steward
        # would be publishing.
        async with stack["harness"].context.uow_factory() as uow:
            release = await uow.releases.get(steward.workspace_id, release_id)
            release.status = ReleaseState.APPROVED
            await uow.releases.update(release)
            await uow.commit()
        await releases.publish(steward, release_id=release_id, override_reason="benchmark is stale")
        async with stack["harness"].context.uow_factory() as uow:
            events = await uow.audit.list_events(steward.workspace_id, limit=50)
        [override] = [e for e in events if e.action.value == "release_gate_overridden"]
        assert override.metadata["reason"] == "benchmark is stale"
        assert override.metadata["unmet"] == ["lab-retrieval@1.0.0"]

    async def test_a_gate_is_lifted_by_clearing_its_version(self, stack) -> None:
        steward = stack["steward"]
        workbench = EvaluationWorkbench(stack["harness"].context)
        row, _ = await stack["service"].register_benchmark(
            steward, _retrieval_benchmark([str(stack["yeast_id"])])
        )
        assert await workbench.set_release_gate(
            steward, benchmark_id=row["benchmark_id"], version=row["version"]
        )
        assert (
            await workbench.set_release_gate(
                steward, benchmark_id=row["benchmark_id"], version=None
            )
            == []
        )


class TestHumanReview:
    async def _generation_run(self, stack) -> dict[str, Any]:
        run = await _run(stack, _synthetic("generation-citations.json"))
        assert run["status"] == "succeeded", run["error_detail"]
        return run

    async def test_cases_are_sampled_assigned_graded_and_summarised(self, stack) -> None:
        steward = stack["steward"]
        workbench = EvaluationWorkbench(stack["harness"].context)
        run = await self._generation_run(stack)
        created = await workbench.assign_reviews(
            steward, run_id=run["id"], reviewer_ids=[steward.user_id], sample_size=1
        )
        assert len(created) == 1
        # Asking again takes the next case, not the same one twice.
        again = await workbench.assign_reviews(
            steward, run_id=run["id"], reviewer_ids=[steward.user_id], sample_size=5
        )
        assert len(again) == 1
        assert {row["case_id"] for row in created} != {row["case_id"] for row in again}

        [first, _second] = await workbench.my_reviews(steward)
        assert first["case"]["question"] and first["case"]["answer"]
        submitted = await workbench.submit_review(
            steward,
            review_id=first["id"],
            scores={"correctness": 4, "citation_support": 5},
            flags=["corpus_gap"],
            reviewer_confidence=3,
            comment="Mostly right.",
            suggested_answer=None,
        )
        assert submitted["status"] == "submitted"
        summary = await workbench.run_reviews(steward, run_id=run["id"])
        assert (summary["assigned"], summary["submitted"]) == (2, 1)
        assert summary["mean_scores"]["correctness"] == 4
        assert summary["mean_scores"]["usefulness"] is None
        assert summary["flag_counts"]["corpus_gap"] == 1
        mine = await workbench.my_reviews(steward)
        assert [item["status"] for item in mine] == ["assigned", "submitted"]

    async def test_a_researcher_cannot_be_a_reviewer(self, stack) -> None:
        run = await self._generation_run(stack)
        with pytest.raises(ValidationFailure, match="cannot review"):
            await EvaluationWorkbench(stack["harness"].context).assign_reviews(
                stack["steward"],
                run_id=run["id"],
                reviewer_ids=[stack["deployment"].outsider_actor.user_id],
                sample_size=1,
            )

    async def test_a_retrieval_run_has_no_answers_to_review(self, stack) -> None:
        run = await _run(stack, _retrieval_benchmark([str(stack["yeast_id"])]))
        with pytest.raises(ValidationFailure, match="generation run"):
            await EvaluationWorkbench(stack["harness"].context).assign_reviews(
                stack["steward"],
                run_id=run["id"],
                reviewer_ids=[stack["steward"].user_id],
                sample_size=1,
            )

    @pytest.mark.parametrize(
        ("scores", "message"),
        [
            ({"correctness": 6}, "between 1 and 5"),
            ({"usefulness": 3}, "correctness"),
            ({"correctness": 3, "vibes": 2}, "unknown scores"),
        ],
    )
    async def test_a_malformed_review_is_refused(self, stack, scores, message) -> None:
        steward = stack["steward"]
        workbench = EvaluationWorkbench(stack["harness"].context)
        run = await self._generation_run(stack)
        [assigned] = await workbench.assign_reviews(
            steward, run_id=run["id"], reviewer_ids=[steward.user_id], sample_size=1
        )
        with pytest.raises(ValidationFailure, match=message):
            await workbench.submit_review(
                steward,
                review_id=assigned["id"],
                scores=scores,
                flags=[],
                reviewer_confidence=None,
                comment=None,
                suggested_answer=None,
            )

    async def test_someone_elses_review_cannot_be_submitted(self, stack) -> None:
        steward = stack["steward"]
        workbench = EvaluationWorkbench(stack["harness"].context)
        run = await self._generation_run(stack)
        [assigned] = await workbench.assign_reviews(
            steward, run_id=run["id"], reviewer_ids=[steward.user_id], sample_size=1
        )
        other = make_actor(
            user_id=stack["deployment"].outsider_actor.user_id,
            workspace_id=steward.workspace_id,
            role=Role.REVIEWER,
        )
        with pytest.raises(PolicyDenied, match="someone else"):
            await workbench.submit_review(
                other,
                review_id=assigned["id"],
                scores={"correctness": 3},
                flags=[],
                reviewer_confidence=None,
                comment=None,
                suggested_answer=None,
            )


def test_run_json_still_reports_cancelled_runs_as_not_passed() -> None:
    row = {
        "id": "r",
        "benchmark": "b@1",
        "benchmark_checksum": "c",
        "status": "cancelled",
        "gates": {"g": True},
    }
    assert run_json(row)["passed"] is False
