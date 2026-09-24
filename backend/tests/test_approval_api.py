from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest
from fastapi.testclient import TestClient
from support import PassingRunner

import devflow.coordinator as coordinator_module
from devflow.main import create_app as _create_app
from devflow.mock_provider import DeterministicMockProvider

create_app = partial(_create_app, asynchronous=False, mode="mock", security_enabled=False)


def app_for(tmp_path):
    return create_app(tmp_path / "state.sqlite", runner=PassingRunner())


@pytest.mark.parametrize(("kind", "status", "revision"), [
    ("approve", "COMPLETE", 1), ("reject", "REJECTED", 0), ("cancel", "CANCELED", 0),
])
def test_decisions_publish_only_approval_and_retry_once(tmp_path, kind, status, revision):
    with TestClient(app_for(tmp_path)) as client:
        coordinator = client.app.state.coordinator
        started = client.post("/api/runs", json={"task": "add deterministic example"}).json()
        run_id = started["run_id"]
        assert started["status"] == "AWAITING_APPROVAL"
        assert coordinator.workspace.read_revision(0) == {}
        request = {"decision_id": "choice-1", "patch_revision": 1, "feedback": "reviewed"}
        response = client.post(f"/api/runs/{run_id}/{kind}", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == status
        assert result["workspace_revision"] == revision
        assert client.post(f"/api/runs/{run_id}/{kind}", json=request).json() == result
        assert coordinator.workspace.read_revision(0) == {}
        if kind == "approve":
            patch = coordinator.database.current_patch(run_id)
            expected = {f["path"]: f["modified"] for f in json.loads(patch["patch_json"])["files"]}
            assert coordinator.workspace.read_revision(1) == expected
        else:
            assert not coordinator.workspace.revision_path(1).exists()
        assert client.post(f"/api/runs/{run_id}/resume", json={"decision_id": "choice-1"}).json() == result
        assert client.post(f"/api/runs/{run_id}/{kind}", json={**request, "feedback": "changed"}).status_code == 409
        for other in {"approve", "reject", "cancel"} - {kind}:
            assert client.post(f"/api/runs/{run_id}/{other}", json={**request, "decision_id": other}).status_code == 409


def test_pending_decision_is_exposed_and_original_can_be_resumed_after_transient_failure(
    tmp_path, monkeypatch
):
    original_resume = coordinator_module.invoke_resume
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient checkpoint failure")
        return original_resume(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "invoke_resume", fail_once)
    with TestClient(app_for(tmp_path), raise_server_exceptions=False) as client:
        started = client.post("/api/runs", json={"task": "recover approval"}).json()
        run_id = started["run_id"]
        request = {"decision_id": "persisted-choice", "patch_revision": 1, "feedback": "ship it"}

        failed = client.post(f"/api/runs/{run_id}/approve", json=request)
        assert failed.status_code == 500
        snapshot = client.get(f"/api/runs/{run_id}").json()
        assert snapshot["status"] == "APPLYING"
        assert snapshot["last_decision"] is None
        assert snapshot["pending_decision"] == {
            "decision_id": "persisted-choice",
            "kind": "approve",
            "patch_revision": 1,
            "feedback": "ship it",
        }

        resumed = client.post(
            f"/api/runs/{run_id}/resume", json={"decision_id": "persisted-choice"}
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "COMPLETE"
        assert client.get(f"/api/runs/{run_id}").json()["pending_decision"] is None


def test_active_run_endpoint_finds_the_single_active_run(tmp_path):
    with TestClient(app_for(tmp_path)) as client:
        assert client.get("/api/runs/active").json() is None
        started = client.post("/api/runs", json={"task": "recover active run"}).json()

        response = client.get("/api/runs/active")
        assert response.status_code == 200
        active = response.json()
        assert active["run_id"] == started["run_id"]
        assert active["status"] == "AWAITING_APPROVAL"
        assert active["task"] == "recover active run"


@pytest.mark.parametrize("kind", ["approve", "reject", "cancel"])
def test_stale_revision_is_409_and_does_not_record_a_decision(tmp_path, kind):
    with TestClient(app_for(tmp_path)) as client:
        run_id = client.post("/api/runs", json={"task": "example"}).json()["run_id"]
        request = {"decision_id": "stale", "patch_revision": 2}
        response = client.post(f"/api/runs/{run_id}/{kind}", json=request)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "revision_conflict"
        with client.app.state.coordinator.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


@pytest.mark.parametrize("decision_id", ["team/choice", "team\\choice", "../choice", "NUL", "x" * 128, "团队/选择"])
def test_decision_id_remains_an_opaque_key_not_a_path(tmp_path, decision_id):
    with TestClient(app_for(tmp_path), raise_server_exceptions=False) as client:
        run_id = client.post("/api/runs", json={"task": "example"}).json()["run_id"]
        request = {"decision_id": decision_id, "patch_revision": 1}
        result = client.post(f"/api/runs/{run_id}/approve", json=request)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "COMPLETE"
        assert result.json()["workspace_revision"] == 1
        assert client.post(f"/api/runs/{run_id}/resume", json={"decision_id": decision_id}).json() == result.json()
        assert client.post(f"/api/runs/{run_id}/approve", json=request).json() == result.json()
        revisions = client.app.state.coordinator.workspace.revisions_root
        assert sorted(p.name for p in revisions.iterdir()) == ["00000000", "00000001"]


@pytest.mark.parametrize("stage", ["plan", "review"])
def test_running_cancel_is_explicit_conflict_before_and_after_code(tmp_path, stage):
    entered, release = threading.Event(), threading.Event()

    class PausingProvider(DeterministicMockProvider):
        def plan(self, *args):
            if stage == "plan":
                entered.set()
                assert release.wait(10)
            return super().plan(*args)

        def review(self, *args):
            if stage == "review":
                entered.set()
                assert release.wait(10)
            return super().review(*args)

    app = create_app(tmp_path / "state.sqlite", runner=PassingRunner(), provider=PausingProvider())
    with TestClient(app, raise_server_exceptions=False) as client, ThreadPoolExecutor() as executor:
        creating = executor.submit(client.post, "/api/runs", json={"task": "example"})
        try:
            assert entered.wait(10)
            with app.state.coordinator.database.connect() as connection:
                run_id = connection.execute("SELECT id FROM runs").fetchone()[0]
            response = client.post(f"/api/runs/{run_id}/cancel", json={"decision_id": "early", "patch_revision": 1})
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "invalid_run_transition"
            assert client.get(f"/api/runs/{run_id}").json()["status"] == "RUNNING"
            with app.state.coordinator.database.connect() as connection:
                assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
        finally:
            release.set()
        assert creating.result(timeout=10).json()["status"] == "AWAITING_APPROVAL"
        response = client.post(f"/api/runs/{run_id}/cancel", json={"decision_id": "early", "patch_revision": 1})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "CANCELED"


@pytest.mark.parametrize("kind", ["approve", "reject", "cancel"])
@pytest.mark.parametrize("feedback", ["请补充边界测试 <script>alert(1)</script>", None])
def test_completed_decision_feedback_survives_api_restart(tmp_path, kind, feedback):
    expected = {
        "decision_id": "recorded-feedback", "kind": kind,
        "patch_revision": 1, "feedback": feedback,
    }
    with TestClient(app_for(tmp_path)) as client:
        started = client.post("/api/runs", json={"task": "retain review feedback"}).json()
        run_id = started["run_id"]
        assert started["last_decision"] is None
        response = client.post(f"/api/runs/{run_id}/{kind}", json={
            "decision_id": "recorded-feedback", "patch_revision": 1, "feedback": feedback,
        })
        assert response.status_code == 200, response.text
        assert response.json()["pending_decision"] is None
        assert response.json()["last_decision"] == expected
        assert client.get(f"/api/runs/{run_id}").json()["last_decision"] == expected
    with TestClient(app_for(tmp_path)) as restarted:
        snapshot = restarted.get(f"/api/runs/{run_id}").json()
        assert snapshot["pending_decision"] is None
        assert snapshot["last_decision"] == expected
