"""The dependency rules, enforced rather than documented.

    contracts            -> standard library + pydantic
    domain               -> contracts
    policy, provenance   -> contracts, domain
    application          -> contracts, domain, policy, provenance
    adapters             -> application ports + external SDKs

Forbidden imports are checked by reading the AST, so a violation fails CI instead
of being noticed in review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "packages" / "python" / "lab_assistant"

#: layer -> the project layers it may import.
ALLOWED: dict[str, frozenset[str]] = {
    "contracts": frozenset(),
    "domain": frozenset({"contracts"}),
    "policy": frozenset({"contracts", "domain"}),
    "provenance": frozenset({"contracts", "domain"}),
    "evaluation": frozenset({"contracts", "domain", "application", "policy", "provenance"}),
    "application": frozenset({"contracts", "domain", "policy", "provenance", "config"}),
    "adapters": frozenset(
        {"contracts", "domain", "policy", "provenance", "application", "config"}
    ),
    "migration": frozenset(
        {"contracts", "domain", "policy", "provenance", "application", "adapters", "config"}
    ),
    # The CLI is the outermost layer: it composes everything and is imported by
    # nothing, so it may reach the migration importers it exists to drive.
    "cli": frozenset(
        {
            "contracts",
            "domain",
            "policy",
            "provenance",
            "application",
            "adapters",
            "evaluation",
            "migration",
            "config",
            "testing",
        }
    ),
}

#: External packages a layer must never import.
FORBIDDEN_EXTERNAL: dict[str, frozenset[str]] = {
    "contracts": frozenset({"sqlalchemy", "fastapi", "httpx", "anthropic", "boto3", "openpyxl"}),
    "domain": frozenset({"sqlalchemy", "fastapi", "httpx", "anthropic", "boto3", "os", "pathlib"}),
    "policy": frozenset({"sqlalchemy", "fastapi", "httpx", "anthropic", "boto3"}),
    "provenance": frozenset({"sqlalchemy", "fastapi", "httpx", "anthropic", "boto3"}),
    "application": frozenset({"sqlalchemy", "fastapi", "httpx", "anthropic", "boto3"}),
}


def _modules() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        layer = relative.parts[0] if len(relative.parts) > 1 else relative.stem
        found.append((layer, path))
    return found


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


@pytest.mark.parametrize(("layer", "path"), _modules(), ids=lambda value: str(value)[-60:])
def test_layer_only_imports_permitted_layers(layer: str, path: Path) -> None:
    allowed = ALLOWED.get(layer)
    if allowed is None:  # a root module such as config.py or testing.py
        return
    for module in _imports(path):
        if not module.startswith("lab_assistant."):
            continue
        target = module.split(".")[1]
        if target == layer:
            continue
        assert target in allowed, (
            f"{path.relative_to(PACKAGE_ROOT)} ({layer}) imports {module}; "
            f"{layer} may import only {sorted(allowed)}"
        )


@pytest.mark.parametrize(("layer", "path"), _modules(), ids=lambda value: str(value)[-60:])
def test_layer_avoids_forbidden_external_packages(layer: str, path: Path) -> None:
    forbidden = FORBIDDEN_EXTERNAL.get(layer)
    if forbidden is None:
        return
    for module in _imports(path):
        root = module.split(".")[0]
        assert root not in forbidden, (
            f"{path.relative_to(PACKAGE_ROOT)} ({layer}) imports {module}, which this layer "
            "must not depend on"
        )


def test_domain_does_not_touch_the_clock_or_the_filesystem() -> None:
    """Time is injected, so tests are deterministic and ordering is explicit."""
    offenders: list[str] = []
    for path in sorted((PACKAGE_ROOT / "domain").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "datetime.now(" in source or "= open(" in source or "with open(" in source:
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))
    assert offenders == [], f"domain modules must not read the clock or files: {offenders}"


def test_workers_do_not_import_each_other() -> None:
    workers = Path(__file__).resolve().parents[2] / "workers"
    if not workers.is_dir():
        pytest.skip("no worker entry points in this checkout")
    for path in sorted(workers.rglob("*.py")):
        for module in _imports(path):
            assert not module.startswith("workers."), (
                f"{path.name} imports {module}; worker deployments share the application "
                "package, never each other"
            )


def test_parsing_layer_avoids_copyleft_pdf_dependency() -> None:
    """The licence position is code-enforced, not just an ADR (see ADR-0011)."""
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for module in _imports(path):
            root = module.split(".")[0]
            assert root not in ("fitz", "pymupdf"), (
                f"{path.name} imports {module}; PyMuPDF is AGPL or commercial and this "
                "project keeps a permissive dependency set"
            )
