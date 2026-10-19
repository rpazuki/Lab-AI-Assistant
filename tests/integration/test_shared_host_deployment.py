"""Validate the merged deployment rather than assuming empty lists remove ports."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_shared_host_compose_merge(tmp_path: Path) -> None:
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is required for the merged configuration check")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if version.returncode:
        pytest.skip("Docker Compose plugin unavailable")
    for name in (
        "docker-compose.yml",
        ".env.compose",
        ".env.server",
        "infra/compose/docker-compose.prod.yml",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=example-only\nLAB_SECRET_KEY=example-only\n")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "infra/compose/docker-compose.prod.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    services = json.loads(result.stdout)["services"]
    assert services["api"]["environment"]["LAB_ENV"] == "server"
    assert services["api"]["environment"]["LAB_APP_PUBLIC_URL"].endswith(":8445/assist")
    assert services["api"]["environment"]["LAB_OBJECT_STORE_KIND"] == "filesystem"
    assert services["api"]["environment"]["LAB_ALLOW_FILESYSTEM_IN_PRODUCTION"] == "true"
    for name in ("api", "worker-network", "worker-compute"):
        assert any(
            m["source"] == "objects" and m["target"] == "/var/lib/lab-assistant/objects"
            for m in services[name]["volumes"]
        )
    assert not services["db"].get("ports")
    assert not services["scanner"].get("ports")
    for name, port in [("api", "8100"), ("web", "3300")]:
        assert len(services[name]["ports"]) == 1
        binding = services[name]["ports"][0]
        assert binding["host_ip"] == "127.0.0.1"
        assert binding["published"] == port
    assert services["worker-compute"].get("network_mode") != "none"
    # Each image entry point already chooses its stage class. Repeating it in
    # command would invoke `lab worker network network` and immediately exit.
    assert not services["worker-network"].get("command")
    assert not services["worker-compute"].get("command")
    for name in ("api", "worker-compute"):
        mounts = services[name]["volumes"]
        assert any(m["target"] == "/run/clamav" and m["read_only"] for m in mounts)
        assert services[name]["depends_on"]["scanner"]["condition"] == "service_healthy"


def test_operator_health_checks_use_own_container(tmp_path: Path) -> None:
    """A healthy application on host port 8000 must not mask this app's failure."""
    import os

    binary = tmp_path / "docker"
    binary.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CHECK_CALLS"\nprintf \'{"status":"ok"}\\n\'\n'
    )
    binary.chmod(0o755)
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CHECK_CALLS=str(calls))
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; health_check 1; readiness_check',
            "check",
            str(ROOT / "scripts/lib/common.sh"),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    requests = calls.read_text().splitlines()
    assert len(requests) == 2
    assert all("exec -T api python -c" in request for request in requests)
    assert "/health" in requests[0]
    assert "/ready" in requests[1]
