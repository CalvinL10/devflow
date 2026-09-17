from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from devflow.models import CommandResult
from devflow.workspace import ManagedWorkspace

CANDIDATE_ROOT = Path("/candidate")
MAX_CAPTURE_CHARS = 100_000
OUTPUT_LIMIT_MESSAGE = "candidate output limit exceeded; process terminated"
READER_JOIN_SECONDS = 1
ALLOWED_ACTIONS: dict[str, list[str]] = {
    "lint": ["/app/.venv/bin/ruff", "check", "--no-cache", "."],
    "test": [
        "/app/.venv/bin/python",
        "-I",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ],
}


def action_command(action: str) -> list[str]:
    try:
        return list(ALLOWED_ACTIONS[action])
    except KeyError as error:
        raise ValueError(f"unsupported candidate action: {action}") from error


def execute(action: str, *, candidate_root: Path = CANDIDATE_ROOT, timeout_seconds: int = 60) -> CommandResult:
    for path in (candidate_root, *candidate_root.parents):
        ManagedWorkspace._reject_link(path)
    root = candidate_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("candidate root must be a directory")
    ManagedWorkspace._reject_links(root)
    command = action_command(action)
    environment = {
        "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DEVFLOW_LLM_PROVIDER": "mock",
        "LANGGRAPH_STRICT_MSGPACK": "true",
    }
    started = time.monotonic()
    timed_out = False
    output_limited = False
    exit_code: int | None
    completed, stdout_bytes, stderr_bytes, timed_out, output_limited = _run_bounded(
        command,
        cwd=root,
        env=environment,
        timeout_seconds=timeout_seconds,
    )
    stdout = captured_text(stdout_bytes)
    stderr = captured_text(stderr_bytes)
    if output_limited:
        stderr = captured_text(f"{stderr}\n{OUTPUT_LIMIT_MESSAGE}")
    exit_code = None if timed_out or output_limited else completed
    duration_ms = round((time.monotonic() - started) * 1000)
    return CommandResult(
        passed=exit_code == 0 and not timed_out,
        command=command,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        exit_code=exit_code,
        timed_out=timed_out,
    )


def _run_bounded(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout_seconds: int
) -> tuple[int, bytes, bytes, bool, bool]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    stdout = bytearray()
    stderr = bytearray()
    output_limited = threading.Event()

    def drain(stream, capture: bytearray) -> None:
        total = 0
        while chunk := stream.read(8192):
            total += len(chunk)
            capture.extend(chunk)
            if len(capture) > MAX_CAPTURE_CHARS:
                del capture[:-MAX_CAPTURE_CHARS]
            if total > MAX_CAPTURE_CHARS and not output_limited.is_set():
                output_limited.set()
                _kill_process(process)

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process(process)
        process.wait()
    finally:
        for reader in readers:
            reader.join(timeout=READER_JOIN_SECONDS)

    return process.returncode, bytes(stdout), bytes(stderr), timed_out, output_limited.is_set()


def _kill_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=3,
                check=False,
            )
    except OSError:
        pass
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def captured_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[-MAX_CAPTURE_CHARS:]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: candidate-runner <lint|test>")
    timeout = int(os.environ.get("DEVFLOW_RUNNER_TIMEOUT_SECONDS", "60"))
    if timeout < 1 or timeout > 300:
        raise SystemExit("DEVFLOW_RUNNER_TIMEOUT_SECONDS must be between 1 and 300")
    try:
        report = execute(sys.argv[1], timeout_seconds=timeout)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True))
        raise SystemExit(2) from error
    print(report.model_dump_json())
    raise SystemExit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
