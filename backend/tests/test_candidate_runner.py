from __future__ import annotations

import sys
import time

import pytest

from devflow import candidate_runner
from devflow.workspace import CandidateBoundaryError


def test_only_named_actions_are_accepted() -> None:
    assert candidate_runner.action_command("lint")[1:] == ["check", "--no-cache", "."]
    with pytest.raises(ValueError, match="unsupported"):
        candidate_runner.action_command("sh -c whoami")


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
