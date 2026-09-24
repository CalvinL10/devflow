"""Launcher port validation without a real daemon or source repository."""

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher runs on Linux/WSL")
@pytest.mark.parametrize("port", ["0", "1023", "65536", "03001", "abc", "3001:3000"])
def test_posix_launcher_rejects_invalid_port_before_docker(port: str) -> None:
    script = ROOT / "scripts/devflow.sh"
    if not script.exists():
        pytest.skip("launcher not included in runner image")
    result = subprocess.run(
        ["bash", str(script), "start", "--demo", "--port", port],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "--port must be an integer" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher runs on Linux/WSL")
def test_posix_launcher_passes_selected_port_to_compose(tmp_path: Path) -> None:
    script = ROOT / "scripts/devflow.sh"
    if not script.exists():
        pytest.skip("launcher not included in runner image")
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s %s\\n" "$DEVFLOW_PORT" "$*" >> "$CALLS"\n'
        'case "$*" in\n'
        '  "compose version --short") echo 2.24.4 ;;\n'
        '  "info --format {{.OSType}}") echo linux ;;\n'
        'esac\n', encoding="utf-8",
    )
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = str(listener.getsockname()[1])
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}", CALLS=str(calls))
    result = subprocess.run(
        ["bash", str(script), "start", "--demo", "--port", port],
        env=env, check=False, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert f"Open http://127.0.0.1:{port}" in result.stdout
    lines = calls.read_text().splitlines()
    assert lines and all(line.startswith(f"{port} ") for line in lines)
    assert any("up --build --detach --wait" in line for line in lines)


@pytest.mark.parametrize("port", ["0", "1023", "65536", "abc", "3001:3000"])
def test_powershell_launcher_rejects_invalid_port(port: str) -> None:
    pwsh = shutil.which("pwsh")
    script = ROOT / "scripts/devflow.ps1"
    if not pwsh or not script.exists():
        pytest.skip("PowerShell/launcher not available")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(script), "start", "-Demo", "-Port", port],
        check=False, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "Port" in result.stderr
    assert "Docker command failed" not in result.stderr
