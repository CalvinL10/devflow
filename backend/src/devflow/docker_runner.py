from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import ClassVar

from devflow.candidate_runner import action_command, captured_text
from devflow.dependencies import normalize_requirements
from devflow.models import CommandResult
from devflow.runner_dispatch import (
    PREPARE_SECONDS,
    REQUEST_LIMIT,
    RESPONSE_LIMIT,
    RUN_SECONDS,
    STOP_SECONDS,
    DockerDispatcher,
    safe_id,
)
from devflow.workspace import ManagedWorkspace


def candidate_runner_from_environment():
    socket_path = os.environ.get("DEVFLOW_RUNNER_SOCKET")
    if socket_path:
        return SocketCandidateRunner(socket_path)
    return DockerCandidateRunner()


class SocketCandidateRunner:
    """Backend client: owns only the control socket, never the Docker socket."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    @staticmethod
    def _workspace_id(workspace: ManagedWorkspace) -> str:
        root = Path(os.path.abspath(os.environ.get(
            "DEVFLOW_WORKSPACES_ROOT", "/var/lib/devflow/workspaces"
        )))
        supplied = Path(os.path.abspath(workspace.root))
        if supplied.parent != root:
            raise ValueError("workspace must be directly under DEVFLOW_WORKSPACES_ROOT")
        for path in (supplied, *supplied.parents):
            ManagedWorkspace._reject_link(path)
        return safe_id(supplied.name)

    def _request(self, request: dict, timeout: float) -> dict:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("runner control socket requires Unix-domain socket support")
        data = json.dumps(request).encode("utf-8") + b"\n"
        if len(data) > REQUEST_LIMIT:
            raise ValueError("runner dispatcher request exceeded its limit")
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(min(5, timeout))
            client.connect(self.socket_path)
            client.sendall(data)
            response = bytearray()
            while b"\n" not in response:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("runner dispatcher response deadline exceeded")
                client.settimeout(remaining)
                chunk = client.recv(min(64 * 1024, RESPONSE_LIMIT + 1 - len(response)))
                if not chunk:
                    raise ValueError("runner dispatcher returned an incomplete response")
                response.extend(chunk)
                if len(response) > RESPONSE_LIMIT:
                    raise ValueError("runner dispatcher response exceeded its limit")
        try:
            payload = json.loads(bytes(response).split(b"\n", 1)[0])
        except (ValueError, UnicodeError) as error:
            raise ValueError("runner dispatcher returned malformed JSON") from error
        if not isinstance(payload, dict):
            raise TypeError("runner dispatcher response must be an object")
        if "error" in payload:
            raise RuntimeError(captured_text(str(payload["error"])))
        return payload

    def run(self, workspace: ManagedWorkspace, run_id: str, action: str) -> CommandResult:
        command = action_command(action)
        safe_id(run_id)
        workspace.require_materialized_candidate(run_id, workspace.candidate_path(run_id))
        started = time.monotonic()
        try:
            payload = self._request({"operation": "run", "run_id": run_id, "action": action,
                                     "workspace_id": self._workspace_id(workspace)},
                                    RUN_SECONDS + STOP_SECONDS + 10)
            if set(payload) != {"result"}:
                raise ValueError("invalid runner result response")
            report = CommandResult.model_validate(payload["result"])
            if report.command not in (command, action_command(action, dependency_env=True)):
                raise ValueError("dispatcher result does not match the requested check")
            return report
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            return CommandResult(
                passed=False, command=command, stdout="",
                stderr=captured_text(f"runner dispatcher failed: {error}"),
                duration_ms=round((time.monotonic() - started) * 1000), exit_code=None,
                timed_out=isinstance(error, TimeoutError),
            )

    def prepare(self, workspace: ManagedWorkspace, run_id: str,
                requirements: list[str]) -> dict[str, str]:
        safe_id(run_id)
        payload = self._request({"operation": "prepare", "run_id": run_id,
                                 "workspace_id": self._workspace_id(workspace),
                                 "requirements": normalize_requirements(requirements)},
                                PREPARE_SECONDS + STOP_SECONDS + 10)
        versions = payload.get("versions")
        if (set(payload) != {"versions"} or not isinstance(versions, dict)
                or len(versions) > 200 or any(not isinstance(k, str) or not isinstance(v, str)
                                            for k, v in versions.items())):
            raise ValueError("invalid dependency versions response")
        return versions

    def stop(self, run_id: str) -> bool:
        safe_id(run_id)
        payload = self._request({"operation": "stop", "run_id": run_id}, STOP_SECONDS + 10)
        if payload != {"stopped": True} or payload.get("stopped") is not True:
            raise RuntimeError("runner did not confirm resource cleanup")
        return True


class DockerCandidateRunner:
    """Explicit host-side mode using the same isolated lifecycle as the dispatcher.

    There is no host execution fallback. Deployments must use SocketCandidateRunner
    in the backend. A process shares local dispatchers so stop works across runner
    instances; each workspace's resources are reconciled on first use.
    """

    _lock = threading.Lock()
    _runs: ClassVar[dict[str, DockerDispatcher]] = {}
    _stopped: ClassVar[set[str]] = set()
    _ready: ClassVar[dict[str, threading.Event]] = {}
    _errors: ClassVar[dict[str, Exception]] = {}

    def __init__(self, image: str | None = None):
        self.image = image or os.environ.get("DEVFLOW_RUNNER_IMAGE", "devflow-candidate-runner")
        self.state_root = Path(os.environ.get(
            "DEVFLOW_LOCAL_RUNNER_STATE", str(Path.home() / ".devflow" / "runner-state"),
        )).absolute()

    def _new_dispatcher(self, candidates_root: Path | None = None) -> DockerDispatcher:
        return DockerDispatcher(candidates_root, self.image, state_root=self.state_root,
                                owner=f"local:{self.state_root}", cross_process=True)

    def _dispatcher(self, workspace: ManagedWorkspace, run_id: str) -> DockerDispatcher:
        safe_id(run_id)
        if os.name == "posix" and os.getuid() == 0:
            raise ValueError("run the host coordinator as a non-root user")
        initialize = False
        with self._lock:
            if run_id in self._stopped:
                raise RuntimeError("run has been stopped; late starts are forbidden")
            dispatcher = self._runs.get(run_id)
            if dispatcher is None:
                dispatcher = self._new_dispatcher(workspace.candidates_root)
                self._runs[run_id] = dispatcher
                self._ready[run_id] = threading.Event()
                initialize = True
            if dispatcher.candidates_root != Path(os.path.abspath(workspace.candidates_root)):
                raise ValueError("run_id is already associated with another workspace")
            ready = self._ready[run_id]
        if initialize:
            try:
                dispatcher.reconcile(run_id)
            except Exception as error:
                self._errors[run_id] = error
                raise
            finally:
                ready.set()
        elif not ready.wait(STOP_SECONDS):
            raise TimeoutError("local runner initialization deadline exceeded")
        if run_id in self._errors:
            raise RuntimeError("local runner initialization failed") from self._errors[run_id]
        if run_id in self._stopped:
            raise RuntimeError("run has been stopped; late starts are forbidden")
        return dispatcher

    def run(self, workspace: ManagedWorkspace, run_id: str, action: str) -> CommandResult:
        command = action_command(action)
        workspace.require_materialized_candidate(run_id, workspace.candidate_path(run_id))
        try:
            return self._dispatcher(workspace, run_id).run(run_id, action)
        except (OSError, RuntimeError) as error:
            return CommandResult(passed=False, command=command, stdout="",
                                 stderr=captured_text(f"runner unavailable: {error}"),
                                 duration_ms=0, exit_code=None)

    def prepare(self, workspace: ManagedWorkspace, run_id: str,
                requirements: list[str]) -> dict[str, str]:
        requirements = normalize_requirements(requirements)
        return self._dispatcher(workspace, run_id).prepare(run_id, requirements)

    def stop(self, run_id: str) -> bool:
        safe_id(run_id)
        with self._lock:
            self._stopped.add(run_id)
        # Fresh dispatcher intentionally ignores process-local _runs. The parent
        # may be stopping resources created only in a multiprocessing spawn child.
        return self._new_dispatcher().stop(run_id)
