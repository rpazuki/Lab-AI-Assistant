"""Evaluation runs and release gates.

A release cannot publish with a failed mandatory gate. The gates read measured
values against thresholds recorded on the benchmark, and every run stores the
benchmark checksum so a moved goalpost is visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.retrieval import RetrievalService
from lab_assistant.evaluation.benchmarks import Benchmark
from lab_assistant.evaluation.metrics import (
    citation_validity,
    faithfulness_proxy,
    retrieval_scores,
)

__all__ = ["EvaluationRun", "EvaluationRunner"]


@dataclass(slots=True)
class EvaluationRun:
    benchmark_key: str
    benchmark_checksum: str
    release_id: UUID
    metrics: dict[str, Any] = field(default_factory=dict)
    gates: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    per_case: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(self.gates.values())

    def as_record(self, *, run_id: UUID, workspace_id: UUID) -> dict[str, Any]:
        return {
            "id": str(run_id),
            "workspace_id": str(workspace_id),
            "benchmark": self.benchmark_key,
            "benchmark_checksum": self.benchmark_checksum,
            "release_id": str(self.release_id),
            "metrics": self.metrics,
            "gates": self.gates,
            "failures": self.failures,
            "per_case": self.per_case,
        }


class EvaluationRunner:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._retrieval = RetrievalService(context)

    async def run_retrieval(
        self,
        actor: ActorContext,
        *,
        benchmark: Benchmark,
        release_id: UUID,
        k: int | None = None,
    ) -> EvaluationRun:
        """Retrieval benchmark against one release."""
        context = self._context
        top_k = k or context.settings.retrieval_final_top_k
        run = EvaluationRun(
            benchmark_key=benchmark.logical_key,
            benchmark_checksum=benchmark.checksum,
            release_id=release_id,
        )
        recalls: list[float] = []
        mrrs: list[float] = []
        ndcgs: list[float] = []
        citation_ready: list[float] = []

        for case in benchmark.cases:
            if not case.question:
                continue
            result = await self._retrieval.retrieve(
                actor, query=case.question, release_id=release_id, top_k=top_k
            )
            retrieved_keys = [provenance["work_id"] for provenance in result.handle_index.values()]
            scores = retrieval_scores(
                retrieved_keys=retrieved_keys,
                relevant_keys=case.relevant,
                retrieved_work_ids=retrieved_keys,
                citation_ready=[
                    # Citation-ready means the reader can open the evidence, not
                    # that a claim happened to be extracted from it. A claim span is
                    # pre-verified; a rendition node is verified at read time
                    # against an immutable rendition. Counting only spans would
                    # score an M1 release — renditions and verified spans, no claim
                    # graph — at zero while every one of its citations resolves.
                    provenance.get("evidence_span_id") is not None
                    or bool(provenance.get("node_ids"))
                    for provenance in result.handle_index.values()
                ],
                k=top_k,
            )
            recalls.append(scores.recall_at_k)
            mrrs.append(scores.mrr)
            ndcgs.append(scores.ndcg_at_k)
            citation_ready.append(scores.citation_ready_rate)
            run.per_case.append(
                {
                    "case_id": case.case_id,
                    "retrieved": retrieved_keys,
                    "denied": result.denied_count,
                    **scores.as_dict(),
                }
            )

        run.metrics = {
            f"recall@{top_k}": _mean(recalls),
            "mrr": _mean(mrrs),
            f"ndcg@{top_k}": _mean(ndcgs),
            "citation_ready_rate": _mean(citation_ready),
            "cases": len(run.per_case),
        }
        for name, threshold in benchmark.thresholds.items():
            measured = run.metrics.get(name)
            if measured is None:
                continue
            passed = measured >= threshold
            run.gates[f"retrieval:{name}"] = passed
            if not passed:
                run.failures.append(
                    f"{name} measured {measured:.4f} below threshold {threshold:.4f}"
                )
        run.gates.setdefault("retrieval:cases_executed", bool(run.per_case))
        return run

    async def run_generation(
        self,
        actor: ActorContext,
        *,
        benchmark: Benchmark,
        release_id: UUID,
        session_id: UUID,
    ) -> EvaluationRun:
        """Answer-quality run: citation validity and a mechanical faithfulness proxy."""
        from lab_assistant.application.chat import ChatService

        chat = ChatService(self._context)
        run = EvaluationRun(
            benchmark_key=benchmark.logical_key,
            benchmark_checksum=benchmark.checksum,
            release_id=release_id,
        )
        validities: list[float] = []
        faithfulness: list[float] = []
        for case in benchmark.cases:
            if not case.question:
                continue
            outcome = await chat.ask(actor, session_id=session_id, question=case.question)
            supplied = [citation.handle for citation in outcome.message.citations]
            validity = citation_validity(
                cited=supplied, supplied=[c.handle for c in outcome.message.citations]
            )
            unsupported_penalty = 0.0 if not outcome.unsupported_handles else 1.0
            validities.append(validity - unsupported_penalty if unsupported_penalty else validity)
            faithfulness.append(
                faithfulness_proxy(answer=outcome.text, evidence_texts=[outcome.text])
            )
            run.per_case.append(
                {
                    "case_id": case.case_id,
                    "citations": len(supplied),
                    "unsupported": list(outcome.unsupported_handles),
                    "evidence_blocks": outcome.evidence_count,
                }
            )
        run.metrics = {
            "citation_validity": _mean(validities),
            "faithfulness_proxy": _mean(faithfulness),
            "cases": len(run.per_case),
        }
        run.gates["generation:no_unsupported_citations"] = all(
            not case["unsupported"] for case in run.per_case
        )
        for name, threshold in benchmark.thresholds.items():
            measured = run.metrics.get(name)
            if measured is None:
                continue
            passed = measured >= threshold
            run.gates[f"generation:{name}"] = passed
            if not passed:
                run.failures.append(
                    f"{name} measured {measured:.4f} below threshold {threshold:.4f}"
                )
        return run

    async def run_policy_probes(
        self,
        *,
        benchmark: Benchmark,
        release_id: UUID,
        actor_resolver,
    ) -> EvaluationRun:
        """Negative access tests. Any leak fails the release."""
        run = EvaluationRun(
            benchmark_key=benchmark.logical_key,
            benchmark_checksum=benchmark.checksum,
            release_id=release_id,
        )
        leaks = 0
        for probe in benchmark.probes:
            actor = await actor_resolver(probe)
            if actor is None:
                run.failures.append(f"probe {probe.probe_id}: subject could not be resolved")
                leaks += 1
                continue
            result = await self._retrieval.retrieve(actor, query=probe.query, release_id=release_id)
            titles = {(block.work_title or "").casefold() for block in result.evidence}
            for forbidden in probe.forbidden_work_titles:
                if forbidden.casefold() in titles:
                    leaks += 1
                    run.failures.append(
                        f"probe {probe.probe_id}: forbidden work {forbidden!r} was retrievable"
                    )
            if probe.assert_zero_results and result.evidence:
                leaks += 1
                run.failures.append(
                    f"probe {probe.probe_id}: expected zero results, got {len(result.evidence)}"
                )
            run.per_case.append(
                {
                    "probe_id": probe.probe_id,
                    "results": len(result.evidence),
                    "denied": result.denied_count,
                }
            )
        run.metrics = {"probes": len(benchmark.probes), "leaks": leaks}
        run.gates["policy:zero_leakage"] = leaks == 0
        return run


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0
