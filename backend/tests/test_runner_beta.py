from __future__ import annotations

import json
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest

from devflow import docker_runner, runner_dispatch
from devflow.candidate_runner import action_command
from devflow.models import CommandResult


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = {}
        self.volumes = {}
        self.block_action = None
        self.started = threading.Event()
        self.fail_remove = False
        self.before_create = None
        self.fail_start = False

    @staticmethod
    def labels(args):
        return dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "--label")

    def __call__(self, args, *, timeout):
        assert 0 < timeout <= runner_dispatch.CLI_SECONDS
        assert args[0] == "docker"
        self.calls.append(list(args))
        out = ""
        if args[1:3] == ["volume", "create"]:
            self.volumes[args[-1]] = self.labels(args)
        elif args[1] == "create":
            name = args[args.index("--name") + 1]
            self.containers[name] = {"args": args, "labels": self.labels(args), "running": False}
            if self.before_create:
                self.before_create(args)
        elif args[1] == "start":
            data = self.containers[args[-1]]
            command = data["args"]
            action = command[-2] if "devflow.dependencies" in command else command[-1]
            data["running"] = action == self.block_action
            if data["running"]:
                self.started.set()
            if self.fail_start:
                raise subprocess.TimeoutExpired(args, timeout)
        elif args[1] == "inspect" and args[2].startswith("--format"):
            data = self.containers[args[-1]]
            out = json.dumps({"Running": data["running"], "ExitCode": 0,
                              "Status": "running" if data["running"] else "exited"})
        elif args[1] == "logs":
            command = self.containers[args[-1]]["args"]
            if "devflow.candidate_runner" in command:
                out = CommandResult(
                    passed=True, command=action_command(command[-1],
                    dependency_env="--env=DEVFLOW_DEPENDENCY_ENV=1" in command),
                    stdout="ok", stderr="", duration_ms=1, exit_code=0,
                ).model_dump_json()
            elif "install" in command:
                out = json.dumps({"pytest": "8.4.0", "ruff": "0.12.0", "requests": "2.32.0"})
        elif args[1] == "rm" or args[1:3] == ["volume", "rm"]:
            if self.fail_remove:
                return subprocess.CompletedProcess(args, 1, "", "daemon denied removal")
            collection = self.volumes if args[1] == "volume" else self.containers
            collection.pop(args[-1], None)
        elif args[1] == "ps" or args[1:3] == ["volume", "ls"]:
            collection = self.volumes if args[1] == "volume" else self.containers
            names = list(collection)
            for value in args:
                if value.startswith("name="):
                    names = [n for n in names if value[5:] in n]
                elif value.startswith("label="):
                    key, expected = value[6:].split("=", 1)
                    names = [n for n in names if (collection[n] if args[1] == "volume"
                             else collection[n]["labels"]).get(key) == expected]
            out = "\n".join(names)
        elif args[1] == "inspect":
            out = json.dumps([{"Name": n, "Config": {"Labels": self.containers[n]["labels"]}}
                              for n in args[2:]])
        elif args[1:3] == ["volume", "inspect"]:
            out = json.dumps([{"Name": n, "Labels": self.volumes[n]} for n in args[3:]])
        elif args[1] != "cp":
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.fixture
def socket_family(monkeypatch):
    monkeypatch.setattr(docker_runner.socket, "AF_UNIX", 1, raising=False)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(runner_dispatch, "_cli", fake)
    root = tmp_path / "workspaces"
    candidate = root / "run-a" / "candidates" / "run-a"
    candidate.mkdir(parents=True)
    (candidate / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    dispatcher = runner_dispatch.DockerDispatcher(
        image="runner:test", workspaces_root=root, state_root=tmp_path / "stopped",
    )
    return dispatcher, fake, root


def test_prepare_network_boundary_offline_env_and_cleanup(setup):
    dispatcher, fake, root = setup
    versions = dispatcher.prepare("run-a", ["Requests>=2"], "run-a")
    assert versions["requests"] == "2.32.0"
    creates = [c for c in fake.calls if c[1] == "create"]
    networked = [c for c in creates if "--network=bridge" in c]
    assert len(networked) == 1
    download = networked[0]
    assert download[-3:] == ["devflow.dependencies", "download", '["requests>=2"]']
    assert len([v for v in download if v.startswith("type=volume")]) == 1
    assert "target=/wheels" in next(v for v in download if v.startswith("type=volume"))
    assert not any("/candidate" in v or "sock" in v or str(root) in v for v in download
                   if not v.startswith("devflow.owner="))
    assert not any(v.startswith("type=bind") for v in download)
    assert "--user=10001:10001" in download
    install = next(c for c in creates if "install" in c)
    assert "--network=none" in install and "--user=10001:10001" in install
    assert any(v.endswith("target=/wheels,readonly") for v in install)
    assert any(v.endswith("target=/deps") for v in install)
    assert not fake.containers
    assert len(fake.volumes) == 1  # Only the prepared per-run environment survives.
    count = len(fake.calls)
    assert dispatcher.prepare("run-a", ["requests>=2"], "run-a") == versions
    assert len(fake.calls) == count
    assert dispatcher.run("run-a", "test", "run-a").passed
    invocation = next(c for c in reversed(fake.calls) if "devflow.candidate_runner" in c)
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--user=10001:10001",
                 "--security-opt=no-new-privileges:true", "--pids-limit=64", "--memory=512m",
                 "--cpus=1", "--pull=never"):
        assert flag in invocation
    mounts = [v for v in invocation if v.startswith("type=volume")]
    assert len(mounts) == 2 and all(v.endswith(",readonly") for v in mounts)
    assert not fake.containers and len(fake.volumes) == 1
    assert dispatcher.stop("run-a") is True
    assert not fake.containers and not fake.volumes
    assert dispatcher.stop("run-a") is True
    with pytest.raises(RuntimeError, match="stopped"):
        dispatcher.prepare("run-a", [], "run-a")


