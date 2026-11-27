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
        # systemd and cron, recorded or scripted: `schedule` must not need either.
        self.units = root / "units"
        self.units.mkdir()
        self.systemctl_calls = root / "systemctl-calls"
        self.systemctl_calls.touch()
        systemctl = bin_dir / "systemctl"
        systemctl.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SYSTEMCTL_CALLS"\n')
        systemctl.chmod(0o755)
        self.crontab = root / "crontab"
        self.crontab.touch()
        fake_crontab = bin_dir / "crontab"
        fake_crontab.write_text('#!/bin/sh\ncat "$CRONTAB_FILE"\n')
        fake_crontab.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            DOCKER_CALLS=str(self.calls),
            SYSTEMCTL_CALLS=str(self.systemctl_calls),
            CRONTAB_FILE=str(self.crontab),
            LAB_ARCHIVE_APP_UID=str(os.getuid()),
            LAB_ARCHIVE_UNIT_DIR=str(self.units),
        )
        self.env.pop("SUDO_USER", None)
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

    def unit(self, name: str) -> str:
        return (self.units / name).read_text()

    def systemctl(self) -> list[str]:
        return self.systemctl_calls.read_text().splitlines()

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
        ("policy", ARCHIVE + "LAB_INSTALLATION_ROLE=reader\n", "only the publisher publishes"),
        ("schedule", "", "LAB_ARCHIVE_STORE_KIND is not set"),
        ("schedule", ARCHIVE + "LAB_INSTALLATION_ROLE=reader\n", "LAB_ARCHIVE_LIBRARY_ID"),
        (
            "schedule",
            ARCHIVE + "LAB_ARCHIVE_POLICY_VALIDITY_HOURS=6\n",
            "would lapse between renewals",
        ),
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
    assert list(host.units.iterdir()) == []


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


def test_policy_renews_the_checkpoint_and_nothing_else(host: Host) -> None:
    host.write(".env.site", ARCHIVE)
    result = host.run("policy")
    assert result.returncode == 0, result.stderr
    assert host.lab_commands() == ["archive policy publish --workspace rlalab --apply"]


def test_schedule_on_a_publisher_renews_well_inside_the_validity_window(host: Host) -> None:
    """A daily publish alone leaves no margin on a 24-hour checkpoint."""
    host.write(".env.site", ARCHIVE)
    result = host.run("schedule", "--workspace", "rlalab", "--keep", "2")
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in host.units.iterdir()) == [
        "lab-archive-policy.service",
        "lab-archive-policy.timer",
        "lab-archive-publish.service",
        "lab-archive-publish.timer",
    ]
    # The runs are the ones asked for: same checkout, same options.
    script = f"/bin/bash {host.repo}/scripts/archive.sh"
    options = f"--workspace rlalab --keep 2 --dir {host.work} --secrets-dir {host.secrets}"
    assert f"ExecStart={script} publish {options}\n" in host.unit("lab-archive-publish.service")
    assert f"ExecStart={script} policy {options}\n" in host.unit("lab-archive-policy.service")
    assert f"WorkingDirectory={host.repo}\n" in host.unit("lab-archive-publish.service")
    publish = host.unit("lab-archive-publish.timer")
    policy = host.unit("lab-archive-policy.timer")
    assert "OnCalendar=*-*-* 02:15:00 UTC\n" in publish
    assert "OnCalendar=*-*-* 08,14,20:15:00 UTC\n" in policy
    # A run missed while the host was off happens at boot, not a day late.
    assert "Persistent=true" in publish and "Persistent=true" in policy
    assert host.systemctl() == [
        "daemon-reload",
        "enable --now lab-archive-publish.timer",
        "enable --now lab-archive-policy.timer",
    ]
    assert host.lab_commands() == []  # scheduling runs nothing

    # Again, unchanged: the units are left alone and systemd is not reloaded.
    host.systemctl_calls.write_text("")
    assert host.run("schedule", "--workspace", "rlalab", "--keep", "2").returncode == 0
    assert "daemon-reload" not in host.systemctl()


def test_schedule_on_a_reader_syncs_and_drops_a_publishers_timers(host: Host) -> None:
    host.write(".env.site", ARCHIVE)
    assert host.run("schedule").returncode == 0
    host.write(
        ".env.site", ARCHIVE + f"LAB_INSTALLATION_ROLE=reader\nLAB_ARCHIVE_LIBRARY_ID={LIBRARY}\n"
    )
    host.systemctl_calls.write_text("")
    result = host.run("schedule")
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in host.units.iterdir()) == [
        "lab-archive-sync.service",
        "lab-archive-sync.timer",
    ]
    assert "OnCalendar=*-*-* 03:15:00 UTC\n" in host.unit("lab-archive-sync.timer")
    assert "disable --now lab-archive-publish.timer" in host.systemctl()
    assert host.systemctl()[-1] == "enable --now lab-archive-sync.timer"


def test_schedule_warns_about_a_crontab_line_that_does_the_same(host: Host) -> None:
    host.write(".env.site", ARCHIVE)
    host.crontab.write_text(
        "# 15 2 * * * old\n15 2 * * * cd /opt/lab-assistant && bash scripts/archive.sh publish\n"
    )
    result = host.run("schedule")
    assert result.returncode == 0, result.stderr
    assert "crontab also runs archive.sh" in result.stderr
    host.crontab.write_text("# 15 2 * * * bash scripts/archive.sh publish\n")  # commented out
    assert "crontab" not in host.run("schedule").stderr


def test_unschedule_removes_the_timers(host: Host) -> None:
    host.write(".env.site", ARCHIVE)
    assert host.run("schedule").returncode == 0
    host.systemctl_calls.write_text("")
    host.write(".env.site", "")  # removing timers needs no working settings
    result = host.run("unschedule")
    assert result.returncode == 0, result.stderr
    assert list(host.units.iterdir()) == []
    assert host.systemctl() == [
        "disable --now lab-archive-publish.timer",
        "disable --now lab-archive-policy.timer",
        "daemon-reload",
    ]
    assert "no archive timers" in host.run("unschedule").stdout
