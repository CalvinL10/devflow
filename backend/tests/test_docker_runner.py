from __future__ import annotations

import subprocess

import pytest
from test_runner_beta import FakeDocker

from devflow import docker_runner, runner_dispatch
from devflow.candidate_runner import action_command
from devflow.models import CommandResult
from devflow.workspace import ManagedWorkspace


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    if docker_runner.os.name == "posix":
        monkeypatch.setattr(docker_runner.os, "getuid", lambda: 10001)
    for name, value in (("_runs", {}), ("_stopped", set()), ("_ready", {}), ("_errors", {})):
        monkeypatch.setattr(docker_runner.DockerCandidateRunner, name, value)
    monkeypatch.setenv("DEVFLOW_LOCAL_RUNNER_STATE", str(tmp_path / "runner-state"))
    workspace = ManagedWorkspace(tmp_path / "workspace", None)
    workspace.initialize()
    workspace.candidate_path("run").mkdir()
    (workspace.candidate_path("run") / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    return workspace


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(runner_dispatch, "_cli", fake)
    return fake


def result(action="lint"):
    return CommandResult(passed=True, command=action_command(action), stdout="ok", stderr="",
                         duration_ms=1, exit_code=0)


def test_docker_invocation_preserves_execution_boundary(candidate, docker):
    assert docker_runner.DockerCandidateRunner().run(candidate, "run", "lint").passed
    invocation = next(c for c in docker.calls if "devflow.candidate_runner" in c)
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--pids-limit=64",
                 "--security-opt=no-new-privileges:true", "--pull=never", "--memory=512m",
                 "--cpus=1", "--user=10001:10001",
                 "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m"):
        assert flag in invocation
    assert invocation[:2] == ["docker", "create"]
    name = invocation[invocation.index("--name") + 1]
    assert ["docker", "start", name] in docker.calls
    assert "--privileged" not in invocation
    assert not any(v.startswith("type=bind") for v in invocation)
    assert any(v.endswith("target=/candidate,readonly") for v in invocation)
    assert str(candidate.root) not in " ".join(invocation)
    assert invocation[-4:] == ["-I", "-m", "devflow.candidate_runner", "lint"]
    before = list(docker.calls)
    with pytest.raises(ValueError, match="unsupported"):
        docker_runner.DockerCandidateRunner().run(candidate, "run", "echo bad")
    assert docker.calls == before


def test_client_timeout_removes_only_its_containers(candidate, docker):
    docker.fail_start = True
    report = docker_runner.DockerCandidateRunner().run(candidate, "run", "test")
    assert report.timed_out and not report.passed
    assert report.exit_code is None
    assert not docker.containers and not docker.volumes
    created = {c[c.index("--name") + 1] for c in docker.calls if c[1] == "create"}
    removed = {c[-1] for c in docker.calls if c[1] == "rm"}
    assert removed == created


@pytest.mark.parametrize("cleanup_failure", ["unavailable", "timeout", "nonzero"])
def test_client_timeout_cleanup_failure_does_not_hide_report(candidate, docker, monkeypatch,
                                                            cleanup_failure):
    def execute(args, *, timeout):
        if args[1] == "start":
            raise subprocess.TimeoutExpired(args, timeout)
        if args[1] == "rm":
            if cleanup_failure == "unavailable":
                raise FileNotFoundError("docker unavailable during cleanup")
            if cleanup_failure == "timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args, 1, "", "container still running")
        return docker(args, timeout=timeout)

    monkeypatch.setattr(runner_dispatch, "_cli", execute)
    runner = docker_runner.DockerCandidateRunner()
    report = runner.run(candidate, "run", "test")
    assert not report.passed and report.exit_code is None
    assert "container cleanup failed" in report.stderr
    with pytest.raises((OSError, RuntimeError, subprocess.TimeoutExpired)):
        runner.stop("run")
    monkeypatch.setattr(runner_dispatch, "_cli", docker)
    assert runner.stop("run")
    assert not docker.containers and not docker.volumes


def test_unavailable_docker_fails_without_host_fallback(candidate, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker unavailable")

    monkeypatch.setattr(runner_dispatch, "_cli", unavailable)
    report = docker_runner.DockerCandidateRunner().run(candidate, "run", "lint")
    assert not report.passed and report.exit_code is None
    assert "runner unavailable" in report.stderr


def test_container_crash_is_returned_as_a_traceable_failure(candidate, docker, monkeypatch):
    original = runner_dispatch.DockerDispatcher._wait

    def wait(self, state, name, deadline):
        if "devflow.candidate_runner" in docker.containers[name]["args"]:
            return subprocess.CompletedProcess(name, 137, "partial", "killed")
        return original(self, state, name, deadline)

    monkeypatch.setattr(runner_dispatch.DockerDispatcher, "_wait", wait)
    report = docker_runner.DockerCandidateRunner().run(candidate, "run", "test")
    assert not report.passed and not report.timed_out
    assert report.exit_code == 137 and report.stdout == "partial"
    assert not docker.containers and not docker.volumes


@pytest.mark.parametrize("stdout,exit_code", [
    ("not json", 0), (result("test").model_dump_json(), 0), (result().model_dump_json(), 1),
])
def test_invalid_container_report_is_not_accepted(candidate, docker, monkeypatch, stdout, exit_code):
    original = runner_dispatch.DockerDispatcher._wait

    def wait(self, state, name, deadline):
        if "devflow.candidate_runner" in docker.containers[name]["args"]:
            return subprocess.CompletedProcess(name, exit_code, stdout, "")
        return original(self, state, name, deadline)

    monkeypatch.setattr(runner_dispatch.DockerDispatcher, "_wait", wait)
    report = docker_runner.DockerCandidateRunner().run(candidate, "run", "lint")
    assert not report.passed and report.exit_code is None
    assert not docker.containers and not docker.volumes


def test_dispatcher_copies_only_candidate_into_temporary_read_only_volume(candidate, docker):
    dispatcher = runner_dispatch.DockerDispatcher(candidate.candidates_root, "runner:test")
    assert dispatcher.run("run", "lint").passed
    copy = next(c for c in docker.calls if c[1] == "cp")
    assert copy[2] == f"{candidate.candidate_path('run')}{runner_dispatch.os.sep}."
    invocation = next(c for c in docker.calls if "devflow.candidate_runner" in c)
    mounts = [v for v in invocation if v.startswith("type=volume")]
    assert len(mounts) == 1 and mounts[0].endswith("target=/candidate,readonly")
    assert not docker.containers and not docker.volumes


def test_runner_factory_uses_dispatch_socket(monkeypatch):
    monkeypatch.setenv("DEVFLOW_RUNNER_SOCKET", "/run/devflow/runner.sock")
    assert isinstance(docker_runner.candidate_runner_from_environment(), docker_runner.SocketCandidateRunner)
