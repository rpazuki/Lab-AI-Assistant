#!/usr/bin/env python3
"""Run the synthetic benchmarks against the deterministic stack.

Nightly CI runs this to catch a metric regression before a release gate does. It
seeds a two-workspace deployment in process, publishes a release, and then runs the
suites the release gates read:

* ``retrieval``  — recall, MRR, nDCG, and how many hits are citation-ready;
* ``generation`` — citation validity and unsupported-handle rate;
* ``policy``     — negative access probes, where **any** leak fails.

The numbers are only meaningful relative to the same fixtures, which is why each
benchmark records its own checksum and this runner prints it.

Synthetic benchmarks name works by a **seed key** (``work:yeast``) rather than by a
uuid, because a uuid is generated at run time. ``SEED_WORKS`` below is the mapping,
and it is part of the fixture contract: adding a case that names an unmapped key is
a failure rather than a silently skipped case.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from lab_assistant.application.chat import ChatService
from lab_assistant.contracts.policy import (
    Effect,
    PolicyAction,
    ResourceType,
    SubjectType,
)
from lab_assistant.domain.workspace import Role
from lab_assistant.evaluation.benchmarks import Benchmark, BenchmarkRegistry, PolicyProbe
from lab_assistant.evaluation.runner import EvaluationRun, EvaluationRunner
from lab_assistant.testing import build_harness, make_actor, publish_demo_corpus

#: Seed key -> the title the fixture corpus publishes for it.
SEED_WORKS: dict[str, str] = {
    "work:yeast": "Lipid production in an oleaginous yeast",
    "work:restricted": "Confidential strain screen",
}

PUBLIC_TITLE = SEED_WORKS["work:yeast"]
RESTRICTED_TITLE = SEED_WORKS["work:restricted"]

DEMO_WORKS: tuple[tuple[str, str], ...] = (
    (
        PUBLIC_TITLE,
        "Strain YL-1 reached a lipid yield of 0.21 g/g substrate at 30 degrees C "
        "on glycerol in a 1 L bioreactor.",
    ),
    (
        RESTRICTED_TITLE,
        "Unpublished screen: strain CS-7 reached a lipid yield of 0.44 g/g substrate "
        "under nitrogen limitation.",
    ),
)

QUERY_ACTIONS = (
    PolicyAction.QUERY,
    PolicyAction.VIEW_CONTENT,
    PolicyAction.INDEX,
    PolicyAction.DISPLAY_EXCERPT,
    PolicyAction.DISPLAY_FULL_CONTENT,
)


class Deployment:
    """A seeded two-workspace deployment: enough for the policy probes to mean something.

    ``rla-lab`` holds a public work and a restricted one. The steward sees both. The
    outsider is a real member of the same workspace with a grant on the public work
    only — which is the case a leak would actually come from, unlike a subject with
    no grants at all, who trivially sees nothing. ``coral-lab`` is a second workspace
    used to probe cross-workspace retrieval against the release under test.
    """

    def __init__(self) -> None:
        self.harness = build_harness()

    async def seed(self) -> None:
        harness = self.harness
        steward = await harness.accounts.create_user(
            email="steward@lab.invalid", password="a-long-passphrase"
        )
        outsider = await harness.accounts.create_user(
            email="outsider@lab.invalid", password="a-long-passphrase"
        )
        coral_owner = await harness.accounts.create_user(
            email="owner@coral-lab.invalid", password="a-long-passphrase"
        )
        workspace, _ = await harness.accounts.create_workspace(
            slug="rla-lab", display_name="RLA Lab", owner_user_id=steward.id
        )
        admin = make_actor(user_id=steward.id, workspace_id=workspace.id, role=Role.ADMIN)
        await harness.accounts.add_member(admin, user_id=outsider.id, role=Role.RESEARCHER)

        steward_grants = [
            await harness.accounts.grant(
                admin,
                subject_type=SubjectType.WORKSPACE,
                subject_id=workspace.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace.id,
                action=action,
                effect=Effect.ALLOW,
                policy_basis="workspace_corpus",
            )
            for action in QUERY_ACTIONS
        ]
        self.workspace = workspace
        # A group the outsider is not in. Restriction lives on the rights decision,
        # not on the grant: it is a property of the material, so every subject is
        # measured against it by one rule.
        private_group = await harness.accounts.create_group(
            admin,
            slug="unpublished-screens",
            display_name="Unpublished screens",
            member_user_ids=frozenset({steward.id}),
        )
        # The group's own grants are what unlock restricted material. A workspace-wide
        # grant deliberately does not: sharing restricted material with everyone is
        # the opposite of restricting it.
        group_grants = [
            await harness.accounts.grant(
                admin,
                subject_type=SubjectType.GROUP,
                subject_id=private_group.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace.id,
                action=action,
                effect=Effect.ALLOW,
                policy_basis="unpublished_screen_share",
            )
            for action in QUERY_ACTIONS
        ]
        self.steward_actor = make_actor(
            user_id=steward.id,
            workspace_id=workspace.id,
            role=Role.ADMIN,
            group_ids=(private_group.id,),
            grants=tuple(steward_grants) + tuple(group_grants),
        )
        self.corpus = await publish_demo_corpus(
            harness,
            self.steward_actor,
            works=DEMO_WORKS,
            name="benchmark-corpus",
            restrict_titles_to_group={RESTRICTED_TITLE: private_group.id},
        )
        self.release_id = self.corpus.release_id

        # The outsider is a normal member with the same workspace-wide grants as
        # anyone else. That is the case a leak would actually come from: a subject
        # with no grants at all trivially sees nothing and proves nothing.
        outsider_grants = [
            await harness.accounts.grant(
                admin,
                subject_type=SubjectType.WORKSPACE,
                subject_id=workspace.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace.id,
                action=action,
                effect=Effect.ALLOW,
                policy_basis="workspace_corpus",
            )
            for action in QUERY_ACTIONS
        ]
        self.outsider_actor = make_actor(
            user_id=outsider.id,
            workspace_id=workspace.id,
            role=Role.RESEARCHER,
            grants=tuple(outsider_grants),
        )

        coral, _ = await harness.accounts.create_workspace(
            slug="coral-lab", display_name="Coral Lab", owner_user_id=coral_owner.id
        )
        coral_admin = make_actor(user_id=coral_owner.id, workspace_id=coral.id, role=Role.ADMIN)
        coral_grants = [
            await harness.accounts.grant(
                coral_admin,
                subject_type=SubjectType.WORKSPACE,
                subject_id=coral.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=coral.id,
                action=action,
                effect=Effect.ALLOW,
                policy_basis="workspace_corpus",
            )
            for action in QUERY_ACTIONS
        ]
        self.coral_actor = make_actor(
            user_id=coral_owner.id,
            workspace_id=coral.id,
            role=Role.ADMIN,
            grants=tuple(coral_grants),
        )
        self.actors_by_email = {
            "steward@lab.invalid": self.steward_actor,
            "outsider@lab.invalid": self.outsider_actor,
            "owner@coral-lab.invalid": self.coral_actor,
        }

    def resolve_seed_keys(self, benchmark: Benchmark) -> Benchmark:
        """Rewrite seed keys to the work ids this run created.

        An unmapped key is an error: silently dropping it would leave a case that
        looks like it passed while measuring nothing.
        """
        cases = []
        for case in benchmark.cases:
            relevant: list[str] = []
            for key in case.relevant:
                title = SEED_WORKS.get(key)
                if title is None:
                    raise SystemExit(
                        f"benchmark {benchmark.logical_key} case {case.case_id} names "
                        f"unknown seed key {key!r}; add it to SEED_WORKS"
                    )
                relevant.append(str(self.corpus.work_ids[title]))
            cases.append(case.model_copy(update={"relevant": tuple(relevant)}))
        return benchmark.model_copy(update={"cases": tuple(cases)})

    async def probe_actor(self, probe: PolicyProbe) -> Any:
        return self.actors_by_email.get(probe.user_email)


async def run_suites(directory: Path, suites: set[str]) -> int:
    registry = BenchmarkRegistry(root=directory)
    benchmarks = registry.list_all()
    if not benchmarks:
        print(f"no benchmarks found under {directory}")
        return 1

    deployment = Deployment()
    await deployment.seed()
    runner = EvaluationRunner(deployment.harness.context)
    chat = ChatService(deployment.harness.context)
    session = await chat.create_session(deployment.steward_actor)

    reports: list[dict[str, Any]] = []
    executed = 0
    for benchmark in benchmarks:
        kind = benchmark.kind
        if kind not in suites:
            continue
        executed += 1
        if kind == "policy":
            run = await runner.run_policy_probes(
                benchmark=benchmark,
                release_id=deployment.release_id,
                actor_resolver=deployment.probe_actor,
            )
        elif kind == "generation":
            run = await runner.run_generation(
                deployment.steward_actor,
                benchmark=deployment.resolve_seed_keys(benchmark),
                release_id=deployment.release_id,
                session_id=session.id,
            )
        else:
            run = await runner.run_retrieval(
                deployment.steward_actor,
                benchmark=deployment.resolve_seed_keys(benchmark),
                release_id=deployment.release_id,
            )
        reports.append(_report(benchmark, run))

    if not executed:
        print(f"no benchmarks matched suites {sorted(suites)}")
        return 1

    print(json.dumps(reports, indent=2, sort_keys=True, default=str))
    failed = [report["benchmark"] for report in reports if not report["passed"]]
    if failed:
        print(f"failed: {', '.join(failed)}")
        return 1
    print(f"{executed} benchmark(s) passed")
    return 0


def _report(benchmark: Benchmark, run: EvaluationRun) -> dict[str, Any]:
    return {
        "benchmark": benchmark.logical_key,
        "kind": benchmark.kind,
        # The checksum travels with the numbers: a moved goalpost is then visible in
        # the diff rather than hidden behind an unchanged benchmark name.
        "checksum": benchmark.checksum,
        "blinded": benchmark.blinded,
        "thresholds": benchmark.thresholds,
        "metrics": run.metrics,
        "gates": run.gates,
        "failures": run.failures,
        "passed": run.passed,
        "cases": len(run.per_case),
    }


def _list(directory: Path) -> int:
    registry = BenchmarkRegistry(root=directory)
    benchmarks = registry.list_all()
    if not benchmarks:
        print(f"no benchmarks found under {directory}")
        return 1
    print(
        json.dumps(
            [
                {
                    "benchmark": benchmark.logical_key,
                    "kind": benchmark.kind,
                    "cases": len(benchmark.cases),
                    "probes": len(benchmark.probes),
                    "thresholds": benchmark.thresholds,
                    "checksum": benchmark.checksum,
                    "blinded": benchmark.blinded,
                }
                for benchmark in benchmarks
            ],
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmarks", type=Path, default=Path("benchmarks/synthetic"))
    parser.add_argument(
        "--suite",
        action="append",
        choices=["retrieval", "generation", "policy", "all"],
        help="which suite to run; repeatable. Defaults to all.",
    )
    parser.add_argument(
        "--list", action="store_true", help="print benchmark metadata without running"
    )
    args = parser.parse_args()
    if args.list:
        return _list(args.benchmarks)
    chosen = set(args.suite or ["all"])
    suites = {"retrieval", "generation", "policy"} if "all" in chosen else chosen
    return asyncio.run(run_suites(args.benchmarks, suites))


if __name__ == "__main__":
    raise SystemExit(main())
