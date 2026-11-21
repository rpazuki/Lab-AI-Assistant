"""`scripts/archive.sh` and the role-reading helpers, against a fake `docker`.

The script is what an operator runs on a timer, so what matters is the order of
the `lab` commands it issues and the cases it refuses before issuing any: a
publisher that tries to sync, a reader that tries to publish, a credential file
that would not be mounted. A fake `docker` records every invocation, which is
enough to see both without a container.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = "c5821c38-97b9-4e25-95d9-1f457c178482"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


class Host:
    """A checkout with this host's settings files and a recording `docker`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        shutil.copytree(ROOT / "scripts", self.repo / "scripts")
        self.work = root / "work"
        self.work.mkdir()
        self.secrets = root / "secrets"
        self.secrets.mkdir()
        self.calls = root / "calls"
        self.calls.touch()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$DOCKER_CALLS"\n'
            'case "$*" in *"exec -T db"*) echo ' + LIBRARY + " ;; esac\n"
        )
        docker.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            DOCKER_CALLS=str(self.calls),
            LAB_ARCHIVE_APP_UID=str(os.getuid()),
        )
        (self.repo / ".env.server").write_text("LAB_ENV=server\n")
        self.write(".env", "POSTGRES_PASSWORD=x\n")

    def write(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                str(self.repo / "scripts/archive.sh"),
                *args,
                "--dir",
                str(self.work),
                "--secrets-dir",
                str(self.secrets),
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def lab_commands(self) -> list[str]:
        return [
            line.split(" api lab ", 1)[1]
            for line in self.calls.read_text().splitlines()
            if " api lab " in line
        ]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


ARCHIVE = "LAB_ARCHIVE_STORE_KIND=gcs\nLAB_ARCHIVE_STORE_BUCKET=shared\n"


def test_publish_issues_policy_then_packages_then_publishes(host: Host) -> None:
    host.write(".env.site", ARCHIVE)
    result = host.run("publish")
    assert result.returncode == 0, result.stderr
    commands = host.lab_commands()
    assert [command.split(" /")[0] for command in commands] == [
        "archive policy publish --workspace rlalab --apply",
        "archive package --workspace rlalab",
        "archive publish --workspace rlalab",
    ]
    # The package and the publish name the same new directory, and only the
    # publish moves the head.
    packaged = commands[1].rsplit(" ", 1)[1]
    assert packaged.startswith(f"{host.work}/snap-")
    assert commands[2] == f"archive publish --workspace rlalab {packaged} --apply"
    # Snapshot and secrets directories are mounted, secrets read-only, and
    # every command runs in a one-off container against the stack's project.
    run_line = next(line for line in host.calls.read_text().splitlines() if " api lab " in line)
    assert "-p lab-assistant" in run_line
    assert "run --rm --no-deps -T" in run_line
    assert f"-v {host.work}:{host.work}" in run_line
    assert f"-v {host.secrets}:{host.secrets}:ro" in run_line
    assert f"LAB_ARCHIVE_LIBRARY_ID={LIBRARY}" in result.stdout


def test_sync_fetches_then_imports(host: Host) -> None:
    host.write(
        ".env.site", ARCHIVE + f"LAB_INSTALLATION_ROLE=reader\nLAB_ARCHIVE_LIBRARY_ID={LIBRARY}\n"
    )
    result = host.run("sync", "--group-map", "a=b")
    assert result.returncode == 0, result.stderr
    fetch, imported = host.lab_commands()
    directory = fetch.split(" ")[2]
    assert directory.startswith(f"{host.work}/fetch-")
    assert fetch == f"archive fetch {directory} --library-id {LIBRARY}"
    assert imported == (
        f"archive import {directory} --workspace rlalab --library-id {LIBRARY} --group-map a=b"
    )
    # A reader runs with the reader overlay, or the one-off container would
    # wait for a scanner that a reader never starts.
    assert "docker-compose.reader.yml" in host.calls.read_text()


@pytest.mark.parametrize(
    ("command", "site", "refusal"),
    [
        ("sync", ARCHIVE, "sync is for a reader"),
        ("publish", ARCHIVE + "LAB_INSTALLATION_ROLE=reader\n", "only the publisher publishes"),
        ("publish", "", "LAB_ARCHIVE_STORE_KIND is not set"),
        ("sync", ARCHIVE + "LAB_INSTALLATION_ROLE=reader\n", "LAB_ARCHIVE_LIBRARY_ID"),
        (
            "publish",
            ARCHIVE + "LAB_ARCHIVE_STORE_CREDENTIALS_PATH=/root/gcs.json\n",
            "must be a file under",
        ),
    ],
)
def test_refusals_happen_before_any_container_starts(
    host: Host, command: str, site: str, refusal: str
) -> None:
    host.write(".env.site", site)
    result = host.run(command)
    assert result.returncode != 0
    assert refusal in result.stderr
    assert host.lab_commands() == []


def test_a_credential_file_that_does_not_exist_is_refused(host: Host) -> None:
    missing = host.secrets / "gcs-publisher.json"
    host.write(".env.site", ARCHIVE + f"LAB_ARCHIVE_STORE_CREDENTIALS_PATH={missing}\n")
    result = host.run("publish")
    assert result.returncode != 0
    assert "does not exist on this host" in result.stderr
    missing.write_text("{}")
    assert host.run("publish").returncode == 0


def test_only_the_newest_snapshots_are_kept(host: Host) -> None:
    host.write(".env.site", ARCHIVE)
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "20260104T000000Z"):
        (host.work / f"snap-{stamp}").mkdir()
    (host.work / "fetch-20250101T000000Z").mkdir()  # another kind: not this command's
    assert host.run("publish", "--keep", "3").returncode == 0
    removals = [line for line in host.calls.read_text().splitlines() if "--entrypoint rm" in line]
    assert len(removals) == 1
    assert removals[0].endswith(f"-rf -- {host.work}/snap-20260101T000000Z")


def test_the_role_is_read_last_file_wins(tmp_path: Path) -> None:
    """`.env.site` outranks `.env.server`, which outranks `.env`, as for Compose."""
    host = Host(tmp_path)
    host.write(".env", "LAB_INSTALLATION_ROLE=publisher\n")
    host.write(".env.site", "LAB_INSTALLATION_ROLE='reader'\n")
    script = (
        'source "$1"; printf "%s|%s|%s" "$INSTALLATION_ROLE" "${APP_SERVICES[*]}" '
        '"$(site_value LAB_ENV)"'
    )
    shown = subprocess.run(
        ["bash", "-c", script, "x", str(host.repo / "scripts/lib/common.sh")],
        env=host.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert shown == "reader|api web|server"

    deploy = shutil.copy(ROOT / "scripts/deploy/update.sh", host.repo / "update.sh")
    shown = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; repo="$2"; setting LAB_INSTALLATION_ROLE',
            "x",
            str(deploy),
            str(host.repo),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert shown == "reader"
