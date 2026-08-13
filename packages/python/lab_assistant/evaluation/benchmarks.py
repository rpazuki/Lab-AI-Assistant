"""Benchmark definitions and the registry that versions them.

A benchmark records its origin, licence, permitted use, and sensitivity. Gold
labels are never overwritten silently: a changed label is a new benchmark version,
and the previous version stays readable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import canonical_json, sha256_bytes

__all__ = [
    "Benchmark",
    "BenchmarkCase",
    "BenchmarkRegistry",
    "PolicyProbe",
]


class BenchmarkCase(BaseModel):
    model_config = {"frozen": True}

    case_id: str
    question: str | None = None
    #: Relevant keys depend on the benchmark kind: work identifiers for
    #: discovery, chunk keys or work ids for retrieval, predicates for extraction.
    relevant: tuple[str, ...] = ()
    filters: dict[str, Any] = Field(default_factory=dict)
    expected_citation_count: int | None = None
    notes: str | None = None


class PolicyProbe(BaseModel):
    """A negative test: this subject must NOT be able to see this thing."""

    model_config = {"frozen": True}

    probe_id: str
    user_email: str
    workspace_slug: str
    query: str
    forbidden_work_titles: tuple[str, ...] = ()
    #: Also assert that totals do not leak the item's existence.
    assert_zero_results: bool = False


class Benchmark(BaseModel):
    model_config = {"frozen": True}

    benchmark_id: str
    version: str
    kind: str  # discovery | identity | parsing | extraction | retrieval | generation | policy
    description: str
    origin: str
    licence: str
    permitted_use: str
    sensitivity: str = "internal"
    blinded: bool = False
    cases: tuple[BenchmarkCase, ...] = ()
    probes: tuple[PolicyProbe, ...] = ()
    thresholds: dict[str, float] = Field(default_factory=dict)

    @property
    def checksum(self) -> str:
        return sha256_bytes(canonical_json(self.model_dump(mode="json")))

    @property
    def logical_key(self) -> str:
        return f"{self.benchmark_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class BenchmarkRegistry:
    """Loads benchmarks from a directory of JSON files."""

    root: Path

    def load(self, benchmark_id: str, version: str | None = None) -> Benchmark:
        for path in sorted(self.root.rglob("*.json")):
            benchmark = self._read(path)
            if benchmark.benchmark_id != benchmark_id:
                continue
            if version is None or benchmark.version == version:
                return benchmark
        raise LookupError(
            f"benchmark {benchmark_id}@{version or 'any'} not found under {self.root}"
        )

    def list_all(self) -> tuple[Benchmark, ...]:
        return tuple(self._read(path) for path in sorted(self.root.rglob("*.json")))

    @staticmethod
    def _read(path: Path) -> Benchmark:
        return Benchmark.model_validate(json.loads(path.read_text(encoding="utf-8")))