def test_legacy_default_workspace_run_without_prepare(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(runner_dispatch, "_cli", fake)
    candidate = tmp_path / "candidates" / "legacy"
    candidate.mkdir(parents=True)
    dispatcher = runner_dispatch.DockerDispatcher(candidate.parent, "runner:test")
    assert dispatcher.run("legacy", "lint").passed
    assert not fake.volumes and not fake.containers


@pytest.mark.parametrize("identifier", ["../escape", "/tmp/a", "C:/work", "a/b", "a\\b", "", None])
def test_workspace_paths_are_not_client_controlled(setup, identifier):
    dispatcher, fake, _ = setup
    with pytest.raises(ValueError):
        dispatcher.run("run-a", "test", identifier)
    assert not fake.calls


def test_arbitrary_action_rejected_before_docker(setup):
    dispatcher, fake, _ = setup
    with pytest.raises(ValueError):
        dispatcher.run("run-a", "sh -c whoami", "run-a")
    assert not fake.calls


def test_prepare_revalidates_requirements_before_any_docker(setup):
    dispatcher, fake, _ = setup
    with pytest.raises(ValueError):
        dispatcher.prepare("run-a", ["foo @ https://evil.invalid/x.whl"], "run-a")
    assert not fake.calls


def test_stop_kills_active_downloader_and_prevents_followup_install(setup):
    dispatcher, fake, _ = setup
    fake.block_action = "download"
    errors = []

    def prepare():
        try:
            dispatcher.prepare("run-a", [], "run-a")
        except RuntimeError as error:
            errors.append(error)

    worker = threading.Thread(target=prepare)
    worker.start()
    assert fake.started.wait(2)
    assert dispatcher.stop("run-a") is True
    worker.join(2)
    assert not worker.is_alive() and errors
    assert not fake.containers and not fake.volumes
    assert not any("install" in c for c in fake.calls)


def test_stop_kills_running_test(setup):
    dispatcher, fake, _ = setup
    fake.block_action = "test"
    reports = []
    worker = threading.Thread(target=lambda: reports.append(dispatcher.run("run-a", "test", "run-a")))
    worker.start()
    assert fake.started.wait(2)
    assert dispatcher.stop("run-a")
    worker.join(2)
    assert not worker.is_alive() and not reports[0].passed
    assert not fake.containers and not fake.volumes


def test_stop_during_create_blocks_late_start(setup):
    dispatcher, fake, _ = setup
    entered, release = threading.Event(), threading.Event()

    def before_create(args):
        if "devflow.candidate_runner" in args:
            entered.set()
            assert release.wait(2)

    fake.before_create = before_create
    worker = threading.Thread(target=lambda: dispatcher.run("run-a", "test", "run-a"))
    worker.start()
    assert entered.wait(2)
    stops = []
    stopper = threading.Thread(target=lambda: stops.append(dispatcher.stop("run-a")))
    stopper.start()
    assert dispatcher._states["run-a"].cancelled.wait(2)
    release.set()
    worker.join(2)
    stopper.join(2)
    assert stops == [True]
    created = next(c for c in fake.calls if "devflow.candidate_runner" in c)
    name = created[created.index("--name") + 1]
    assert ["docker", "start", name] not in fake.calls
    assert not fake.containers and not fake.volumes


def test_failed_cleanup_is_not_confirmed_and_can_be_retried(setup):
    dispatcher, fake, _ = setup
    dispatcher.prepare("run-a", [], "run-a")
    fake.fail_remove = True
    with pytest.raises(RuntimeError, match="cleanup not confirmed"):
        dispatcher.stop("run-a")
    assert fake.volumes and dispatcher._states["run-a"].volumes
    with pytest.raises(RuntimeError, match="stopped"):
        dispatcher.prepare("run-a", [], "run-a")
    fake.fail_remove = False
    assert dispatcher.stop("run-a") and not fake.volumes


def test_start_timeout_does_not_leak_container_or_volume(setup):
    dispatcher, fake, _ = setup
    fake.fail_start = True
    report = dispatcher.run("run-a", "test", "run-a")
    assert report.timed_out and not report.passed
    assert not fake.containers and not fake.volumes


def test_restart_reconciles_labels_and_preserves_stop_tombstone(setup, tmp_path):
    dispatcher, fake, root = setup
    dispatcher.prepare("run-a", [], "run-a")
    assert fake.volumes
    restarted = runner_dispatch.DockerDispatcher(
        image="runner:test", workspaces_root=root, state_root=tmp_path / "stopped",
    )
    restarted.reconcile()
    assert not fake.volumes and not fake.containers
    again = runner_dispatch.DockerDispatcher(
        image="runner:test", workspaces_root=root, state_root=tmp_path / "stopped",
    )
    with pytest.raises(RuntimeError, match="stopped"):
        again.prepare("run-a", [], "run-a")


def test_stop_unknown_run_prevents_later_creation(setup):
    dispatcher, fake, _ = setup
    assert dispatcher.stop("not-started")
    with pytest.raises(RuntimeError, match="stopped"):
        dispatcher.prepare("not-started", [])
    assert len(fake.calls) == 2  # Both Docker resource lists positively confirmed empty.


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, timeout):
        assert 0 < timeout <= 340

    def connect(self, _):
        pass

    def sendall(self, value):
        self.sent = json.loads(value)

    def recv(self, count):
        value, self.response = self.response[:count], self.response[count:]
        return value


