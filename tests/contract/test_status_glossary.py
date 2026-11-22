"""The web app's status glossary explains exactly the values each enum has.

`apps/web/src/lib/statuses.json` is what a member reads when they select the info
button beside a status, and what the how-to guides tabulate. A value the backend
can produce but the glossary does not describe would show a chip with no
explanation; a described value the enum no longer has is documentation of nothing.
Both fail here, so adding an enum member means describing it in the same change.
"""

from __future__ import annotations

import importlib
import json
from enum import StrEnum
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GLOSSARY = json.loads((REPO / "apps/web/src/lib/statuses.json").read_text(encoding="utf-8"))
BACKED = sorted(kind for kind, group in GLOSSARY.items() if group.get("enum"))


def _enum(path: str) -> type[StrEnum]:
    module, _, name = path.rpartition(".")
    found = getattr(importlib.import_module(module), name)
    assert issubclass(found, StrEnum), path
    return found


def test_every_group_is_complete_and_titled() -> None:
    for kind, group in GLOSSARY.items():
        assert group["title"].strip(), kind
        assert group["values"], kind
        for value, meaning in group["values"].items():
            assert meaning.strip(), f"{kind}.{value} has no description"
        assert set(group.get("ui_only", ())) <= set(group["values"]), kind


@pytest.mark.parametrize("kind", BACKED)
def test_glossary_matches_its_enum(kind: str) -> None:
    group = GLOSSARY[kind]
    expected = {member.value for member in _enum(group["enum"])}
    described = set(group["values"]) - set(group.get("ui_only", ()))
    assert expected - described == set(), f"{kind}: undescribed values"
    assert described - expected == set(), f"{kind}: values the enum does not have"


def test_the_statuses_people_see_are_backed_by_their_enums() -> None:
    """The groups for the lifecycle states must stay tied to the code, not drift."""
    for kind in (
        "discovery_run",
        "source_outcome",
        "acquisition",
        "acquisition_outcome",
        "relevance",
        "identity",
        "release",
        "task",
        "audit_action",
    ):
        assert GLOSSARY[kind].get("enum"), kind
