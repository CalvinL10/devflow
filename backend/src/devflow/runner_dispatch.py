from __future__ import annotations

import json
import os
import socketserver
import subprocess
import uuid
from pathlib import Path

from devflow.candidate_runner import MAX_CAPTURE_CHARS, action_command, captured_text
from devflow.models import CommandResult
from devflow.workspace import RUN_ID_PATTERN, ManagedWorkspace


class DockerDispatcher:
    """Trusted local dispatcher that alone owns access to the Docker daemon."""

    def __init__(self, candidates_root: Path, image: str):
        # The backend creates this directory after the dispatcher becomes healthy.
        self.candidates_root = Path(os.path.abspath(candidates_root))
        self.image = image

    def run(self, run_id: str, action: str) -> CommandResult:
        command = action_command(action)
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ValueError("run_id is not safe for a candidate directory")
        candidates_root = self.candidates_root.resolve(strict=True)
        candidate = candidates_root / run_id
        ManagedWorkspace._reject_links(candidate)
        if not candidate.resolve(strict=True).is_relative_to(candidates_root):
            raise ValueError("candidate path escapes the managed candidate root")
        if not candidate.is_dir():
            raise ValueError("candidate is not a directory")

        token = uuid.uuid4().hex
        volume = f"devflow-candidate-{token}"
        staging = f"devflow-stage-{token}"
        execution = f"devflow-check-{token}"
        created_volume = False
        try:
            self._checked(["docker", "volume", "create", volume], timeout=30)
            created_volume = True
            self._checked([
                "docker", "create", "--pull=never", "--name", staging,
                "--mount", f"type=volume,source={volume},target=/candidate",
                "--entrypoint=/bin/true", self.image,
            ], timeout=30)
            self._checked(["docker", "cp", f"{candidate}{os.sep}.", f"{staging}:/candidate"], timeout=30)
            self._checked(["docker", "rm", staging], timeout=30)
            result = subprocess.run([
                "docker", "run", "--rm", "--pull=never", "--name", execution,
                "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true", "--pids-limit=64",
                "--memory=512m", "--cpus=1", "--user=10001:10001",
                "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--mount", f"type=volume,source={volume},target=/candidate,readonly",
                "--workdir=/candidate", "--env=DEVFLOW_RUNNER_TIMEOUT_SECONDS=60",
                "--entrypoint=/app/.venv/bin/python", self.image,
                "-I", "-m", "devflow.candidate_runner", action,
            ], capture_output=True, text=True, encoding="utf-8", errors="replace",
                shell=False, timeout=90, check=False)
        except subprocess.TimeoutExpired as error:
            self._best_effort(["docker", "rm", "--force", execution])
            return CommandResult(
                passed=False, command=command, stdout=captured_text(error.stdout),
                stderr=captured_text(f"{captured_text(error.stderr)}\ncontainer execution timed out"),
                duration_ms=90_000, exit_code=None, timed_out=True,
            )
        finally:
            self._best_effort(["docker", "rm", "--force", staging])
            if created_volume:
                self._best_effort(["docker", "volume", "rm", "--force", volume])

        if result.returncode not in (0, 1):
            return CommandResult(
                passed=False, command=command, stdout=result.stdout[-MAX_CAPTURE_CHARS:],
                stderr=result.stderr[-MAX_CAPTURE_CHARS:], duration_ms=0,
                exit_code=result.returncode,
            )
        report = CommandResult.model_validate_json(result.stdout.strip())
        if report.command != command or report.passed != (result.returncode == 0):
            raise ValueError("container result does not match the requested check")
        return report

    @staticmethod
    def _checked(args: list[str], *, timeout: int) -> None:
        completed = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False, timeout=timeout, check=False,
        )
        if completed.returncode:
            raise RuntimeError(captured_text(completed.stderr) or f"Docker exited {completed.returncode}")

    @staticmethod
    def _best_effort(args: list[str]) -> None:
        try:
            subprocess.run(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=False, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(4097)
            if not raw.endswith(b"\n") or len(raw) > 4096:
                raise ValueError("invalid dispatcher request")
            request = json.loads(raw)
            report = self.server.dispatcher.run(request["run_id"], request["action"])
            response = {"result": report.model_dump(mode="json")}
        except Exception as error:  # noqa: BLE001 - protocol boundary returns traceable failure
            response = {"error": f"runner dispatcher failed: {type(error).__name__}: {error}"}
        try:
            self.wfile.write(json.dumps(response, sort_keys=True).encode("utf-8") + b"\n")
        except BrokenPipeError:
            pass


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class _Server(_UnixStreamServer):
    def __init__(self, socket_path: str, dispatcher: DockerDispatcher):
        self.dispatcher = dispatcher
        super().__init__(socket_path, _Handler)


def main() -> None:
    if not hasattr(socketserver, "UnixStreamServer"):
        raise SystemExit("runner dispatcher requires a POSIX Unix-domain socket")
    socket_path = os.environ.get("DEVFLOW_RUNNER_SOCKET", "/run/devflow/runner.sock")
    candidate_root = Path(os.environ.get(
        "DEVFLOW_CANDIDATES_ROOT", "/var/lib/devflow/workspaces/default/candidates"
    ))
    image = os.environ.get("DEVFLOW_RUNNER_IMAGE", "devflow-candidate-runner:compose")
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    previous_umask = os.umask(0)
    try:
        server = _Server(socket_path, DockerDispatcher(candidate_root, image))
    finally:
        os.umask(previous_umask)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
