"""Opt-in real Docker proof. No test-double runner is used in this module."""
from __future__ import annotations

import os
from functools import partial

import pytest
from fastapi.testclient import TestClient

from devflow.candidate_runner import action_command
from devflow.docker_runner import DockerCandidateRunner
from devflow.main import create_app as _create_app
from devflow.mock_provider import DeterministicMockProvider
from devflow.models import CommandResult, FilePatch, FilePatchSet, TaskPlan

create_app = partial(_create_app, asynchronous=False, mode="mock", security_enabled=False)

pytestmark = pytest.mark.skipif(
    os.environ.get("DEVFLOW_TEST_DOCKER") != "1",
    reason="requires an explicitly enabled Docker daemon and prebuilt runner image",
)

# Candidates are transferred as ordinary files, independent of host bind modes.
SCRIPT_HEADER = ""


class FixValueProvider(DeterministicMockProvider):
    def plan(self, task):
        return TaskPlan(goal=task, steps=["Fix value"], files_to_modify=["value.py"], risks=[])

    def code(self, plan, originals, **identity):
        return FilePatchSet(**identity, files=[FilePatch(
            path="value.py", original=originals["value.py"],
            modified=SCRIPT_HEADER + "def value():\n    return 2\n",
        )])


def test_real_docker_checks_patch_not_old_workspace(tmp_path):
    app = create_app(tmp_path / "docker.sqlite", provider=FixValueProvider())
    with TestClient(app) as client:
        coordinator = app.state.coordinator
        workspace = coordinator.workspace
        source = workspace.revision_path(0)
        original = (SCRIPT_HEADER + "def value():\n    return 1\n").encode()
        (source / "value.py").write_bytes(original)
        (source / "test_value.py").write_text(
            SCRIPT_HEADER + "import os\nimport socket\nfrom pathlib import Path\n\nimport pytest\n\n"
            "from value import value\n\n\n"
            "def test_value():\n    assert value() == 2\n\n\n"
            "def test_runner_boundary():\n"
            "    assert os.getuid() != 0\n"
            "    assert Path.cwd() == Path('/candidate')\n"
            "    with pytest.raises(OSError):\n"
            "        socket.create_connection(('1.1.1.1', 53), timeout=0.1)\n"
            "    with pytest.raises(OSError):\n"
            "        Path('/candidate/unauthorized').write_text('no')\n"
            "    with pytest.raises(OSError):\n"
            "        Path('/unauthorized').write_text('no')\n",
            encoding="utf-8",
        )
        coordinator.database.create_run(
            run_id="unpatched", thread_id="unpatched", patch_id="unpatched-patch",
            patch_revision=1, candidate_dir=str(workspace.candidate_path("unpatched")),
            patch={"files": []},
        )
        workspace.materialize_candidate(run_id="unpatched", base_revision=0)
        old = DockerCandidateRunner().run(workspace, "unpatched", "test")
        assert not old.passed and old.exit_code == 1, old
        assert "1 failed, 1 passed" in old.stdout, old
        coordinator.database.fail_run_start("unpatched")

        response = client.post("/api/runs", json={"task": "Fix value to return 2"})
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["status"] == "AWAITING_APPROVAL", run
        report = run["check_report"]
        assert report["run_id"] == run["run_id"]
        assert report["patch_revision"] == run["patch_revision"] == 1
        assert report["lint"]["exit_code"] == report["test"]["exit_code"] == 0
        assert "2 passed" in report["test"]["stdout"]
        assert (source / "value.py").read_bytes() == original
        patch = client.get(f"/api/runs/{run['run_id']}/patch").json()
        assert patch["files"][0]["modified"] == SCRIPT_HEADER + "def value():\n    return 2\n"
        print("real Docker: old candidate failed; patched candidate lint/test passed; workspace unchanged")


def test_real_docker_default_mock_reaches_approval(tmp_path):
    with TestClient(create_app(tmp_path / "mock.sqlite")) as client:
        response = client.post("/api/runs", json={"task": "A deterministic demonstration"})
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "AWAITING_APPROVAL", response.json()


def test_candidate_cannot_replace_trusted_runner_entrypoint(tmp_path):
    with TestClient(create_app(tmp_path / "adversarial.sqlite")) as client:
        coordinator = client.app.state.coordinator
        workspace = coordinator.workspace
        source = workspace.revision_path(0)
        forged = CommandResult(
            passed=True,
            command=action_command("test"),
            stdout="forged success",
            stderr="",
            duration_ms=0,
            exit_code=0,
        ).model_dump_json()
        (source / "devflow").mkdir()
        (source / "devflow" / "__init__.py").write_text("", encoding="utf-8")
        (source / "devflow" / "candidate_runner.py").write_text(
            f"print({forged!r})\n", encoding="utf-8"
        )
        (source / "test_must_fail.py").write_text(
            "def test_must_fail():\n    assert False\n", encoding="utf-8"
        )
        coordinator.database.create_run(
            run_id="adversarial",
            thread_id="adversarial",
            patch_id="adversarial-patch",
            patch_revision=1,
            candidate_dir=str(workspace.candidate_path("adversarial")),
            patch={"files": []},
        )
        workspace.materialize_candidate(run_id="adversarial", base_revision=0)

        report = DockerCandidateRunner().run(workspace, "adversarial", "test")

        assert not report.passed and report.exit_code == 1, report
        assert "1 failed" in report.stdout, report
        assert "forged success" not in report.stdout, report