def socket_workspace(root):
    return SimpleNamespace(root=root, candidate_path=lambda run: root / "candidates" / run,
                           require_materialized_candidate=lambda run, path: path)


@pytest.mark.parametrize("response", [b"not json\n", b"[]\n", b"{}\n", b'{"result":{}}\n',
                                      b"\xff\n", b"", b"{", b'{"error": 4}\n',
                                      b"x" * (runner_dispatch.RESPONSE_LIMIT + 1)],
                         ids=["json", "array", "empty", "model", "utf8", "eof", "partial",
                              "error", "oversized"])
def test_malformed_socket_run_response_is_bounded_failure(tmp_path, monkeypatch, response,
                                                         socket_family):
    fake = FakeSocket(response)
    monkeypatch.setattr(docker_runner.socket, "socket", lambda *_: fake)
    monkeypatch.setenv("DEVFLOW_WORKSPACES_ROOT", str(tmp_path))
    report = docker_runner.SocketCandidateRunner("control.sock").run(
        socket_workspace(tmp_path / "run-a"), "run-a", "test",
    )
    assert not report.passed and report.exit_code is None
    assert "dispatcher" in report.stderr


def test_socket_prepare_sends_only_safe_identity_and_normalized_inputs(tmp_path, monkeypatch,
                                                                     socket_family):
    fake = FakeSocket(b'{"versions":{"requests":"2.32.0"}}\n')
    monkeypatch.setattr(docker_runner.socket, "socket", lambda *_: fake)
    monkeypatch.setenv("DEVFLOW_WORKSPACES_ROOT", str(tmp_path))
    runner = docker_runner.SocketCandidateRunner("control.sock")
    assert runner.prepare(socket_workspace(tmp_path / "run-a"), "run-a", ["Requests"]) == {
        "requests": "2.32.0",
    }
    assert fake.sent == {"operation": "prepare", "run_id": "run-a", "workspace_id": "run-a",
                         "requirements": ["requests"]}


