"""The committed JSON Schemas match the contracts they were generated from.

`generate_schemas.py` says "CI regenerates them and fails on drift". CI checks
`schemas/openapi.json` and nothing else, so a contract could change and leave a
stale schema committed for a consumer to validate against — the third claim in
this repository that was documented and not enforced.

The schemas exist so a consumer can validate a payload without importing this
package. A stale one is worse than none: it rejects valid payloads and accepts
invalid ones, with the repository's authority behind it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "cli"))

from generate_schemas import SCHEMAS, TARGETS  # noqa: E402


def _regenerate(model, version: str, relative: str) -> dict:
    schema = TypeAdapter(model).json_schema(by_alias=True)
    schema["$id"] = f"https://lab-ai-assistant.invalid/schemas/{relative}"
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-semantic-version"] = version
    return schema


def test_there_are_schemas_to_check() -> None:
    assert TARGETS


@pytest.mark.parametrize("relative", sorted(TARGETS), ids=lambda value: value.split("/")[-1])
def test_the_committed_schema_matches_its_contract(relative: str) -> None:
    model, version = TARGETS[relative]
    path = SCHEMAS / relative
    assert path.is_file(), f"{relative} is registered and not committed; run generate_schemas.py"
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert committed == _regenerate(model, version, relative), (
        f"{relative} is stale: run `python tools/cli/generate_schemas.py` and commit it. "
        "A consumer validates against the committed file, not against this package."
    )


def test_the_draft_archive_schemas_say_they_are_drafts() -> None:
    """A draft a consumer mistook for a stable contract is a trap."""
    for relative in ("archive/snapshot-manifest.schema.json",
                     "archive/stage-result-envelope.schema.json"):
        assert relative in TARGETS
        assert TARGETS[relative][1].endswith("-draft")
