"""Every index the migration creates is described by the metadata.

`env.py` says CI runs `alembic check` as the drift guard, and autogenerate
compares the database against `tables.py`. Three indexes were created by
`op.execute` and declared nowhere, so autogenerate wanted to drop them on every
run and the guard reported a standing failure against any migrated database. A
guard that always fails is a guard nobody reads, and a real drift would have
been indistinguishable from the noise.

This catches the next one at the point it is written, rather than the next time
somebody runs `alembic check` against a real database — which, on the evidence,
is rarely.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lab_assistant.adapters.db.tables import METADATA, embedding_table_for

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "database" / "migrations" / "versions"

#: Indexes named in a migration's raw SQL.
_CREATE_INDEX = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _declared_index_names() -> set[str]:
    for dimension in (64, 384, 768, 1024):
        embedding_table_for(dimension)
    return {index.name for table in METADATA.tables.values() for index in table.indexes}


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.py"))


def test_there_are_migrations_to_check() -> None:
    """A regex that matches nothing passes every assertion below."""
    assert _migration_files()


@pytest.mark.parametrize("path", _migration_files(), ids=lambda path: path.name)
def test_raw_sql_indexes_are_declared_in_the_metadata(path: Path) -> None:
    declared = _declared_index_names()
    created = set(_CREATE_INDEX.findall(path.read_text(encoding="utf-8")))
    undeclared = sorted(created - declared)
    assert not undeclared, (
        f"{path.name} creates {undeclared} in raw SQL, and tables.py does not declare them. "
        "`alembic check` compares the database against the metadata, so autogenerate will "
        "want to drop each one on every run. Declare it as an Index(...) — with "
        ".ddl_if(dialect='postgresql') when the expression is PostgreSQL-only."
    )


def test_the_expression_indexes_are_declared() -> None:
    """The three that were missing, named so a regex change cannot hide them."""
    declared = _declared_index_names()
    assert {
        "ix_search_chunks_fts",
        "ix_claim_projections_subject",
        "ix_tasks_claimable",
    } <= declared


def test_the_guard_would_notice_an_undeclared_index(tmp_path: Path) -> None:
    """A guard nobody has seen fail is a guard that might match nothing."""
    migration = tmp_path / "0002_example.py"
    migration.write_text(
        'op.execute("CREATE INDEX IF NOT EXISTS ix_not_declared ON works (title)")\n',
        encoding="utf-8",
    )
    created = set(_CREATE_INDEX.findall(migration.read_text(encoding="utf-8")))
    assert created == {"ix_not_declared"}
    assert not created <= _declared_index_names()
