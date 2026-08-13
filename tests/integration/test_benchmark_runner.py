"""The synthetic benchmark run, in the ordinary suite.

Nightly CI runs `tools/cli/run_benchmarks.py`, but a benchmark harness that only
runs nightly regresses for a day before anyone notices — and the first thing to
regress silently is the part that makes the policy suite meaningful: whether the
"restricted" fixture work is actually restricted. It was not, the first time this
ran, and the probe passed for the wrong reason.

The tool lives in `tools/` rather than in the package because it depends on
`lab_assistant.testing`, which the evaluation layer may not import. Loading it by
path here keeps that boundary intact.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "benchmarks" / "synthetic"


def _load_runner() -> ModuleType:
    path = ROOT / "tools" / "cli" / "run_benchmarks.py"
    spec = importlib.util.spec_from_file_location("lab_benchmark_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load_runner()


class TestSyntheticBenchmarks:
    async def test_every_suite_passes_its_gates(self, runner, capsys) -> None:
        code = await runner.run_suites(SYNTHETIC, {"retrieval", "generation", "policy"})
        captured = capsys.readouterr()
        assert code == 0, captured.out
        assert "benchmark(s) passed" in captured.out
        # Each report carries the benchmark checksum, so a moved goalpost shows up in
        # the run rather than hiding behind an unchanged benchmark name.
        assert '"checksum"' in captured.out

    async def test_the_policy_probe_is_restrictive_for_the_right_reason(
        self, runner, capsys
    ) -> None:
        """The probe must fail if the restricted work becomes visible.

        A probe that passes because the subject can see *nothing* proves nothing, so
        this asserts the positive half too: the same outsider retrieves the public
        work and not the restricted one.
        """
        from lab_assistant.application.retrieval import RetrievalService

        deployment = runner.Deployment()
        await deployment.seed()
        retrieval = RetrievalService(deployment.harness.context)

        outsider = await retrieval.retrieve(
            deployment.outsider_actor, query="lipid yield strain", release_id=deployment.release_id
        )
        steward = await retrieval.retrieve(
            deployment.steward_actor, query="lipid yield strain", release_id=deployment.release_id
        )
        outsider_titles = {block.work_title for block in outsider.evidence}
        steward_titles = {block.work_title for block in steward.evidence}

        assert runner.PUBLIC_TITLE in outsider_titles, "the probe subject sees nothing at all"
        assert runner.RESTRICTED_TITLE not in outsider_titles
        assert runner.RESTRICTED_TITLE in steward_titles, "the group member lost their access"
        assert outsider.denied_count >= 1

    async def test_an_unmapped_seed_key_fails_loudly(self, runner) -> None:
        """A case naming an unknown work must not silently measure nothing."""
        from lab_assistant.evaluation.benchmarks import Benchmark, BenchmarkCase

        deployment = runner.Deployment()
        await deployment.seed()
        benchmark = Benchmark(
            benchmark_id="typo",
            version="1.0.0",
            kind="retrieval",
            description="a case with a mistyped seed key",
            origin="synthetic",
            licence="internal",
            permitted_use="test",
            cases=(BenchmarkCase(case_id="oops", question="anything", relevant=("work:typo",)),),
        )
        with pytest.raises(SystemExit) as raised:
            deployment.resolve_seed_keys(benchmark)
        assert "unknown seed key" in str(raised.value)

    async def test_listing_does_not_need_a_deployment(self, runner, capsys) -> None:
        assert runner._list(SYNTHETIC) == 0
        assert '"benchmark"' in capsys.readouterr().out
