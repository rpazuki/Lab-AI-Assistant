"""Evaluation: benchmarks, metrics, runs, and release gates."""

from lab_assistant.evaluation.benchmarks import (
    Benchmark,
    BenchmarkCase,
    BenchmarkRegistry,
    PolicyProbe,
)
from lab_assistant.evaluation.metrics import (
    citation_validity,
    extraction_scores,
    faithfulness_proxy,
    gold_seed_recall,
    mrr,
    ndcg_at_k,
    recall_at_k,
    retrieval_scores,
    unique_contribution,
    unsupported_numbers,
)
from lab_assistant.evaluation.runner import EvaluationRun, EvaluationRunner

__all__ = [
    "Benchmark",
    "BenchmarkCase",
    "BenchmarkRegistry",
    "EvaluationRun",
    "EvaluationRunner",
    "PolicyProbe",
    "citation_validity",
    "extraction_scores",
    "faithfulness_proxy",
    "gold_seed_recall",
    "mrr",
    "ndcg_at_k",
    "recall_at_k",
    "retrieval_scores",
    "unique_contribution",
    "unsupported_numbers",
]
