"""Real spawned-worker API tests. Runners are explicit doubles, not Docker proof."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient
from support import PassingRunner

from devflow.main import create_app
from devflow.mock_provider import DeterministicMockProvider


class PreparedRunner(PassingRunner):
    def prepare(self, workspace, run_id, requirements):
        return {"test_double": True, "requirements": requirements}

    def stop(self, run_id):
        return True


class SlowProvider(DeterministicMockProvider):
    def plan(self, task):
        time.sleep(60)
        return super().plan(task)


class FailingProvider(DeterministicMockProvider):
    def plan(self, task):
        raise ValueError("sensitive-remote-error-do-not-echo")


class UnavailableCleanup(PreparedRunner):
    def stop(self, run_id):
        return False


def wait_status(client, run_id, statuses, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(.05)
    raise AssertionError(run)


def app_for(tmp_path, **kwargs):
    return create_app(tmp_path / "data.sqlite", runner=kwargs.pop("runner", PreparedRunner()),
                      mode="mock", security_enabled=False, **kwargs)


def test_async_immediate_idempotent_stop_and_history(tmp_path):
    app = app_for(tmp_path, provider=SlowProvider())
    with TestClient(app) as client:
        body = {"task": "slow task", "request_id": "stable-request"}
        started = time.monotonic()
        created = client.post("/api/runs", json=body)
        assert created.status_code == 202
        assert time.monotonic() - started < 5
        run_id = created.json()["run_id"]
        assert created.json()["status"] == "RUNNING"
        assert client.post("/api/runs", json=body).json()["run_id"] == run_id
        assert client.post("/api/runs", json={**body, "task": "different"}).status_code == 409
        assert client.post("/api/runs", json={**body, "request_id": "new"}).status_code == 409
        assert client.delete("/api/settings/provider").status_code == 409
        assert client.get(f"/api/runs/{run_id}/patch/download").status_code == 409
        assert client.post(f"/api/runs/{run_id}/stop").json()["stop_requested"]
        assert client.post(f"/api/runs/{run_id}/stop").status_code == 200
        wait_status(client, run_id, {"CANCELED"})
        assert not app.state.supervisor.process.is_alive()
        assert client.get("/api/runs/active").json() is None
        assert client.get("/api/runs").json()["runs"][0]["run_id"] == run_id


def test_failure_visible_and_sanitized(tmp_path):
    with TestClient(app_for(tmp_path, provider=FailingProvider())) as client:
        run_id = client.post("/api/runs", json={"task": "fail", "request_id": "fail"}).json()["run_id"]
        failed = wait_status(client, run_id, {"FAILED"})
        assert failed["error"]["phase"] == "plan"
        assert failed["error"]["code"] == "stage_failed"
        history = client.get("/api/runs")
        assert "sensitive-remote-error" not in history.text
        assert client.get("/api/runs/active").json() is None


def git(root: Path, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def test_import_approve_download_apply_and_next_run_isolation(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "core.autocrlf", "false")
    (repository / "original.py").write_bytes(b"answer = 42\n")
    git(repository, "add", ".")
    git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    original = git(repository, "status", "--porcelain=v1")
    app = app_for(tmp_path, project_path=repository)
    with TestClient(app) as client:
        preview = client.get("/api/project/preview").json()
        assert not preview["errors"]
        imported = client.post("/api/imports", json={"commit": preview["commit"]})
        assert imported.status_code == 201, imported.text
        body = {"task": "new function", "import_id": imported.json()["import_id"], "request_id": "first"}
        created = client.post("/api/runs", json=body)
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        ready = wait_status(client, run_id, {"AWAITING_APPROVAL", "FAILED"})
        assert ready["status"] == "AWAITING_APPROVAL", ready
        decision = {"decision_id": "approve-first", "patch_revision": 1}
        approved = client.post(f"/api/runs/{run_id}/approve", json=decision)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "COMPLETE"
        patch = client.get(f"/api/runs/{run_id}/patch/download")
        assert patch.status_code == 200
        assert patch.headers["x-devflow-source-commit"] == preview["commit"]
        assert patch.content == client.get(f"/api/runs/{run_id}/patch/download").content
        independent = tmp_path / "copy"
        git(tmp_path, "clone", "--no-local", str(repository), str(independent))
        patch_file = tmp_path / "result.patch"
        patch_file.write_bytes(patch.content)
        git(independent, "apply", "--check", str(patch_file))
        git(independent, "apply", str(patch_file))
        assert (independent / "devflow_task.py").exists()
        assert git(repository, "status", "--porcelain=v1") == original == b""
        assert not (repository / "devflow_task.py").exists()
        second = client.post("/api/runs", json={**body, "request_id": "second", "task": "another function"}).json()
        other_workspace = app.state.coordinator.workspace_for(second["run_id"])
        assert other_workspace.read_revision(0) == {"original.py": "answer = 42\n"}
        wait_status(client, second["run_id"], {"AWAITING_APPROVAL"})
    with TestClient(app_for(tmp_path, project_path=repository)) as restarted:
        assert restarted.get(f"/api/runs/{run_id}/patch/download").content == patch.content


def test_cleanup_failure_retains_active_slot_across_restart(tmp_path):
    app = app_for(tmp_path, runner=UnavailableCleanup(), provider=FailingProvider())
    with TestClient(app) as client:
        run_id = client.post("/api/runs", json={"task": "fail", "request_id": "cleanup"}).json()["run_id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run.get("cleanup_pending"):
                break
            time.sleep(.05)
        assert run["cleanup_pending"]
        assert run["status"] == "RUNNING"
    with TestClient(app_for(tmp_path, runner=UnavailableCleanup())) as restarted:
        assert restarted.get("/api/runs/active").json()["run_id"] == run_id
    with TestClient(app_for(tmp_path)) as recovered:
        assert recovered.get("/api/runs/active").json() is None
        assert recovered.get(f"/api/runs/{run_id}").json()["status"] == "FAILED"


def test_security_and_no_validation_secret_echo(tmp_path):
    app = create_app(tmp_path / "security.sqlite", mode="mock", runner=PreparedRunner(), security_enabled=True)
    with TestClient(app, base_url="http://127.0.0.1:3000") as client:
        assert client.get("/api/health", headers={"host": "attacker.example"}).status_code == 403
        assert client.get("/api/health", headers={"origin": "http://attacker.example"}).status_code == 403
        assert client.delete("/api/settings/provider").status_code == 403
        assert client.delete("/api/settings/provider", headers={"x-devflow-request": "1", "sec-fetch-site": "cross-site"}).status_code == 403
        secret = "private-key-never-return" * 300
        response = client.put("/api/settings/provider", headers={"x-devflow-request": "1"},
                              json={"base_url": "https://example.com/v1", "model": "test", "api_key": secret})
        assert response.status_code == 422
        assert "private-key-never-return" not in response.text
        assert not client.get("/api/settings/provider").json()["key_configured"]
