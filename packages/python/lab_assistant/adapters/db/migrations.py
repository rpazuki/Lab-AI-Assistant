"""The application's Alembic revisions, oldest first.

Read from the migration scripts rather than from the database, because the
question it answers is about the *code*: is the revision recorded in a backup
one this application has ever heard of? A revision the code does not know means
the dump came from a newer schema, and restoring it into older code loses the
columns the older code cannot see — silently, which is the whole problem.

Parsed from the module-level `revision` / `down_revision` assignments rather
than by importing the scripts: importing a migration executes it at module
scope in some codebases, and a preflight check must not have side effects.
"""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_VERSIONS_DIR", "expected_revision", "migration_order", "schema_revision_reader"]

#: `database/migrations/versions`, relative to the repository root. Resolved
#: from this file so it works from a checkout and from an installed package in
#: the container, where the repository is at a different path.
DEFAULT_VERSIONS_DIR = Path(__file__).resolve().parents[5] / "database" / "migrations" / "versions"


def migration_order(versions_dir: Path | None = None) -> tuple[str, ...]:
    """Every revision this application knows, oldest first.

    Returns an empty tuple when the scripts are not readable — running from a
    wheel without the migration directory, say. An empty tuple is a real answer
    that `evaluate_restore` reports as "could not establish", which is better
    than a guess that would let a future dump through.
    """
    directory = Path(versions_dir) if versions_dir is not None else DEFAULT_VERSIONS_DIR
    if not directory.is_dir():
        return ()

    down: dict[str, str | None] = {}
    for path in sorted(directory.glob("*.py")):
        revision, parent = _revisions(path)
        if revision is not None:
            down[revision] = parent
    if not down:
        return ()

    children: dict[str | None, list[str]] = {}
    for revision, parent in down.items():
        children.setdefault(parent, []).append(revision)

    # Walk forward from the base. A branched history yields more than one child;
    # sorting makes the order deterministic rather than filesystem-dependent, and
    # a membership check — which is what the caller does — does not care which
    # branch a revision sits on.
    order: list[str] = []
    frontier = sorted(children.get(None, []))
    seen: set[str] = set()
    while frontier:
        revision = frontier.pop(0)
        if revision in seen:
            continue
        seen.add(revision)
        order.append(revision)
        frontier.extend(sorted(children.get(revision, [])))
        frontier.sort(key=lambda value: (value in seen, value))

    # A revision whose parent is missing is unreachable from the base. It is
    # still a revision this application knows, so it belongs in the answer:
    # leaving it out would make a legitimate dump look like it came from the
    # future.
    order.extend(sorted(set(down) - seen))
    return tuple(order)


def _revisions(path: Path) -> tuple[str | None, str | None]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):  # pragma: no cover - a broken script is not an order
        return None, None
    revision: str | None = None
    parent: str | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        literal = value.value if isinstance(value, ast.Constant) else None
        if target.id == "revision" and isinstance(literal, str):
            revision = literal
        elif target.id == "down_revision":
            parent = literal if isinstance(literal, str) else None
    return revision, parent


def expected_revision(versions_dir: Path | None = None) -> str | None:
    """The revision this build expects a database to be at: the migration head.

    ``None`` when the scripts are unreadable or the history has branched into
    more than one head. Both are reported by the caller as "not checked" rather
    than as a mismatch — a readiness probe that fails because it could not
    measure something takes down a deployment that was working.
    """
    order = migration_order(versions_dir)
    if not order:
        return None
    directory = Path(versions_dir) if versions_dir is not None else DEFAULT_VERSIONS_DIR
    parents = {parent for _, parent in (_revisions(path) for path in directory.glob("*.py"))}
    heads = [revision for revision in order if revision not in parents]
    return heads[0] if len(heads) == 1 else None


def schema_revision_reader(engine: Any) -> Callable[[], Awaitable[str | None]]:
    """A `SchemaRevisionReader` over an async engine.

    Deliberately not the dump-and-restore adapter, which can answer the same
    question: a web process should not carry a `pg_dump` runner, nor the
    credentials one implies, to read a single string.
    """

    async def read() -> str | None:
        from sqlalchemy import text

        async with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                present = (
                    await connection.execute(text("SELECT to_regclass('public.alembic_version')"))
                ).scalar()
                if present is None:
                    return None
            else:
                names = await connection.run_sync(
                    lambda sync: sync.dialect.has_table(sync, "alembic_version")
                )
                if not names:
                    return None
            rows = (await connection.execute(text("SELECT version_num FROM alembic_version"))).all()
        if not rows:
            return None
        if len(rows) > 1:  # branched history; not a single answer
            return ",".join(sorted(str(row[0]) for row in rows))
        return str(rows[0][0])

    return read
