"""Exercise the existing production Compose smoke job through port 3000 only.

prepare creates a new disposable Git project; verify never edits that project.
The workflow supplies mock mode with an environment-only temporary override.
No provider keys, demo overlay, host runner, or backend published port is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ORIGIN = "http://127.0.0.1:3000"
TASK = "CI production Compose import and approval proof"
FIXTURE = {
    "pyproject.toml": (
        '[project]\nname = "devflow-ci-fixture"\nversion = "0.0.1"\n'
        'dependencies = []\n\n[tool.pytest.ini_options]\npythonpath = ["."]\n'
    ),
    "fixture.py": "def answer():\n    return 42\n",
    "test_fixture.py": (
        "import os\nfrom pathlib import Path\n\nfrom fixture import answer\n\n\n"
        "def test_fixture_runs_in_candidate_container():\n"
        "    assert answer() == 42\n"
        "    assert os.getuid() == 10001\n"
        "    assert Path.cwd() == Path('/candidate')\n"
        "    assert not Path('/var/run/docker.sock').exists()\n"
    ),
    "devflow_task.py": "def task_goal():\n    return 'Original committed fixture'\n",
    "test_devflow_task.py": (
        "from devflow_task import task_goal\n\n\ndef test_task_goal():\n"
        "    assert task_goal() == 'Original committed fixture'\n"
    ),
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    # The CI fixture is owned by the backend UID. Trust only this caller-supplied
    # disposable path on the host; do not change Git's global trust settings.
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        f"safe.directory={root.as_posix()}",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.autocrlf=false",
        "-c",
        "commit.gpgsign=false",
        "-C",
        str(root),
        *arguments,
    ]
    result = subprocess.run(
        command, input=data, capture_output=True, env=env, timeout=30, check=False
    )
    require(
        result.returncode == 0,
        f"Git {arguments[0]} failed: {result.stderr.decode(errors='replace')[:2000]}",
    )
    return result.stdout


def prepare(fixture: Path) -> None:
    # Refuse existing paths, including a checkout accidentally passed by a user.
    require(not os.path.lexists(fixture), "fixture path must not already exist")
    fixture.mkdir(parents=True, mode=0o755)
    for name, text in FIXTURE.items():
        path = fixture / name
        path.write_bytes(text.encode())
        path.chmod(0o644)
    git(fixture, "init", "--quiet", "--template=", "--initial-branch=main")
    git(fixture, "config", "user.name", "DevFlow CI")
    git(fixture, "config", "user.email", "devflow-ci@example.invalid")
    git(fixture, "config", "core.autocrlf", "false")
    git(fixture, "add", "--", *FIXTURE)
    git(fixture, "commit", "--quiet", "-m", "Disposable CI fixture")
    require(git(fixture, "status", "--porcelain") == b"", "fixture must be clean")
    print(f"Prepared clean Git fixture: {fixture}", flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self):
        # Do not send local test traffic through a host-configured HTTP proxy.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(
        self,
        path: str,
        *,
        method="GET",
        body=None,
        expected=200,
        local_header=True,
        origin=ORIGIN,
        timeout=15,
    ):
        headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
        if local_header:
            headers["X-DevFlow-Request"] = "1"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(
            ORIGIN + path, data=data, headers=headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read(2 * 1024 * 1024 + 1)
            require(len(payload) <= 2 * 1024 * 1024, f"oversized response from {path}")
            require(
                response.status == expected,
                f"{method} {path}: expected {expected}, got {response.status}: "
                + payload[:4000].decode(errors="replace"),
            )
            return payload, response.headers

    def json(self, path: str, **kwargs):
        data, headers = self.request(path, **kwargs)
        require(
            headers.get("X-Content-Type-Options") == "nosniff",
            f"missing nosniff: {path}",
        )
        require(
            "no-store" in headers.get("Cache-Control", ""), f"missing no-store: {path}"
        )
        return json.loads(data)


def wait_ready(api: API, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not contacted"
    while time.monotonic() < deadline:
        try:
            health = api.json(
                "/api/health", timeout=min(5, max(0.1, deadline - time.monotonic()))
            )
        except (OSError, ValueError, RuntimeError) as error:
            last_error = str(error)
            time.sleep(1)
            continue
        require(
            health.get("status") == "ok" and health.get("provider") == "mock",
            f"smoke requires explicit mock mode: {health}",
        )
        page, _ = api.request("/")
        require(b"<html" in page.lower(), "port 3000 did not serve the frontend")
        return
    raise RuntimeError(f"frontend/same-origin API did not become ready: {last_error}")


def wait_run(api: API, run_id: str, wanted: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        run = api.json(
            f"/api/runs/{run_id}",
            timeout=min(15, max(0.1, deadline - time.monotonic())),
        )
        require(run.get("run_id") == run_id, "poll returned a different run")
        state = (run.get("status"), run.get("phase"))
        if state != previous:
            print(f"{run_id}: status={state[0]} phase={state[1]}", flush=True)
            previous = state
        if state[0] == wanted:
            return run
        require(
            state[0] not in {"FAILED", "CANCELED", "CANCELLED", "REJECTED", "COMPLETE"},
            f"run stopped before {wanted}: {json.dumps(run)[:12000]}",
        )
        time.sleep(1)
    raise RuntimeError(
        f"run did not reach {wanted} within {timeout}s; last state={previous}"
    )


def source_state(root: Path) -> dict:
    # Byte/stat comparison, not a new hash algorithm or persisted baseline.
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def check_commands(run: dict) -> None:
    report = run.get("check_report") or {}
    require(report.get("passed") is True, f"candidate checks missing/failed: {report}")
    require(
        report.get("run_id") == run["run_id"]
        and report.get("patch_revision") == run["patch_revision"],
        "check report is not for the current run/patch",
    )
    expected = {
        "lint": ["/deps/venv/bin/ruff", "check", "--no-cache", "."],
        "test": [
            "/deps/venv/bin/python",
            "-I",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
    }
    for action, command in expected.items():
        result = report.get(action) or {}
        require(
            result.get("command") == command,
            f"{action} did not use the container dependency env",
        )
        require(
            result.get("passed") is True
            and result.get("exit_code") == 0
            and result.get("timed_out") is False,
            f"{action} failed: {result}",
        )
        print(f"Container {action}: {json.dumps(result)}", flush=True)
    require(
        re.search(r"\b2 passed\b", report["test"].get("stdout", "")) is not None,
        "pytest must actually execute both fixture and generated tests",
    )
    require(
        "All checks passed" in report["lint"].get("stdout", ""),
        "ruff success output missing",
    )


def check_patch(fixture: Path, commit: str, patch: dict, exported: bytes) -> None:
    expected = {name: text.encode() for name, text in FIXTURE.items()}
    changes = patch.get("files", [])
    require(
        {change["path"] for change in changes}
        == {"devflow_task.py", "test_devflow_task.py"},
        "mock must modify the two committed task files",
    )
    for change in changes:
        name = change["path"]
        require(
            change["original"].encode() == expected[name],
            "patch original differs from fixture",
        )
        require(
            isinstance(change["modified"], str),
            "mock unexpectedly deleted a fixture file",
        )
        expected[name] = change["modified"].encode()
    require(
        expected["devflow_task.py"]
        == f"def task_goal():\n    return {TASK!r}\n".encode(),
        "mock did not implement the requested task in its deterministic module",
    )
    require(b"diff --git " in exported, "download is not a native Git patch")
    with tempfile.TemporaryDirectory(prefix="devflow-ci-apply-") as temporary:
        target = Path(temporary) / "checkout"
        # A physical copy preserves the exact source commit without shared index,
        # hardlinks, ownership, hooks from another repo, or mutations to the mount.
        shutil.copytree(fixture, target)
        require(
            git(target, "rev-parse", "HEAD").decode().strip() == commit,
            "apply target has the wrong source commit",
        )
        git(target, "apply", "--check", "--whitespace=error", "-", data=exported)
        git(target, "apply", "--whitespace=error", "-", data=exported)
        actual = {
            p.relative_to(target).as_posix(): p.read_bytes()
            for p in target.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(target).parts
        }
        require(
            actual == expected,
            "applied checkout does not match the approved patch contents",
        )
        git(target, "diff", "--check")
        print(
            "Downloaded patch: git apply --check and git apply passed; exact bytes match",
            flush=True,
        )


def verify(fixture: Path, timeout: float) -> None:
    require((fixture / ".git").is_dir(), "fixture is not a Git repository")
    require(
        git(fixture, "status", "--porcelain") == b"", "mounted fixture is not clean"
    )
    commit = git(fixture, "rev-parse", "HEAD").decode().strip()
    before = source_state(fixture)
    api = API()
    run_id = None
    complete = False
    try:
        wait_ready(api, min(timeout, 90))
        preview = api.json("/api/project/preview")
        require(
            preview.get("errors") == [], f"project preview rejected fixture: {preview}"
        )
        require(
            preview.get("commit") == commit
            and sorted(preview.get("files", [])) == sorted(FIXTURE),
            f"preview is not the mounted fixture commit: {preview}",
        )
        require(
            "pyproject.toml" in preview.get("dependency_sources", []),
            "missing manifest discovery",
        )
        body = {"commit": commit, "dependency_source": "pyproject.toml", "extras": []}
        # Middleware rejection responses intentionally do not have downstream
        # success headers, so inspect these using the raw request method.
        denied, _ = api.request(
            "/api/imports", method="POST", body=body, local_header=False, expected=403
        )
        require(
            json.loads(denied).get("error", {}).get("code") == "csrf_denied",
            "write without the local request header was not denied",
        )
        denied, _ = api.request(
            "/api/imports",
            method="POST",
            body=body,
            origin="https://untrusted.invalid",
            expected=403,
        )
        require(
            json.loads(denied).get("error", {}).get("code") == "origin_denied",
            "foreign origin was not denied",
        )
        imported = api.json("/api/imports", method="POST", body=body, expected=201)
        require(imported.get("commit") == commit, "import changed the source commit")
        created = api.json(
            "/api/runs",
            method="POST",
            expected=202,
            body={
                "task": TASK,
                "import_id": imported["import_id"],
                "request_id": str(uuid4()),
            },
        )
        run_id = created["run_id"]
        run = wait_run(api, run_id, "AWAITING_APPROVAL", timeout)
        require(
            run.get("provider") == "mock"
            and run.get("import_id") == imported["import_id"],
            "run lost its mock provider/import identity",
        )
        check_commands(run)
        patch = api.json(f"/api/runs/{run_id}/patch")
        require(
            patch.get("run_id") == run_id
            and patch.get("patch_revision") == run["patch_revision"],
            "patch endpoint returned a different run/revision",
        )
        api.request(f"/api/runs/{run_id}/patch/download", expected=409)
        api.json(
            f"/api/runs/{run_id}/approve",
            method="POST",
            body={"decision_id": str(uuid4()), "patch_revision": run["patch_revision"]},
        )
        finished = wait_run(api, run_id, "COMPLETE", timeout)
        require(
            finished.get("source_commit") == commit,
            "completed run lost its source commit",
        )
        complete = True
        exported, headers = api.request(f"/api/runs/{run_id}/patch/download")
        require(
            headers.get("X-DevFlow-Source-Commit") == commit,
            "wrong download source commit",
        )
        require(headers.get_content_type() == "text/x-diff", "wrong patch content type")
        require(
            "attachment;" in headers.get("Content-Disposition", ""),
            "patch is not an attachment",
        )
        check_patch(fixture, commit, patch, exported)
        print(
            "Production Compose mock smoke passed via http://127.0.0.1:3000", flush=True
        )
    finally:
        if run_id is not None and not complete:
            try:
                api.request(f"/api/runs/{run_id}/stop", method="POST")
            except (OSError, RuntimeError) as error:
                print(f"Could not stop failed smoke run: {error}", flush=True)
        require(
            source_state(fixture) == before,
            "smoke changed the mounted original repository",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify"])
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=360)
    args = parser.parse_args()
    require(0 < args.timeout <= 900, "timeout must be between 0 and 900 seconds")
    fixture = args.fixture.absolute()
    if args.action == "prepare":
        prepare(fixture)
    else:
        verify(fixture, args.timeout)


if __name__ == "__main__":
    main()
