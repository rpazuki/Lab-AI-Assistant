"""Stored bytes are registered in the database, and the rule is enforced.

Twelve columns are foreign keys into ``objects``. Nothing wrote that table: the
object store wrote its own metadata beside the payload, every application service
then wrote an ``object_id`` into a dependent row, and no row in ``objects`` ever
appeared. SQLite ignores foreign keys unless asked, so the offline suite was
green; PostgreSQL enforces them, so acquisition could not insert an asset at all.

The bug is a write path somebody forgot, and a new write path is as easy to
forget. So the guard reads the AST: a ``put`` on an object store inside the
application layer must be accompanied, in the same function, by the
``register_object`` call that writes the row.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "packages" / "python" / "lab_assistant"
APPLICATION = PACKAGE_ROOT / "application"


def _unregistered_puts(path: Path) -> list[str]:
    """``object_store.put(...)`` calls whose function never registers the result.

    Scoped to the enclosing function rather than the module, because "somewhere
    in this file a registration happens" is exactly the kind of near-miss that
    leaves one of several write paths unregistered.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []

    def is_store_put(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "put"
            and "object_store" in ast.dump(node.func.value)
        )

    def registers(node: ast.AST) -> bool:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                name = inner.func
                if isinstance(name, ast.Name) and name.id == "register_object":
                    return True
                if isinstance(name, ast.Attribute) and name.attr == "register":
                    return True
        return False

    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        puts = [node for node in ast.walk(function) if is_store_put(node)]
        if puts and not registers(function):
            offenders.append(f"{function.name}() at line {puts[0].lineno}")
    return offenders


def _application_modules() -> list[Path]:
    return sorted(path for path in APPLICATION.rglob("*.py") if path.name != "__init__.py")


@pytest.mark.parametrize("path", _application_modules(), ids=lambda path: path.name)
def test_every_stored_object_is_registered(path: Path) -> None:
    offenders = _unregistered_puts(path)
    assert not offenders, (
        f"{path.relative_to(PACKAGE_ROOT)} stores bytes without registering them: {offenders}. "
        "Call register_object(uow, stored) in the transaction that writes the row "
        "referencing it, or the foreign key into objects has nothing to point at."
    )


def test_the_guard_actually_catches_an_unregistered_write(tmp_path: Path) -> None:
    """A guard nobody has seen fail is a guard that might match nothing."""
    forgotten = tmp_path / "forgotten.py"
    forgotten.write_text(
        "async def store(context, data, uow):\n"
        "    stored = await context.object_store.put(data, workspace_id=None,"
        " object_class='asset')\n"
        "    await uow.assets.add_asset(stored)\n",
        encoding="utf-8",
    )
    assert _unregistered_puts(forgotten) == ["store() at line 2"]

    registered = tmp_path / "registered.py"
    registered.write_text(
        "async def store(context, data, uow):\n"
        "    stored = await context.object_store.put(data, workspace_id=None,"
        " object_class='asset')\n"
        "    await register_object(uow, stored)\n"
        "    await uow.assets.add_asset(stored)\n",
        encoding="utf-8",
    )
    assert _unregistered_puts(registered) == []


def test_the_guard_sees_more_than_one_write_path() -> None:
    """The application really does store objects, so a green run means something."""
    stored_in = {path.name for path in _application_modules() if "object_store" in path.read_text()}
    assert {"acquisition.py", "discovery.py", "parsing.py", "releases.py"} <= stored_in
