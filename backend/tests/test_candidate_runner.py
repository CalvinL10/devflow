from __future__ import annotations

import os
import sys
import time

import pytest

from devflow import candidate_runner
from devflow.workspace import CandidateBoundaryError


def test_only_named_actions_are_accepted() -> None:
    assert candidate_runner.action_command("lint")[1:] == ["check", "--no-cache", "."]
    with pytest.raises(ValueError, match="unsupported"):
        candidate_runner.action_command("sh -c whoami")


@pytest.mark.parametrize("layout", ["root-module", "root-package", "src-package", "src-namespace"])
@pytest.mark.parametrize("import_mode", ["prepend", "importlib"])
def test_real_pytest_imports_candidate_layout_without_install(tmp_path, monkeypatch,
                                                             layout, import_mode):
    root = tmp_path / "candidate"
    root.mkdir()
    source = root / "src" if layout.startswith("src-") else root
    source.mkdir(exist_ok=True)
    if layout == "root-module":
        (source / "layout_app.py").write_text("VALUE = 42\n", encoding="utf-8")
        statement = "from layout_app import VALUE"
    else:
        package = source / "layout_app"
        package.mkdir()
        if layout != "src-namespace":
            (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "core.py").write_text("VALUE = 42\n", encoding="utf-8")
        statement = "from layout_app.core import VALUE"
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_layout.py").write_text(
        f"{statement}\nimport sys\ndef test_value():\n"
        "    assert VALUE == 42\n    assert sys.flags.isolated == 1\n",
        encoding="utf-8",
    )
    (root / "pytest.ini").write_text(
        f"[pytest]\naddopts = --import-mode={import_mode}\ntestpaths = tests\n",
        encoding="utf-8",
    )
    # Local verification changes ONLY the interpreter path, not the launch code.
    command = candidate_runner.action_command("test")
    command[0] = sys.executable
    monkeypatch.setitem(candidate_runner.ALLOWED_ACTIONS, "test", command)
    monkeypatch.delenv("DEVFLOW_DEPENDENCY_ENV", raising=False)
    bounded = candidate_runner._run_bounded

    def local_subprocess(command, **kwargs):
        # Linux containers do not need Windows' system DLL lookup environment.
        if os.name == "nt":
            kwargs["env"]["SystemRoot"] = os.environ["SystemRoot"]
        return bounded(command, **kwargs)

    monkeypatch.setattr(candidate_runner, "_run_bounded", local_subprocess)
    report = candidate_runner.execute("test", candidate_root=root, timeout_seconds=10)
    assert report.passed, report.stdout + report.stderr


def test_prepared_actions_use_only_the_separate_dependency_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVFLOW_DEPENDENCY_ENV", "1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-forward")
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return 0, b"ok", b"", False, False

    monkeypatch.setattr(candidate_runner, "_run_bounded", execute)
    assert candidate_runner.execute("test", candidate_root=tmp_path).passed
    command, options = calls[0]
    assert command[0] == "/deps/venv/bin/python"
    assert "-I" in command
    assert options["env"]["PATH"].startswith("/deps/venv/bin:")
    assert "AWS_SECRET_ACCESS_KEY" not in options["env"]
    assert candidate_runner.action_command("lint", dependency_env=True)[0] == "/deps/venv/bin/ruff"


def test_command_runs_in_candidate_and_does_not_change_workspace(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    candidate = tmp_path / "candidate"
    workspace.mkdir()
    candidate.mkdir()
    original = workspace / "tracked.txt"
    original.write_text("unchanged", encoding="utf-8")
    command = [
        sys.executable,
        "-I",
        "-c",
        "from pathlib import Path; Path('candidate-only.txt').write_text('ok'); print(Path.cwd())",
    ]
    monkeypatch.setitem(candidate_runner.ALLOWED_ACTIONS, "test", command)

    report = candidate_runner.execute("test", candidate_root=candidate, timeout_seconds=5)

    assert report.passed is True
    assert str(candidate.resolve()) in report.stdout
    assert (candidate / "candidate-only.txt").read_text(encoding="utf-8") == "ok"
    assert original.read_text(encoding="utf-8") == "unchanged"


def test_timeout_is_reported(tmp_path, monkeypatch) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setitem(
        candidate_runner.ALLOWED_ACTIONS,
        "test",
        [sys.executable, "-I", "-c", "import time; time.sleep(10)"],
    )
    report = candidate_runner.execute("test", candidate_root=candidate, timeout_seconds=1)
    assert report.passed is False
    assert report.timed_out is True
    assert report.exit_code is None


def test_timeout_does_not_wait_for_descendant_inheriting_output(
    tmp_path, monkeypatch
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setitem(
        candidate_runner.ALLOWED_ACTIONS,
        "test",
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-I', '-c', "
                "'import time; time.sleep(10)']); "
                "print('spawned', flush=True); time.sleep(10)"
            ),
        ],
    )

    started = time.monotonic()
    report = candidate_runner.execute("test", candidate_root=candidate, timeout_seconds=1)

    assert time.monotonic() - started < 4
    assert report.timed_out is True


def test_output_limit_terminates_candidate_process(tmp_path, monkeypatch) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setitem(
        candidate_runner.ALLOWED_ACTIONS,
        "test",
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys,time; "
                f"sys.stdout.write('x' * {candidate_runner.MAX_CAPTURE_CHARS * 2}); "
                "sys.stdout.flush(); time.sleep(10)"
            ),
        ],
    )

    started = time.monotonic()
    report = candidate_runner.execute("test", candidate_root=candidate, timeout_seconds=8)

    assert time.monotonic() - started < 5
    assert report.passed is False
    assert report.timed_out is False
    assert report.exit_code is None
    assert len(report.stdout) <= candidate_runner.MAX_CAPTURE_CHARS
    assert "output limit exceeded" in report.stderr


def test_timeout_output_decodes_invalid_utf8(tmp_path, monkeypatch):
    monkeypatch.setitem(
        candidate_runner.ALLOWED_ACTIONS,
        "test",
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import os,time; os.write(1, b'partial\\xff'); "
                "os.write(2, b'error\\xff'); time.sleep(10)"
            ),
        ],
    )

    report = candidate_runner.execute("test", candidate_root=tmp_path, timeout_seconds=1)

    assert report.timed_out and not report.passed
    assert report.stdout == "partial\ufffd" and report.stderr == "error\ufffd"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_nested_links_are_rejected_before_execution(tmp_path, monkeypatch, kind):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    linked = candidate / "escape.txt"
    try:
        if kind == "symlink":
            linked.symlink_to(outside)
        else:
            linked.hardlink_to(outside)
    except OSError as error:
        pytest.skip(f"{kind} creation is not available: {error}")
    monkeypatch.setitem(
        candidate_runner.ALLOWED_ACTIONS,
        "test",
        [sys.executable, "-I", "-c", "print('must not run')"],
    )

    with pytest.raises(CandidateBoundaryError, match="symbolic link|hard-linked"):
        candidate_runner.execute("test", candidate_root=candidate, timeout_seconds=5)
