"""Stage-level metrics.

Quality is not one end-to-end score. A good answer can hide bad discovery
coverage; a good retrieval score can hide access leakage. Each function here
measures one layer, so a regression can be attributed to the layer that caused it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import log2

__all__ = [
    "ExtractionScores",
    "RetrievalScores",
    "citation_validity",
    "extraction_scores",
    "faithfulness_proxy",
    "gold_seed_recall",
    "mrr",
    "ndcg_at_k",
    "recall_at_k",
    "retrieval_scores",
    "unique_contribution",
]


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    hits = len(set(retrieved[:k]) & relevant_set)
    return hits / len(relevant_set)


def mrr(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    relevant_set = set(relevant)
    for position, key in enumerate(retrieved, start=1):
        if key in relevant_set:
            return 1.0 / position
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    gains = [1.0 if key in relevant_set else 0.0 for key in retrieved[:k]]
    dcg = sum(gain / log2(index + 2) for index, gain in enumerate(gains))
    ideal = sum(1.0 / log2(index + 2) for index in range(min(k, len(relevant_set))))
    return dcg / ideal if ideal else 0.0


def gold_seed_recall(found_identifiers: Iterable[str], gold: Iterable[str]) -> float:
    gold_set = {value.casefold() for value in gold}
    if not gold_set:
        return 1.0
    found = {value.casefold() for value in found_identifiers}
    return len(found & gold_set) / len(gold_set)


def unique_contribution(
    by_connector: dict[str, set[str]],
) -> dict[str, int]:
    """Works found only by each connector.

    This is the number that justifies keeping a connector, or retiring it.
    """
    result: dict[str, int] = {}
    for connector, works in by_connector.items():
        others: set[str] = set()
        for other, other_works in by_connector.items():
            if other != connector:
                others |= other_works
        result[connector] = len(works - others)
    return result


@dataclass(frozen=True, slots=True)
class RetrievalScores:
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    duplicate_work_rate: float
    citation_ready_rate: float
    k: int

    def as_dict(self) -> dict[str, float]:
        return {
            f"recall@{self.k}": round(self.recall_at_k, 4),
            "mrr": round(self.mrr, 4),
            f"ndcg@{self.k}": round(self.ndcg_at_k, 4),
            "duplicate_work_rate": round(self.duplicate_work_rate, 4),
            "citation_ready_rate": round(self.citation_ready_rate, 4),
        }


def retrieval_scores(
    *,
    retrieved_keys: Sequence[str],
    relevant_keys: Iterable[str],
    retrieved_work_ids: Sequence[str],
    citation_ready: Sequence[bool],
    k: int,
) -> RetrievalScores:
    duplicates = (
        (len(retrieved_work_ids) - len(set(retrieved_work_ids))) / len(retrieved_work_ids)
        if retrieved_work_ids
        else 0.0
    )
    ready = (
        sum(1 for value in citation_ready if value) / len(citation_ready) if citation_ready else 0.0
    )
    return RetrievalScores(
        recall_at_k=recall_at_k(retrieved_keys, relevant_keys, k),
        mrr=mrr(retrieved_keys, relevant_keys),
        ndcg_at_k=ndcg_at_k(retrieved_keys, relevant_keys, k),
        duplicate_work_rate=duplicates,
        citation_ready_rate=ready,
        k=k,
    )


@dataclass(frozen=True, slots=True)
class ExtractionScores:
    precision: float
    recall: float
    evidence_validity: float
    abstention_accuracy: float
    multi_experiment_recall: float

    def as_dict(self) -> dict[str, float]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "evidence_validity": round(self.evidence_validity, 4),
            "abstention_accuracy": round(self.abstention_accuracy, 4),
            "multi_experiment_recall": round(self.multi_experiment_recall, 4),
        }


def extraction_scores(
    *,
    proposed: Sequence[tuple[str, str]],
    gold: Sequence[tuple[str, str]],
    evidence_valid: Sequence[bool],
    abstentions_correct: Sequence[bool],
    experiments_found: int,
    experiments_gold: int,
) -> ExtractionScores:
    proposed_set = set(proposed)
    gold_set = set(gold)
    precision = len(proposed_set & gold_set) / len(proposed_set) if proposed_set else 0.0
    recall = len(proposed_set & gold_set) / len(gold_set) if gold_set else 1.0
    validity = (
        sum(1 for value in evidence_valid if value) / len(evidence_valid) if evidence_valid else 1.0
    )
    abstention = (
        sum(1 for value in abstentions_correct if value) / len(abstentions_correct)
        if abstentions_correct
        else 1.0
    )
    multi = experiments_found / experiments_gold if experiments_gold else 1.0
    return ExtractionScores(
        precision=precision,
        recall=recall,
        evidence_validity=validity,
        abstention_accuracy=abstention,
        multi_experiment_recall=min(1.0, multi),
    )


def citation_validity(*, cited: Sequence[str], supplied: Iterable[str]) -> float:
    """Fraction of cited handles that the server actually supplied."""
    supplied_set = set(supplied)
    if not cited:
        return 1.0
    return sum(1 for handle in cited if handle in supplied_set) / len(cited)


def faithfulness_proxy(*, answer: str, evidence_texts: Sequence[str]) -> float:
    """Mechanical faithfulness proxy: are the answer's numbers in the evidence?

    Deliberately not a model-as-judge score. This is the cheap check that runs on
    every release; expert review and a versioned judge model remain necessary, and
    a proxy that pretends otherwise would be worse than none.
    """
    import re

    numbers = re.findall(r"\d+(?:\.\d+)?", answer)
    if not numbers:
        return 1.0
    haystack = " ".join(evidence_texts)
    supported = sum(1 for number in numbers if number in haystack)
    return supported / len(numbers)
