#!/usr/bin/env python3
"""Documentation CI.

Checks, in the order they tend to fail:

1. internal links resolve;
2. non-evergreen pages carry a status header (status, owner, last verified);
3. every page is reachable from the documentation index — an orphan page is a page
   nobody maintains;
4. ADR numbering is unique, statuses are valid, and a superseded ADR names its
   successor;
5. every fenced ``bash`` command in the README quick start exists as a runnable
   target (the command's first word resolves to a file, a script, or a known tool);
6. no page contains something that looks like a credential.

Standard library only, so it runs anywhere without installing anything.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

#: Pages that are evergreen and need no status header.
EVERGREEN = {"docs/README.md"}

VALID_STATUSES = {
    "proposed",
    "active",
    "accepted",
    "deprecated",
    "historical",
    "superseded",
    "rejected",
}

LINK = re.compile(r"\[[^\]]+\]\(([^)#\s]+)(#[^)]*)?\)")
STATUS = re.compile(r"^Status:\s*(\w+)", re.MULTILINE)
OWNER = re.compile(r"^Owner:\s*\S+", re.MULTILINE)
VERIFIED = re.compile(r"^Last verified:\s*\d{4}-\d{2}-\d{2}", re.MULTILINE)
SECRETISH = re.compile(
    r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{16,}", re.IGNORECASE
)
#: Placeholders that are obviously not credentials.
SECRET_ALLOWED = re.compile(
    r"(change-me|development-only|<[^>]+>|redacted|example|xxx+)", re.IGNORECASE
)


def markdown_files() -> list[Path]:
    files = sorted(DOCS.rglob("*.md"))
    for name in ("README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md", "CLAUDE.md"):
        candidate = ROOT / name
        if candidate.exists():
            files.append(candidate)
    return files


def check_links(problems: list[str]) -> None:
    for path in markdown_files():
        for match in LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                problems.append(f"{_rel(path)}: broken link to {target}")


def check_status_headers(problems: list[str]) -> None:
    for path in markdown_files():
        relative = _rel(path)
        exempt = ("README.md", "AGENTS.md", "SECURITY.md", "CONTRIBUTING.md")
        if relative in EVERGREEN or relative.startswith(exempt):
            continue
        text = path.read_text(encoding="utf-8")
        status = STATUS.search(text)
        if not status:
            problems.append(f"{relative}: missing 'Status:' header")
        elif status.group(1).lower() not in VALID_STATUSES:
            problems.append(f"{relative}: unknown status {status.group(1)!r}")
        if not OWNER.search(text):
            problems.append(f"{relative}: missing 'Owner:' header")
        if not VERIFIED.search(text):
            problems.append(f"{relative}: missing 'Last verified: YYYY-MM-DD' header")


def check_orphans(problems: list[str]) -> None:
    index = DOCS / "README.md"
    if not index.exists():
        problems.append("docs/README.md is missing")
        return
    linked: set[Path] = set()
    frontier = [index]
    seen: set[Path] = set()
    while frontier:
        current = frontier.pop()
        if current in seen or not current.exists():
            continue
        seen.add(current)
        linked.add(current)
        for match in LINK.finditer(current.read_text(encoding="utf-8")):
            target = match.group(1)
            if target.startswith(("http", "mailto:")):
                continue
            resolved = (current.parent / target).resolve()
            if resolved.suffix == ".md":
                frontier.append(resolved)
    for path in sorted(DOCS.rglob("*.md")):
        if path.resolve() not in {p.resolve() for p in linked}:
            problems.append(f"{_rel(path)}: not reachable from docs/README.md")


def check_adrs(problems: list[str]) -> None:
    directory = DOCS / "architecture" / "adr"
    if not directory.is_dir():
        problems.append("docs/architecture/adr is missing")
        return
    numbers: dict[str, Path] = {}
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":  # the index, not an ADR
            continue
        match = re.match(r"^(\d{4})-", path.name)
        if not match:
            problems.append(f"{_rel(path)}: ADR filename must start with NNNN-")
            continue
        number = match.group(1)
        if number in numbers:
            problems.append(f"{_rel(path)}: duplicate ADR number {number}")
        numbers[number] = path
        text = path.read_text(encoding="utf-8")
        status = STATUS.search(text)
        if not status:
            problems.append(f"{_rel(path)}: ADR has no status")
            continue
        value = status.group(1).lower()
        if value not in VALID_STATUSES:
            problems.append(f"{_rel(path)}: ADR status {value!r} is not recognised")
        if value == "superseded" and "Superseded by" not in text:
            problems.append(f"{_rel(path)}: superseded ADR must name its successor")
        for heading in ("## Context", "## Decision", "## Consequences"):
            if heading not in text:
                problems.append(f"{_rel(path)}: ADR is missing {heading}")


def check_readme_commands(problems: list[str]) -> None:
    readme = ROOT / "README.md"
    if not readme.exists():
        problems.append("README.md is missing")
        return
    known = {
        "python3.12",
        "python",
        "pip",
        "pytest",
        "ruff",
        "uvicorn",
        "npm",
        "npx",
        "docker",
        "lab",
        "alembic",
        "cp",
        "cd",
        "set",
        "source",
        "psql",
        "make",
        # The deployed server's own tools, for the operations section.
        "systemctl",
        "journalctl",
    }
    for block in re.findall(r"```bash\n(.*?)```", readme.read_text(encoding="utf-8"), re.DOTALL):
        for line in block.splitlines():
            command = line.strip()
            if not command or command.startswith("#"):
                continue
            if command.startswith("$"):
                problems.append(f"README.md: drop the '$' prompt from '{command}'")
                continue
            words = command.split()
            # `sudo` changes who runs a command, not which: judge what it runs.
            if words[0] == "sudo":
                words = words[1:] or ["sudo"]
            head = words[0]
            if head == "bash" and len(words) > 1 and not words[1].startswith("-"):
                # `bash scripts/x.sh` runs a repository script: it must be there.
                if not (ROOT / words[1]).is_file():
                    problems.append(f"README.md: '{words[1]}' does not exist")
            elif head.startswith("./"):
                # A repository-relative script must actually be there.
                if not (ROOT / head[2:]).exists():
                    problems.append(f"README.md: '{head}' does not exist")
            elif head.rsplit("/", 1)[-1] not in known:
                # A tool invoked through an interpreter path (.venv/bin/pytest) is
                # judged by its basename: the virtualenv is not in the repository.
                problems.append(f"README.md: unknown command '{head}' in the quick start")


def check_no_secrets(problems: list[str]) -> None:
    for path in markdown_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            if SECRETISH.search(line) and not SECRET_ALLOWED.search(line):
                problems.append(f"{_rel(path)}: line looks like a credential: {line.strip()[:60]}")


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:  # pragma: no cover - outside the repository
        return str(path)


def main() -> int:
    problems: list[str] = []
    check_links(problems)
    check_status_headers(problems)
    check_orphans(problems)
    check_adrs(problems)
    check_readme_commands(problems)
    check_no_secrets(problems)
    if problems:
        print("documentation checks failed:", file=sys.stderr)
        for problem in sorted(problems):
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("documentation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
