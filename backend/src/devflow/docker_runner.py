from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid

from devflow.candidate_runner import MAX_CAPTURE_CHARS, action_command, captured_text
from devflow.models import CommandResult
from devflow.workspace import ManagedWorkspace


def candidate_runner_from_environment():
    socket_path = os.environ.get("DEVFLOW_RUNNER_SOCKET")
    if socket_path:
        return SocketCandidateRunner(socket_path)
    return DockerCandidateRunner()


class SocketCandidateRunner:
    """Client for the Compose-only, Docker-socket-owning runner dispatcher."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def run(self, workspace: ManagedWorkspace, run_id: str, action: str) -> CommandResult:
        command = action_command(action)
        workspace.require_materialized_candidate(run_id, workspace.candidate_path(run_id))
        started = time.monotonic()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(100)
                client.connect(self.socket_path)
                request = json.dumps({"run_id": run_id, "action": action}).encode("utf-8") + b"\n"
                client.sendall(request)
                response = bytearray()
                while b"\n" not in response:
                    chunk = client.recv(64 * 1024)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if len(response) > MAX_CAPTURE_CHARS * 3:
                        raise ValueError("runner dispatcher response exceeded its limit")
        except (OSError, TimeoutError) as error:
            return CommandResult(
                passed=False, command=command, stdout="",
                stderr=f"runner dispatcher unavailable: {error}",
                duration_ms=round((time.monotonic() - started) * 1000), exit_code=None,
            )
        payload = json.loads(bytes(response).split(b"\n", 1)[0])
        if "error" in payload:
            return CommandResult(
                passed=False, command=command, stdout="", stderr=payload["error"],
                duration_ms=round((time.monotonic() - started) * 1000), exit_code=None,
            )
        report = CommandResult.model_validate(payload["result"])
        if report.command != command:
            raise ValueError("dispatcher result does not match the requested check")
        return report


class DockerCandidateRunner:
    """Host-side launcher. Never executes candidate code in the backend process."""

    def __init__(self, image: str | None = None):
        self.image = image or os.environ.get("DEVFLOW_RUNNER_IMAGE", "devflow-candidate-runner")

    def run(self, workspace: ManagedWorkspace, run_id: str, action: str) -> CommandResult:
        command = action_command(action)
        candidate = workspace.require_materialized_candidate(
            run_id, workspace.candidate_path(run_id)
        )
        if "," in str(candidate):
            raise ValueError("Docker mount paths must not contain commas")
        name = f"devflow-check-{uuid.uuid4().hex}"
        user = "10001:10001"
        if os.name == "posix":
            if os.getuid() == 0:
                raise ValueError("run the host coordinator as a non-root user")
            # Preserve 0700 candidate access without granting root or broadening permissions.
            user = f"{os.getuid()}:{os.getgid()}"
        args = [
            "docker", "run", "--rm", "--pull=never", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--pids-limit=64",
            "--memory=512m", "--cpus=1", f"--user={user}",
            "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--mount", f"type=bind,source={candidate},target=/candidate,readonly",
            "--workdir=/candidate", "--env=DEVFLOW_RUNNER_TIMEOUT_SECONDS=60",
            "--entrypoint=/app/.venv/bin/python", self.image,
            "-I", "-m", "devflow.candidate_runner", action,
        ]
        started = time.monotonic()
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                shell=False, timeout=90, check=False,
            )
        except subprocess.TimeoutExpired as error:
            cleanup_error = self._remove_timed_out_container(name)
            stderr_parts = [part for part in (captured_text(error.stderr),) if part]
            stderr_parts.append("container execution timed out")
            if cleanup_error:
                stderr_parts.append(f"container cleanup failed: {cleanup_error}")
            return CommandResult(
                passed=False,
                command=command,
                stdout=captured_text(error.stdout),
                stderr=captured_text("\n".join(stderr_parts)),
                duration_ms=round((time.monotonic() - started) * 1000),
                exit_code=None, timed_out=True,
            )
        except OSError as error:
            return CommandResult(
                passed=False, command=command, stdout="", stderr=f"runner unavailable: {error}",
                duration_ms=round((time.monotonic() - started) * 1000), exit_code=None,
            )
        if result.returncode not in (0, 1):
            return CommandResult(
                passed=False, command=command, stdout=result.stdout[-MAX_CAPTURE_CHARS:],
                stderr=result.stderr[-MAX_CAPTURE_CHARS:],
                duration_ms=round((time.monotonic() - started) * 1000),
                exit_code=result.returncode,
            )
        report = CommandResult.model_validate_json(result.stdout.strip())
        if report.command != command or report.passed != (result.returncode == 0):
            raise ValueError("container result does not match the requested check")
        return report

    @staticmethod
    def _remove_timed_out_container(name: str) -> str | None:
        # subprocess timeout kills the Docker client, not necessarily its container.
        try:
            completed = subprocess.run(
                ["docker", "rm", "--force", name], capture_output=True,
                shell=False, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return str(error)
        if completed.returncode != 0:
            return captured_text(completed.stderr) or (
                f"docker rm exited with code {completed.returncode}"
            )
        return None