@pytest.mark.parametrize("response", [b'{"stopped":false}\n', b'{"stopped":1}\n', b'{}\n'])
def test_stop_requires_explicit_true_response(monkeypatch, response, socket_family):
    monkeypatch.setattr(docker_runner.socket, "socket", lambda *_: FakeSocket(response))
    with pytest.raises(RuntimeError):
        docker_runner.SocketCandidateRunner("control.sock").stop("run-a")


def test_direct_runner_uses_same_lifecycle(setup, monkeypatch):
    _, fake, root = setup
    monkeypatch.setattr(docker_runner.DockerCandidateRunner, "_runs", {})
    monkeypatch.setattr(docker_runner.DockerCandidateRunner, "_stopped", set())
    monkeypatch.setattr(docker_runner.DockerCandidateRunner, "_ready", {})
    monkeypatch.setattr(docker_runner.DockerCandidateRunner, "_errors", {})
    monkeypatch.setenv("DEVFLOW_LOCAL_RUNNER_STATE", str(root.parent / "local-state"))
    if docker_runner.os.name == "posix":
        monkeypatch.setattr(docker_runner.os, "getuid", lambda: 10001)
    workspace = socket_workspace(root / "run-a")
    workspace.candidates_root = workspace.root / "candidates"
    runner = docker_runner.DockerCandidateRunner("runner:test")
    assert runner.prepare(workspace, "run-a", ["requests"])
    assert runner.run(workspace, "run-a", "test").passed
    assert docker_runner.DockerCandidateRunner("runner:test").stop("run-a")
    assert not fake.volumes and not fake.containers
    assert not runner.run(workspace, "run-a", "test").passed


def test_direct_parent_stop_discovers_spawn_child_resources_without_memory_registry(setup, monkeypatch):
    _, fake, root = setup
    monkeypatch.setenv("DEVFLOW_LOCAL_RUNNER_STATE", str(root.parent / "spawn-state"))
    for name, value in (("_runs", {}), ("_stopped", set()), ("_ready", {}), ("_errors", {})):
        monkeypatch.setattr(docker_runner.DockerCandidateRunner, name, value)
    if docker_runner.os.name == "posix":
        monkeypatch.setattr(docker_runner.os, "getuid", lambda: 10001)
    workspace = socket_workspace(root / "run-a")
    workspace.candidates_root = workspace.root / "candidates"
    worker = docker_runner.DockerCandidateRunner("runner:test")
    worker.prepare(workspace, "run-a", [])
    child_dispatcher = worker._runs["run-a"]
    assert fake.volumes
    # A spawn parent never receives the child's dictionary mutations.
    monkeypatch.setattr(docker_runner.DockerCandidateRunner, "_runs", {})
    monkeypatch.setattr(docker_runner.DockerCandidateRunner, "_ready", {})
    parent = docker_runner.DockerCandidateRunner("runner:test")
    before = len(fake.calls)
    assert parent.stop("run-a") is True
    assert any(c[1:3] == ["volume", "ls"] for c in fake.calls[before:])
    assert not fake.volumes and not fake.containers
    with pytest.raises(RuntimeError, match="stopped"):
        child_dispatcher.run("run-a", "test")


