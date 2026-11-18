#!/usr/bin/env python3
"""Generate JSON Schema for the domain contracts.

Schemas have stable ids and semantic versions, and they are committed so a
consumer can validate a payload without importing this package. CI regenerates
them and fails on drift.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from lab_assistant.contracts.archive import ArtifactRef, SnapshotManifest, StageResultEnvelope
from lab_assistant.contracts.archive_policy import PolicyCheckpoint, PolicyHeadDescriptor
from lab_assistant.contracts.backup import BackupManifest
from lab_assistant.contracts.extraction import ClaimProposal, TemplateVersionSpec
from lab_assistant.contracts.parsing import RenditionDocument
from lab_assistant.contracts.release import ExportPackageManifest, ReleaseManifest
from lab_assistant.domain.observation import SourceAssertion
from lab_assistant.domain.scope import ScopeVersion

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"

TARGETS = {
    "source-observation/source-assertion.schema.json": (SourceAssertion, "1.0.0"),
    "source-observation/scope-version.schema.json": (ScopeVersion, "1.0.0"),
    "rendition/rendition-document.schema.json": (RenditionDocument, "1.0.0"),
    "claim/claim-proposal.schema.json": (ClaimProposal, "1.0.0"),
    "claim/template-version.schema.json": (TemplateVersionSpec, "1.0.0"),
    "release-package/release-manifest.schema.json": (ReleaseManifest, "1.0.0"),
    "release-package/export-package-manifest.schema.json": (ExportPackageManifest, "1.0.0"),
    # Not a draft: `lab backup create` writes this file today, and an operator's
    # own tooling reads it without importing this package.
    "recovery/backup-manifest.schema.json": (BackupManifest, "1.0.0"),
    # Draft: nothing publishes or imports these yet (phase 2). Committed now so
    # the format can be reviewed before an implementation makes it expensive to
    # change, and so the fixture and the format are designed against each other.
    "archive/snapshot-manifest.schema.json": (SnapshotManifest, "1.0.0-draft"),
    "archive/stage-result-envelope.schema.json": (StageResultEnvelope, "1.0.0-draft"),
    # One row of a snapshot's artifact inventory, including its retention edges.
    "archive/artifact-ref.schema.json": (ArtifactRef, "1.0.0-draft"),
    # The policy stream: a checkpoint and the head that names the current one.
    "archive/policy-checkpoint.schema.json": (PolicyCheckpoint, "1.0.0-draft"),
    "archive/policy-head.schema.json": (PolicyHeadDescriptor, "1.0.0-draft"),
}


def main() -> int:
    for relative, (model, version) in TARGETS.items():
        schema = TypeAdapter(model).json_schema(by_alias=True)
        schema["$id"] = f"https://lab-ai-assistant.invalid/schemas/{relative}"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["x-semantic-version"] = version
        path = SCHEMAS / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
