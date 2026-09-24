from __future__ import annotations

import json
import os
import socketserver
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from devflow.candidate_runner import _run_bounded, action_command, captured_text
from devflow.dependencies import normalize_requirements
from devflow.models import CommandResult
from devflow.workspace import RUN_ID_PATTERN, ManagedWorkspace

CLI_SECONDS = 8
STOP_SECONDS = 30
RUN_SECONDS = 120
PREPARE_SECONDS = 300
REQUEST_LIMIT = 80 * 1024
RESPONSE_LIMIT = 1024 * 1024
MANAGED_LABEL = "devflow.runner=beta"


def safe_id(value: str) -> str:
    if not isinstance(value, str) or RUN_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("run_id/workspace_id must be a safe identifier")
    return value


def _cli(args: list[str], *, timeout: float = CLI_SECONDS) -> subprocess.CompletedProcess:
    code, out, err, timed_out, limited = _run_bounded(
        args, cwd=Path.cwd(), env=dict(os.environ), timeout_seconds=timeout,
    )
    if timed_out:
        raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err)
    if limited:
        raise RuntimeError("Docker command output exceeded its limit")
    return subprocess.CompletedProcess(args, code, out.decode("utf-8", "replace"),
                                       err.decode("utf-8", "replace"))


def _checked(args: list[str], deadline: float) -> str:
    remaining = min(CLI_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("runner operation deadline exceeded")
    result = _cli(args, timeout=remaining)
    if result.returncode:
        raise RuntimeError(captured_text(result.stderr) or f"Docker exited {result.returncode}")
    return result.stdout


@dataclass
class _Run:
    workspace_id: str
    bound: bool = True
    stop_file: Path | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    guard: threading.Lock = field(default_factory=threading.Lock)
    operation: threading.Lock = field(default_factory=threading.Lock)
    containers: set[str] = field(default_factory=set)
    volumes: set[str] = field(default_factory=set)
    dependency_volume: str | None = None
    requirements: list[str] | None = None
    versions: dict[str, str] | None = None


class DockerDispatcher:
    """One trusted daemon owner. No caller-controlled paths or Docker commands.

    candidates_root is the optional legacy/default workspace root. New callers
    supply workspace_id and resolve under workspaces_root/<id>/candidates/<run>.
    Only one dispatcher may own a given owner label/state directory at a time.
    """

    def __init__(self, candidates_root: Path | None = None,
                 image: str = "devflow-candidate-runner", *,
                 workspaces_root: Path | None = None, state_root: Path | None = None,
                 owner: str | None = None, cross_process: bool = False):
        self.candidates_root = (Path(os.path.abspath(candidates_root))
                                if candidates_root is not None else None)
        self.workspaces_root = Path(os.path.abspath(workspaces_root or os.environ.get(
            "DEVFLOW_WORKSPACES_ROOT", "/var/lib/devflow/workspaces"
        )))
        self.image = image
        self.owner = owner or str(self.candidates_root or self.workspaces_root)
        self.state_root = state_root
        self.cross_process = cross_process
        if cross_process and state_root is None:
            raise ValueError("cross-process lifecycle requires a persistent state directory")
        if state_root is not None:
            for path in (state_root, *state_root.parents):
                ManagedWorkspace._reject_link(path)
            state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if cross_process:
                ManagedWorkspace._reject_link(state_root / ".locks")
                (state_root / ".locks").mkdir(exist_ok=True, mode=0o700)
        self._states: dict[str, _Run] = {}
        self._registry_lock = threading.Lock()

    def _state(self, run_id: str, workspace_id: str | None = None) -> _Run:
        safe_id(run_id)
        if workspace_id is not None:
            safe_id(workspace_id)
        with self._registry_lock:
            state = self._states.get(run_id)
            if state is None:
                state = self._states[run_id] = _Run(
                    workspace_id or "default", bound=workspace_id is not None,
                    stop_file=self.state_root / run_id if self.state_root is not None else None,
                )
                if self.state_root is not None and (self.state_root / run_id).exists():
                    state.cancelled.set()
            elif workspace_id is not None:
                if state.bound and state.workspace_id != workspace_id:
                    raise ValueError("run_id is already associated with another workspace")
                state.workspace_id, state.bound = workspace_id, True
            return state

    def _cancel(self, run_id: str, state: _Run) -> None:
        state.cancelled.set()  # Set BEFORE waiting for the create/start lock.
        if self.state_root is not None:
            path = self.state_root / run_id
            ManagedWorkspace._reject_link(path)
            with path.open("a", encoding="utf-8"):
                pass

    @contextmanager
    def _locked(self, state: _Run, deadline: float):
        if not state.guard.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("runner resource lock deadline exceeded")
        try:
            if self.cross_process:
                with self._process_lock(state.stop_file.name, deadline):
                    yield
            else:
                yield
        finally:
            state.guard.release()

    @contextmanager
    def _process_lock(self, run_id: str, deadline: float):
        """OS lock shared by supervisor/worker; never held across workload waits."""
        path = self.state_root / ".locks" / run_id
        ManagedWorkspace._reject_link(path)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "r+b", buffering=0) as stream:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
            while True:
                try:
                    if os.name == "posix":
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("runner process lock deadline exceeded") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                if os.name == "posix":
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                else:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _active(state: _Run) -> None:
        if state.stop_file is not None and state.stop_file.exists():
            state.cancelled.set()
        if state.cancelled.is_set():
            raise RuntimeError("run has been stopped; late starts are forbidden")

    def _candidate(self, run_id: str, workspace_id: str) -> Path:
        safe_id(run_id)
        safe_id(workspace_id)
        root = (self.candidates_root if workspace_id == "default" and self.candidates_root
                else self.workspaces_root / workspace_id / "candidates")
        candidate = root / run_id
        for path in (candidate, *candidate.parents):
            ManagedWorkspace._reject_link(path)
        ManagedWorkspace._reject_links(candidate)
        if not candidate.is_dir() or not candidate.resolve(strict=True).is_relative_to(
            root.resolve(strict=True)
        ):
            raise ValueError("candidate is not a managed directory")
        return candidate

    def _labels(self, run_id: str, state: _Run) -> list[str]:
        return ["--label", MANAGED_LABEL, "--label", f"devflow.owner={self.owner}",
                "--label", f"devflow.run={run_id}",
                "--label", f"devflow.workspace={state.workspace_id}"]

    def _volume(self, run_id: str, state: _Run, deadline: float) -> str:
        name = f"devflow-data-{uuid.uuid4().hex}"
        with self._locked(state, deadline):
            self._active(state)
            state.volumes.add(name)  # Keep uncertain creates registered for cleanup.
            _checked(["docker", "volume", "create", *self._labels(run_id, state), name], deadline)
        return name

    def _create(self, run_id: str, state: _Run, deadline: float, *,
                mounts: list[str], arguments: list[str], network: bool = False,
                root: bool = False, prepared: bool = False) -> str:
        name = f"devflow-job-{uuid.uuid4().hex}"
        args = ["docker", "create", "--pull=never", "--name", name,
                *self._labels(run_id, state), "--network=bridge" if network else "--network=none",
                "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                "--pids-limit=64", "--memory=512m", "--cpus=1",
                "--user=0:0" if root else "--user=10001:10001",
                "--log-driver=local", "--log-opt=max-size=256k", "--log-opt=max-file=1",
                "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=384m" if network
                else "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--env=HOME=/tmp", "--env=DEVFLOW_RUNNER_TIMEOUT_SECONDS=60"]
        if root:
            # Fixed offline ownership helper only, never candidate code.
            args.extend(["--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER"])
        if prepared:
            args.append("--env=DEVFLOW_DEPENDENCY_ENV=1")
        for mount in mounts:
            args.extend(["--mount", mount])
        args.extend(["--workdir=/tmp", "--entrypoint=/app/.venv/bin/python", self.image,
                     "-I", *arguments])
        with self._locked(state, deadline):
            self._active(state)
            state.containers.add(name)
            _checked(args, deadline)
        return name

    def _start(self, state: _Run, name: str, deadline: float) -> None:
        # Detached start has a short deadline. Never hold this lock while the
        # workload runs: stop must be able to kill a downloader or test promptly.
        with self._locked(state, deadline):
            self._active(state)
            _checked(["docker", "start", name], deadline)

    def _wait(self, state: _Run, name: str, deadline: float) -> subprocess.CompletedProcess:
        while True:
            self._active(state)
            status = json.loads(_checked([
                "docker", "inspect", "--format={{json .State}}", name,
            ], deadline))
            if not isinstance(status, dict) or type(status.get("Running")) is not bool:
                raise ValueError("invalid Docker container state")
            if not status["Running"]:
                if status.get("Status") != "exited" or type(status.get("ExitCode")) is not int:
                    raise RuntimeError("container did not exit normally")
                logs = _checked(["docker", "logs", name], deadline)
                return subprocess.CompletedProcess(name, status["ExitCode"], logs, "")
            if time.monotonic() >= deadline:
                raise TimeoutError("container execution timed out")
            state.cancelled.wait(0.1)

    def _execute(self, run_id: str, state: _Run, deadline: float, **kwargs):
        name = self._create(run_id, state, deadline, **kwargs)
        self._start(state, name, deadline)
        result = self._wait(state, name, deadline)
        with self._locked(state, deadline):
            self._remove(state, name, False, deadline)
        if result.returncode:
            raise RuntimeError(captured_text(result.stdout) or
                               f"container exited {result.returncode}")
        return result.stdout

    def _remove(self, state: _Run, name: str, volume: bool, deadline: float) -> None:
        args = (["docker", "volume", "rm", "--force", name] if volume
                else ["docker", "rm", "--force", name])
        try:
            _checked(args, deadline)
        except RuntimeError:
            # 'Not found' and daemon failures must not be conflated. A successful
            # list explicitly confirming absence is required after a failed rm.
            listing = (["docker", "volume", "ls", "--format={{.Name}}"] if volume
                       else ["docker", "ps", "-a", "--format={{.Names}}"])
            names = _checked(listing + ["--filter", f"name={name}"], deadline).splitlines()
            if name in names:
                raise RuntimeError(f"cleanup not confirmed for {name}") from None
        (state.volumes if volume else state.containers).discard(name)

    def _cleanup(self, state: _Run, *, keep_dependencies: bool = False,
                 deadline: float | None = None) -> None:
        deadline = deadline or time.monotonic() + STOP_SECONDS
        with self._locked(state, deadline):
            self._cleanup_locked(state, deadline, keep_dependencies=keep_dependencies)

    def _cleanup_locked(self, state: _Run, deadline: float, *,
                        keep_dependencies: bool = False) -> None:
        for name in list(state.containers):
            self._remove(state, name, False, deadline)
        for name in list(state.volumes):
            if keep_dependencies and name == state.dependency_volume:
                continue
            self._remove(state, name, True, deadline)
        if not keep_dependencies:
            state.dependency_volume = None
            state.requirements = state.versions = None

    @staticmethod
    def _mount(volume: str, target: str, readonly: bool = False) -> str:
        return f"type=volume,source={volume},target={target}" + (",readonly" if readonly else "")

    def _ownership(self, run_id: str, state: _Run, deadline: float,
                   mounts: list[str], paths: list[str]) -> None:
        # Only fixed target paths, with no candidate content used as executable input.
        script = ("import os; from pathlib import Path; "
                  f"roots={paths!r}; "
                  "paths=[p for r in roots for p in [Path(r), *Path(r).rglob('*')]]; "
                  "[(os.chown(p,10001,10001), os.chmod(p,0o700 if p.is_dir() "
                  "else 0o600)) for p in paths]")
        self._execute(run_id, state, deadline, mounts=mounts, arguments=["-c", script], root=True)

    def prepare(self, run_id: str, requirements: list[str],
                workspace_id: str = "default") -> dict[str, str]:
        requirements = normalize_requirements(requirements)
        state = self._state(run_id, workspace_id)
        self._active(state)
        if not state.operation.acquire(blocking=False):
            raise RuntimeError("run already has an active operation")
        deadline = time.monotonic() + PREPARE_SECONDS
        try:
            self._active(state)
            if state.requirements is not None:
                if state.requirements != requirements:
                    raise ValueError("run dependencies already prepared with different requirements")
                return dict(state.versions)
            wheels = self._volume(run_id, state, deadline)
            deps = self._volume(run_id, state, deadline)
            self._ownership(run_id, state, deadline,
                            [self._mount(wheels, "/wheels"), self._mount(deps, "/deps")],
                            ["/wheels", "/deps"])
            normalized = json.dumps(requirements, separators=(",", ":"))
            self._execute(run_id, state, deadline, mounts=[self._mount(wheels, "/wheels")],
                          arguments=["-m", "devflow.dependencies", "download", normalized],
                          network=True)
            versions = json.loads(self._execute(
                run_id, state, deadline,
                mounts=[self._mount(wheels, "/wheels", True), self._mount(deps, "/deps")],
                arguments=["-m", "devflow.dependencies", "install", normalized],
            ))
            if (not isinstance(versions, dict) or len(versions) > 200
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in versions.items())):
                raise ValueError("invalid installed dependency versions")
            with self._locked(state, deadline):
                self._active(state)
                state.dependency_volume = deps
                state.requirements, state.versions = requirements, versions
            self._cleanup(state, keep_dependencies=True)
            self._active(state)
            return dict(versions)
        except Exception:
            self._cancel(run_id, state)
            self._cleanup(state)
            raise
        finally:
            state.operation.release()

    def run(self, run_id: str, action: str, workspace_id: str = "default") -> CommandResult:
        command = action_command(action)
        candidate = self._candidate(run_id, workspace_id)
        state = self._state(run_id, workspace_id)
        self._active(state)
        if not state.operation.acquire(blocking=False):
            raise RuntimeError("run already has an active operation")
        started = time.monotonic()
        deadline = started + RUN_SECONDS
        report = None
        try:
            command = action_command(action, dependency_env=state.dependency_volume is not None)
            volume = self._volume(run_id, state, deadline)
            stage = self._create(run_id, state, deadline, mounts=[self._mount(volume, "/candidate")],
                                 arguments=["-c", "pass"])
            # docker cp reads only the validated candidate; no source is ever
            # mounted in the networked downloader or the offline installer.
            with self._locked(state, deadline):
                self._active(state)
                _checked(["docker", "cp", f"{candidate}{os.sep}.", f"{stage}:/candidate"], deadline)
                self._remove(state, stage, False, deadline)
            self._ownership(run_id, state, deadline, [self._mount(volume, "/candidate")],
                            ["/candidate"])
            mounts = [self._mount(volume, "/candidate", True)]
            if state.dependency_volume:
                mounts.append(self._mount(state.dependency_volume, "/deps", True))
            name = self._create(run_id, state, deadline, mounts=mounts,
                                arguments=["-m", "devflow.candidate_runner", action],
                                prepared=state.dependency_volume is not None)
            self._start(state, name, deadline)
            result = self._wait(state, name, deadline)
            if result.returncode not in (0, 1):
                report = CommandResult(passed=False, command=command, stdout=result.stdout,
                                       stderr="candidate container failed", duration_ms=0,
                                       exit_code=result.returncode)
            else:
                report = CommandResult.model_validate_json(result.stdout)
                if report.command != command or report.passed != (result.returncode == 0):
                    raise ValueError("container result does not match the requested check")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            report = CommandResult(
                passed=False, command=command, stdout="", stderr=captured_text(str(error)),
                duration_ms=round((time.monotonic() - started) * 1000), exit_code=None,
                timed_out=isinstance(error, (TimeoutError, subprocess.TimeoutExpired)),
            )
        finally:
            try:
                self._cleanup(state, keep_dependencies=not state.cancelled.is_set())
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                self._cancel(run_id, state)
                report = CommandResult(passed=False, command=command, stdout="",
                                       stderr=f"container cleanup failed: {error}",
                                       duration_ms=0, exit_code=None)
            state.operation.release()
        return report

    def stop(self, run_id: str) -> bool:
        state = self._state(run_id)
        self._cancel(run_id, state)
        deadline = time.monotonic() + STOP_SECONDS
        with self._locked(state, deadline):
            # Query Docker even if this process never saw the worker. The common
            # lock and persistent stop marker prevent that worker's late starts.
            self._discover(deadline, run_id=run_id)
            self._cleanup_locked(state, deadline)
        return True  # All registered resources removed; starts permanently blocked.

    def reconcile(self, run_id: str | None = None) -> None:
        """Before accepting requests, remove this owner's resources from a restart.

        Discovery errors fail startup, not silently discard uncertain resources.
        Recovered run IDs are tombstoned, never resumed against a partial env.
        """
        deadline = time.monotonic() + STOP_SECONDS
        if run_id is not None:
            state = self._state(run_id)
            with self._locked(state, deadline):
                self._discover(deadline, run_id=run_id)
                if state.containers or state.volumes:
                    self._cancel(run_id, state)
                    self._cleanup_locked(state, deadline)
            return
        self._discover(deadline)
        for identity, state in list(self._states.items()):
            self._cancel(identity, state)
            self._cleanup(state, deadline=deadline)

    def _discover(self, deadline: float, *, run_id: str | None = None) -> None:
        filters = ["--filter", f"label={MANAGED_LABEL}",
                   "--filter", f"label=devflow.owner={self.owner}"]
        if run_id is not None:
            filters.extend(["--filter", f"label=devflow.run={safe_id(run_id)}"])
        for volume in (False, True):
            listing = (["docker", "volume", "ls", "-q"] if volume
                       else ["docker", "ps", "-aq"])
            names = _checked([*listing, *filters], deadline).splitlines()
            if not names:
                continue
            if len(names) > 1000:
                raise RuntimeError("too many orphan runner resources; operator cleanup required")
            inspect = (["docker", "volume", "inspect"] if volume else ["docker", "inspect"])
            for data in json.loads(_checked([*inspect, *names], deadline)):
                labels = data.get("Labels", {}) if volume else data["Config"]["Labels"]
                if (labels.get("devflow.runner") != "beta"
                        or labels.get("devflow.owner") != self.owner):
                    raise RuntimeError("unexpected resource owner during reconciliation")
                identity = safe_id(labels["devflow.run"])
                if run_id is not None and identity != run_id:
                    raise RuntimeError("unexpected run identity during reconciliation")
                state = self._state(identity, safe_id(labels["devflow.workspace"]))
                name = data["Name"].lstrip("/")
                (state.volumes if volume else state.containers).add(name)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(5)
        try:
            raw = self.rfile.readline(REQUEST_LIMIT + 1)
            if not raw.endswith(b"\n") or len(raw) > REQUEST_LIMIT:
                raise ValueError("invalid dispatcher request")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise TypeError("dispatcher request must be an object")
            operation = request.get("operation", "run")
            expected = {"run_id", "operation"}
            if operation == "run":
                expected.update({"workspace_id", "action"})
            elif operation == "prepare":
                expected.update({"workspace_id", "requirements"})
            elif operation != "stop":
                raise ValueError("unsupported dispatcher operation")
            if set(request) - expected:
                raise ValueError("unexpected dispatcher request fields")
            dispatcher = self.server.dispatcher
            run_id = safe_id(request["run_id"])
            if operation == "stop":
                response = {"stopped": dispatcher.stop(run_id)}
            elif operation == "prepare":
                response = {"versions": dispatcher.prepare(
                    run_id, request["requirements"], request.get("workspace_id", "default"),
                )}
            else:
                response = {"result": dispatcher.run(
                    run_id, request["action"], request.get("workspace_id", "default"),
                ).model_dump(mode="json")}
        except Exception as error:  # noqa: BLE001 - bounded protocol failure
            response = {"error": captured_text(f"runner dispatcher failed: {type(error).__name__}: {error}")}
        try:
            self.wfile.write(json.dumps(response, sort_keys=True).encode("utf-8") + b"\n")
        except OSError:
            pass


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class _Server(socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, socket_path: str, dispatcher: DockerDispatcher):
        self.dispatcher = dispatcher
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(socket_path, _Handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main() -> None:
    if not hasattr(socketserver, "UnixStreamServer"):
        raise SystemExit("runner dispatcher requires a POSIX Unix-domain socket")
    socket_path = os.environ.get("DEVFLOW_RUNNER_SOCKET", "/run/devflow/runner.sock")
    legacy_root = os.environ.get("DEVFLOW_CANDIDATES_ROOT")
    image = os.environ.get("DEVFLOW_RUNNER_IMAGE", "devflow-candidate-runner:compose")
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dispatcher = DockerDispatcher(Path(legacy_root) if legacy_root else None, image,
                                  state_root=path.parent / "stopped")
    dispatcher.reconcile()
    if path.exists():
        path.unlink()
    previous_umask = os.umask(0o117)
    try:
        server = _Server(socket_path, dispatcher)
        os.chown(socket_path, 10001, 10001)
    finally:
        os.umask(previous_umask)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
