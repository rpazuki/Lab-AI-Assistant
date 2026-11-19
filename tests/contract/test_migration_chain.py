"""The migration files form one chain alembic can store.

A revision id longer than 32 characters passes every SQLite test and then fails
on the first real upgrade: alembic's own ``alembic_version.version_num`` is a
``VARCHAR(32)``, and the update that records the revision is refused after the
migration's DDL has run. These checks read the files, so they need no database.
"""

from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[2] / "database" / "migrations" / "versions"


def _revisions() -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision = "([^"]+)"', text, re.M)
        down = re.search(r'^down_revision = (?:"([^"]+)"|None)', text, re.M)
        assert revision and down, f"{path.name} does not declare its revisions"
        found[revision.group(1)] = down.group(1)
    return found


def test_every_revision_id_fits_alembic_version() -> None:
    too_long = [revision for revision in _revisions() if len(revision) > 32]
    assert too_long == [], f"revision ids over 32 characters: {too_long}"


def test_the_revisions_form_one_unbranched_chain() -> None:
    revisions = _revisions()
    roots = [revision for revision, down in revisions.items() if down is None]
    assert len(roots) == 1
    children: dict[str | None, list[str]] = {}
    for revision, down in revisions.items():
        children.setdefault(down, []).append(revision)
    assert all(len(items) == 1 for items in children.values()), children
    assert all(down in revisions for down in revisions.values() if down is not None)
