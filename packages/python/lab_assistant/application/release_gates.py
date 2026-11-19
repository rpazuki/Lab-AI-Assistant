"""Benchmarks a release must pass before it is published.

A steward names benchmark versions per workspace; publishing a release then needs,
for each, a run against *that release* that succeeded with every gate true. The
check lives here, beside publishing, and reads runs through the repository: the
application layer may not import ``evaluation``, and publishing must not depend
on the evaluation worker being installed to enforce what was already measured.

An admin may publish anyway with a reason. The override is the exception the
audit log records, not a setting: the next release is gated again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

__all__ = ["GateStatus", "evaluation_gate_status", "run_passed"]


def run_passed(run: dict[str, Any]) -> bool:
    """The same verdict the API reports: succeeded, with gates, all of them true."""
    gates = run.get("gates") or {}
    return run.get("status", "succeeded") == "succeeded" and bool(gates) and all(gates.values())


@dataclass(frozen=True, slots=True)
class GateStatus:
    benchmark_id: str
    version: str
    passed: bool
    #: The passing run, or else the newest run of this benchmark on this release.
    run_id: str | None
    run_status: str | None

    @property
    def key(self) -> str:
        return f"{self.benchmark_id}@{self.version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "version": self.version,
            "key": self.key,
            "passed": self.passed,
            "run_id": self.run_id,
            "run_status": self.run_status,
        }


async def evaluation_gate_status(
    uow: Any, workspace_id: UUID, release_id: UUID
) -> list[GateStatus]:
    """Each required benchmark, and whether this release has passed it."""
    statuses: list[GateStatus] = []
    for gate in await uow.evaluations.list_release_gates(workspace_id):
        key = f"{gate['benchmark_id']}@{gate['version']}"
        runs = [
            run
            for run in await uow.evaluations.list_runs(
                workspace_id, release_id=release_id, limit=500
            )
            if run.get("benchmark") == key
        ]
        passing = next((run for run in runs if run_passed(run)), None)
        shown = passing or (runs[0] if runs else None)
        statuses.append(
            GateStatus(
                benchmark_id=gate["benchmark_id"],
                version=gate["version"],
                passed=passing is not None,
                run_id=str(shown["id"]) if shown else None,
                run_status=shown.get("status") if shown else None,
            )
        )
    return statuses