def test_direct_unknown_stop_queries_docker_and_fails_on_unavailable_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVFLOW_LOCAL_RUNNER_STATE", str(tmp_path / "state"))
    calls = []

    def unavailable(args, *, timeout):
        calls.append(args)
        raise FileNotFoundError("daemon unavailable")

    monkeypatch.setattr(runner_dispatch, "_cli", unavailable)
    with pytest.raises(FileNotFoundError):
        docker_runner.DockerCandidateRunner().stop("unknown-parent-run")
    assert calls and "label=devflow.run=unknown-parent-run" in calls[0]


def test_independent_dispatchers_share_stop_marker_and_start_lock(setup, tmp_path):
    _, fake, root = setup
    state_root = tmp_path / "shared-state"
    child = runner_dispatch.DockerDispatcher(
        workspaces_root=root, state_root=state_root, owner="shared", cross_process=True,
    )
    parent = runner_dispatch.DockerDispatcher(
        workspaces_root=root, state_root=state_root, owner="shared", cross_process=True,
    )
    entered, release = threading.Event(), threading.Event()

    def create(args):
        if "devflow.candidate_runner" in args:
            entered.set()
            assert release.wait(3)

    fake.before_create = create
    worker = threading.Thread(target=lambda: child.run("run-a", "test", "run-a"))
    worker.start()
    assert entered.wait(2)
    answers = []
    stopper = threading.Thread(target=lambda: answers.append(parent.stop("run-a")))
    stopper.start()
    assert parent._states["run-a"].cancelled.wait(2)
    # Synchronize with the on-disk marker, not the child's in-memory event.
    for _ in range(100):
        if (state_root / "run-a").exists():
            break
        threading.Event().wait(0.01)
    assert (state_root / "run-a").exists()
    release.set()
    worker.join(3)
    stopper.join(3)
    assert not worker.is_alive() and not stopper.is_alive()
    assert answers == [True] and not fake.containers and not fake.volumes
    created = next(c for c in fake.calls if "devflow.candidate_runner" in c)
    name = created[created.index("--name") + 1]
    assert ["docker", "start", name] not in fake.calls


def test_threaded_server_accepts_stop_while_run_is_waiting(tmp_path):
    entered, stopped = threading.Event(), threading.Event()

    class Dispatcher:
        def run(self, run_id, action, workspace_id):
            entered.set()
            assert stopped.wait(2)
            return CommandResult(passed=False, command=action_command(action), stdout="",
                                 stderr="stopped", duration_ms=1, exit_code=None)

        def stop(self, run_id):
            stopped.set()
            return True

    # Windows lacks UnixStreamServer; the production mixin falls back to TCP for
    # this protocol test, while main() still requires Unix sockets in deployment.
    address = ("127.0.0.1", 0) if not hasattr(runner_dispatch.socketserver, "UnixStreamServer") \
        else str(tmp_path / "runner.sock")
    server = runner_dispatch._Server(address, Dispatcher())
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    def connect():
        if isinstance(address, tuple):
            return socket.create_connection(server.server_address, timeout=2)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(address)
        return client

    try:
        with connect() as running:
            running.sendall(b'{"run_id":"run-a","action":"test"}\n')
            assert entered.wait(2)
            with connect() as stopping:
                stopping.sendall(b'{"run_id":"run-a","operation":"stop"}\n')
                assert json.loads(stopping.recv(4096)) == {"stopped": True}
            assert "result" in json.loads(running.recv(4096))
    finally:
        server.shutdown()
        server.server_close()
        serving.join(2)


def test_network_container_forwards_only_standard_proxy_settings(monkeypatch, setup):
    dispatcher, fake, _ = setup
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("DEVFLOW_API_KEY", "must-not-forward")
    dispatcher.prepare("run-proxy", ["Requests>=2"], "run-proxy")
    download = next(c for c in fake.calls if "download" in c)
    assert "--env=HTTPS_PROXY=http://proxy.invalid:8080" in download
    assert not any("DEVFLOW_API_KEY" in item for item in download)
