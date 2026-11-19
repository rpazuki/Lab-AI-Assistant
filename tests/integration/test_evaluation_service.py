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
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from lab_assistant.contracts.connector import IdentifierScheme
from lab_assistant.contracts.errors import (
    ConcurrencyConflict,
    InstallationDenied,
    PolicyDenied,
    ValidationFailure,
)
from lab_assistant.domain.identity import WorkIdentifier
from lab_assistant.domain.installation import InstallationRole
from lab_assistant.domain.states import ReleaseState
from lab_assistant.domain.workspace import Role
from lab_assistant.evaluation.service import EvaluationService, run_json
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
