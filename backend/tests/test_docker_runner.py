from __future__ import annotations

import subprocess

import pytest

from devflow import docker_runner, runner_dispatch
from devflow.candidate_runner import action_command
from devflow.coordinator import RunCoordinator
from devflow.models import CommandResult


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    if docker_runner.os.name == "posix":
        monkeypatch.setattr(docker_runner.os, "getuid", lambda: 10001)
        monkeypatch.setattr(docker_runner.os, "getgid", lambda: 10001)
    coordinator = RunCoordinator(tmp_path / "runner.sqlite")
    workspace = coordinator.workspace
    coordinator.database.create_run(
        run_id="run", thread_id="run", patch_id="patch", patch_revision=1,
        candidate_dir=str(workspace.candidate_path("run")), patch={"files": []},
    )
    workspace.materialize_candidate(run_id="run", base_revision=0)
    return workspace


def result(action="lint"):
    return CommandResult(passed=True, command=action_command(action), stdout="ok", stderr="",
                         duration_ms=1, exit_code=0)


def test_docker_invocation_preserves_execution_boundary(candidate, monkeypatch):
    calls = []
    def execute(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, result().model_dump_json(), "")
    monkeypatch.setattr(docker_runner.subprocess, "run", execute)
    assert docker_runner.DockerCandidateRunner().run(candidate, "run", "lint").passed
    args, options = calls[0]
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--pids-limit=64",
                 "--security-opt=no-new-privileges:true", "--pull=never", "--memory=512m",
                 "--cpus=1", "--user=10001:10001",
                 "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m"):
        assert flag in args
    assert "--privileged" not in args
    assert f"type=bind,source={candidate.candidate_path('run')},target=/candidate,readonly" in args
    assert len([a for a in args if a.startswith("type=bind")]) == 1
    assert str(candidate.revisions_root) not in " ".join(args)
    assert options["shell"] is False and options["timeout"] == 90
    assert args[-4:] == ["-I", "-m", "devflow.candidate_runner", "lint"]
    with pytest.raises(ValueError, match="unsupported"):
        docker_runner.DockerCandidateRunner().run(candidate, "run", "echo bad")
    assert len(calls) == 1


def test_client_timeout_removes_only_its_container(candidate, monkeypatch):
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(
                args, 90, output=b"partial\xff", stderr=b"diagnostic\xff"
            )
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(docker_runner.subprocess, "run", execute)
    report = docker_runner.DockerCandidateRunner().run(candidate, "run", "test")
    assert report.timed_out and not report.passed
    assert report.exit_code is None
    assert report.stdout == "partial\ufffd"
    assert "diagnostic\ufffd" in report.stderr
    assert "container execution timed out" in report.stderr
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "--force", name]


@pytest.mark.parametrize("cleanup_failure", ["unavailable", "timeout", "nonzero"])
def test_client_timeout_cleanup_failure_does_not_hide_report(
    candidate, monkeypatch, cleanup_failure
):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, 90, output="partial")
        if cleanup_failure == "unavailable":
            raise FileNotFoundError("docker unavailable during cleanup")
        if cleanup_failure == "timeout":
            raise subprocess.TimeoutExpired(args, 15)
        return subprocess.CompletedProcess(args, 1, "", "container still running")

    monkeypatch.setattr(docker_runner.subprocess, "run", execute)

    report = docker_runner.DockerCandidateRunner().run(candidate, "run", "test")

    assert report.timed_out and not report.passed
    assert report.stdout == "partial"
    assert "container cleanup failed" in report.stderr
    assert len(calls) == 2


def test_unavailable_docker_fails_without_host_fallback(candidate, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker unavailable")
    monkeypatch.setattr(docker_runner.subprocess, "run", unavailable)
    report = docker_runner.DockerCandidateRunner().run(candidate, "run", "lint")
    assert not report.passed and report.exit_code is None
    assert "runner unavailable" in report.stderr


def test_container_crash_is_returned_as_a_traceable_failure(candidate, monkeypatch):
    monkeypatch.setattr(
        docker_runner.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 137, "partial", "killed"),
    )

    report = docker_runner.DockerCandidateRunner().run(candidate, "run", "test")

    assert not report.passed and not report.timed_out
    assert report.exit_code == 137
    assert report.stdout == "partial"
    assert report.stderr == "killed"
    assert report.duration_ms >= 0


@pytest.mark.parametrize("stdout,exit_code", [
    ("not json", 0), (result("test").model_dump_json(), 0), (result().model_dump_json(), 1),
])
def test_invalid_container_report_is_not_accepted(candidate, monkeypatch, stdout, exit_code):
    monkeypatch.setattr(docker_runner.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, exit_code, stdout, ""))
    with pytest.raises(ValueError):
        docker_runner.DockerCandidateRunner().run(candidate, "run", "lint")


def test_dispatcher_copies_only_candidate_into_temporary_read_only_volume(tmp_path, monkeypatch):
    candidates = tmp_path / "candidates"
    candidate_path = candidates / "run-safe"
    candidate_path.mkdir(parents=True)
    (candidate_path / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(args, 0, result().model_dump_json(), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runner_dispatch.subprocess, "run", execute)
    report = runner_dispatch.DockerDispatcher(candidates, "runner:test").run(
        "run-safe", "lint"
    )

    assert report.passed
    copy = next(call for call in calls if call[:2] == ["docker", "cp"])
    assert copy[2] == f"{candidate_path}{runner_dispatch.os.sep}."
    invocation = next(call for call in calls if call[:2] == ["docker", "run"])
    assert "--network=none" in invocation
    assert "--read-only" in invocation
    mounts = [value for value in invocation if value.startswith("type=volume")]
    assert len(mounts) == 1 and mounts[0].endswith("target=/candidate,readonly")
    assert str(candidates) not in " ".join(invocation)
    assert any(call[:3] == ["docker", "volume", "rm"] for call in calls)


def test_runner_factory_uses_dispatch_socket(monkeypatch):
    monkeypatch.setenv("DEVFLOW_RUNNER_SOCKET", "/run/devflow/runner.sock")
    selected = docker_runner.candidate_runner_from_environment()
    assert isinstance(selected, docker_runner.SocketCandidateRunner)
