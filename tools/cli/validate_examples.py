#!/usr/bin/env python3
"""Validate the example configuration against the live schemas.

An example that no longer validates is a documentation bug that would otherwise
be discovered by a new deployment. Every path referenced by the examples must also
exist, because "the guide mentions a file that isn't there" was a real complaint
about the predecessor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from lab_assistant.contracts.extraction import TemplateVersionSpec
from lab_assistant.domain.scope import ConnectorPlanEntry, ScopeVersion
from lab_assistant.evaluation.benchmarks import Benchmark
from lab_assistant.policy.rights import RightsPolicy

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "configs" / "examples"


def main() -> int:
    problems: list[str] = []
    if not EXAMPLES.is_dir():
        print(f"{EXAMPLES} does not exist", file=sys.stderr)
        return 1

    for path in sorted((EXAMPLES / "scopes").glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            ScopeVersion.model_validate(
                {
                    **payload,
                    "connector_plan": [
                        ConnectorPlanEntry.model_validate(entry)
                        for entry in payload.get("connector_plan", [])
                    ],
                }
            )
        except Exception as exc:
            problems.append(f"{path.name}: {exc}")

    for path in sorted((EXAMPLES / "templates").glob("*.yaml")):
        try:
            TemplateVersionSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except Exception as exc:
            problems.append(f"{path.name}: {exc}")

    for path in sorted((EXAMPLES / "policies").glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            RightsPolicy.model_validate(payload)
        except Exception as exc:
            problems.append(f"{path.name}: {exc}")

    for path in sorted((ROOT / "benchmarks" / "synthetic").glob("*.json")):
        try:
            Benchmark.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"{path.name}: {exc}")

    workspace = EXAMPLES / "workspace.example.yaml"
    if not workspace.exists():
        problems.append("workspace.example.yaml is missing")
    else:
        payload = yaml.safe_load(workspace.read_text(encoding="utf-8"))
        for key in ("slug", "display_name", "scopes", "templates", "rights_policy"):
            if key not in payload:
                problems.append(f"workspace.example.yaml: missing {key}")
        # Every referenced path must exist: a guide pointing at a missing file is
        # worse than no guide.
        references = [
            *payload.get("scopes", []),
            *payload.get("templates", []),
            payload.get("rights_policy"),
        ]
        for reference in references:
            if reference and not (EXAMPLES / reference).exists():
                problems.append(f"workspace.example.yaml references missing {reference}")
        # Examples use unusable credentials on purpose.
        text = workspace.read_text(encoding="utf-8")
        if "secret" in text.lower() and "name" not in text.lower():
            problems.append("workspace.example.yaml appears to contain a secret value")

    if problems:
        print("example configuration failed validation:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("example configuration validates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
