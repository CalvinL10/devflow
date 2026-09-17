from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from support import PassingRunner

from devflow.coordinator import RunCoordinator
from devflow.errors import InvalidRunTransition
from devflow.main import create_app
from devflow.mock_provider import DeterministicMockProvider
from devflow.models import CommandResult, DecisionKind, FilePatch, FilePatchSet, ReviewFinding
from devflow.workflow import build_graph, open_graph


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class InspectingRunner(PassingRunner):
    def __init__(self):
        self.observed = []

    def run(self, workspace, run_id, action):
        candidate = workspace.require_materialized_candidate(run_id, workspace.candidate_path(run_id))
        self.observed.append((action, tree(candidate)))
        return super().run(workspace, run_id, action)


def test_http_run_reaches_approval_with_persisted_current_candidate(tmp_path):
    database = tmp_path / "api.sqlite"
    runner = InspectingRunner()
    app = create_app(database, runner=runner)
    with TestClient(app) as client:
        workspace = app.state.coordinator.workspace
        (workspace.revision_path(0) / "tracked.txt").write_text("unchanged")
        before = tree(workspace.revisions_root)
        created = client.post("/api/runs", json={"task": "record my task"})
        assert created.status_code == 201
        run = created.json()
        assert run["status"] == "AWAITING_APPROVAL"
        patch_response = client.get(f"/api/runs/{run['run_id']}/patch")
        patch = FilePatchSet.model_validate(patch_response.json())
        assert patch.files
        assert patch.run_id == run["run_id"]
        assert patch.patch_revision == run["patch_revision"] == 1
        assert run["check_report"]["run_id"] == run["run_id"]
        assert run["check_report"]["patch_revision"] == 1
        assert run["review_report"]["patch_revision"] == 1
        assert [action for action, _ in runner.observed] == ["lint", "test"]
        for _, contents in runner.observed:
            for file in patch.files:
                assert contents[file.path] == file.modified.encode()
        assert tree(workspace.revisions_root) == before
        events = app.state.coordinator.database.list_events(run["run_id"])
        assert [e["node"] for e in events if e["type"] == "node.started"] == [
            "plan", "code", "materialize", "lint_test", "review"
        ]
        assert run["event_seqs"] == list(range(1, len(events) + 1))
        assert client.post("/api/runs", json={"task": "second"}).status_code == 409
        with open_graph(database) as checkpointer:
            state = build_graph(checkpointer).get_state({"configurable": {"thread_id": run["thread_id"]}})
        assert state.next == ("await_approval",)
        assert state.values["check_report"] == run["check_report"]

    with TestClient(create_app(database, runner=PassingRunner())) as restarted:
        assert restarted.get(f"/api/runs/{run['run_id']}").json() == run
        assert restarted.get(f"/api/runs/{run['run_id']}/patch").json() == patch.model_dump(mode="json")


@pytest.mark.parametrize("task", ["", "   ", "x" * 10001, None, 12], ids=["empty", "blank", "long", "null", "number"])
def test_invalid_task_is_not_created(tmp_path, task):
    with TestClient(create_app(tmp_path / "api.sqlite", runner=PassingRunner())) as client:
        assert client.post("/api/runs", json={"task": task}).status_code == 422


def test_request_cannot_supply_paths_patches_or_runner_commands(tmp_path):
    with TestClient(create_app(tmp_path / "api.sqlite", runner=PassingRunner())) as client:
        for key in ("candidate_dir", "patch", "command", "run_id"):
            assert client.post("/api/runs", json={"task": "test", key: "outside"}).status_code == 422
        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/runs/missing/patch").status_code == 404


@pytest.mark.parametrize("action", ["lint", "test"])
@pytest.mark.parametrize("timeout", [False, True])
def test_failed_check_never_reviews_or_allows_approval(tmp_path, action, timeout):
    class FailingRunner(PassingRunner):
        def run(self, workspace, run_id, name):
            report = super().run(workspace, run_id, name)
            if name == action:
                return CommandResult(**{
                    **report.model_dump(), "passed": False,
                    "exit_code": None if timeout else 1, "timed_out": timeout,
                })
            return report

    coordinator = RunCoordinator(tmp_path / "failed.sqlite", runner=FailingRunner())
    result = coordinator.start(run_id="failed", patch_id="patch")
    assert result["status"] == "FAILED"
    assert result["check_report"]["passed"] is False
    assert "review_report" not in result
    assert not any(event["type"] == "run.interrupted" for event in coordinator.database.list_events("failed"))
    with pytest.raises(InvalidRunTransition):
        coordinator.database.mark_awaiting_approval("failed")
    with pytest.raises(InvalidRunTransition):
        coordinator.decide(run_id="failed", patch_revision=1, decision_id="d", kind=DecisionKind.APPROVE)
    assert RunCoordinator(coordinator.database.path).snapshot("failed")["status"] == "FAILED"


@pytest.mark.parametrize(
    "exit_code,timed_out",
    [(137, False), (None, True)],
    ids=["container-crash", "timeout"],
)
def test_runner_failure_is_persisted_and_api_remains_available(
    tmp_path, exit_code, timed_out
):
    class BrokenRunner(PassingRunner):
        def run(self, workspace, run_id, action):
            report = super().run(workspace, run_id, action)
            if action != "lint":
                return report
            return CommandResult(
                passed=False,
                command=report.command,
                stdout="partial output",
                stderr="runner stopped",
                duration_ms=123,
                exit_code=exit_code,
                timed_out=timed_out,
            )

    with TestClient(create_app(tmp_path / "runner-failure.sqlite", runner=BrokenRunner())) as client:
        response = client.post("/api/runs", json={"task": "exercise runner failure"})

        assert response.status_code == 201
        run = response.json()
        assert run["status"] == "FAILED"
        lint = run["check_report"]["lint"]
        assert lint["stdout"] == "partial output"
        assert lint["stderr"] == "runner stopped"
        assert lint["exit_code"] == exit_code
        assert lint["duration_ms"] == 123
        assert lint["timed_out"] is timed_out
        assert client.get("/api/health").status_code == 200
        assert client.get(f"/api/runs/{run['run_id']}").json() == run


@pytest.mark.parametrize("recommendation", ["reject", "revise", "approve-with-error"])
def test_failed_review_never_allows_approval(tmp_path, recommendation):
    class Reviewer(DeterministicMockProvider):
        def review(self, plan, patch, checks):
            report = super().review(plan, patch, checks)
            if recommendation == "approve-with-error":
                report.findings = [ReviewFinding(severity="error", message="blocking issue")]
            else:
                report.recommendation = recommendation
            return report

    coordinator = RunCoordinator(tmp_path / "review.sqlite", provider=Reviewer(), runner=PassingRunner())
    result = coordinator.start(run_id="run", patch_id="patch")
    assert result["status"] == "FAILED"
    assert result["check_report"]["passed"]
    assert "review_report" in result
    with pytest.raises(InvalidRunTransition):
        coordinator.database.mark_awaiting_approval("run")


def test_coder_only_receives_text_and_patch_survives_reject(tmp_path):
    class Coder(DeterministicMockProvider):
        def code(self, plan, originals, **identity):
            assert isinstance(originals["existing.py"], str)
            with pytest.raises(TypeError):
                originals["existing.py"] = "changed"
            return FilePatchSet(**identity, files=[
                FilePatch(path="existing.py", original=originals["existing.py"], modified="x = 2\n"),
                FilePatch(path="deleted.txt", original=originals["deleted.txt"], modified=None),
                FilePatch(path="nested/new.txt", original=None, modified="new\n"),
            ])

    coordinator = RunCoordinator(tmp_path / "coder.sqlite", provider=Coder(), runner=PassingRunner())
    base = coordinator.workspace.revision_path(0)
    (base / "existing.py").write_text("x = 1\n")
    (base / "deleted.txt").write_text("delete me\n")
    before = tree(base)
    result = coordinator.start(run_id="run", patch_id="patch", patch_revision=3)
    assert result["status"] == "AWAITING_APPROVAL"
    assert result["check_report"]["patch_revision"] == 3
    assert tree(base) == before
    candidate = tree(coordinator.workspace.candidate_path("run"))
    assert candidate == {"existing.py": b"x = 2\n", "nested/new.txt": b"new\n"}
    recovered = FilePatchSet.model_validate_json(coordinator.database.get_patch("run", 3)["patch_json"])
    assert len(recovered.files) == 3
    coordinator.decide(run_id="run", patch_revision=3, decision_id="reject", kind=DecisionKind.REJECT)
    assert tree(base) == before


@pytest.mark.parametrize("kind", ["check_report", "review_report"])
@pytest.mark.parametrize("field,value", [("run_id", "other"), ("patch_revision", 2)])
def test_restart_does_not_approve_mismatched_report(tmp_path, monkeypatch, kind, field, value):
    coordinator = RunCoordinator(tmp_path / "mismatch.sqlite", runner=PassingRunner())
    def crash(_run_id):
        raise RuntimeError("process stopped before DB transition")
    monkeypatch.setattr(coordinator.database, "mark_awaiting_approval", crash)
    monkeypatch.setattr(coordinator.database, "fail_run_start", lambda _: False)
    with pytest.raises(RuntimeError):
        coordinator.start(run_id="run", patch_id="patch")
    report = coordinator.database.artifacts("run")[kind]
    report[field] = value
    with coordinator.database.transaction() as connection:
        connection.execute("UPDATE run_artifacts SET payload_json = ? WHERE run_id = ? AND kind = ?",
                           (json.dumps(report), "run", kind))
    restarted = RunCoordinator(coordinator.database.path)
    assert restarted.snapshot("run")["status"] == "FAILED"


def test_nonempty_patch_is_published_as_a_new_managed_revision(tmp_path):
    coordinator = RunCoordinator(tmp_path / "approve.sqlite", runner=PassingRunner())
    coordinator.start(run_id="run", patch_id="patch")
    result = coordinator.decide(run_id="run", patch_revision=1, decision_id="approve", kind=DecisionKind.APPROVE)
    assert result["status"] == "COMPLETE"
    assert result["workspace_revision"] == 1
    assert tree(coordinator.workspace.revision_path(0)) == {}
    published = coordinator.workspace.read_revision(1)
    assert set(published) == {"devflow_task.py", "test_devflow_task.py"}
    assert "Demonstrate the deterministic coding workflow" in published["devflow_task.py"]


@pytest.mark.parametrize("stage", ["code", "materialize", "review"])
def test_node_exception_is_persisted_and_releases_active_run(tmp_path, monkeypatch, stage):
    coordinator = RunCoordinator(tmp_path / "exception.sqlite", runner=PassingRunner())
    def fail(_state):
        raise ValueError("stage failed")
    original = getattr(coordinator.pipeline, stage)
    monkeypatch.setattr(coordinator.pipeline, stage, fail)
    with pytest.raises(ValueError, match="stage failed"):
        coordinator.start(run_id="failed", patch_id="patch")
    assert coordinator.snapshot("failed")["status"] == "FAILED"
    assert any(e["node"] == stage and e["type"] == "node.failed"
               for e in coordinator.database.list_events("failed"))
    with coordinator.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM run_checkpoint_refs").fetchone()[0] == 1
    assert tree(coordinator.workspace.revision_path(0)) == {}
    monkeypatch.setattr(coordinator.pipeline, stage, original)
    assert coordinator.start(run_id="next", patch_id="next-patch")["status"] == "AWAITING_APPROVAL"


def test_missing_evidence_cannot_enter_approval(tmp_path):
    coordinator = RunCoordinator(tmp_path / "missing.sqlite", runner=PassingRunner())
    coordinator.database.create_run(run_id="run", thread_id="run", patch_id="patch",
                                    patch_revision=1, candidate_dir="unused", patch={"files": []})
    with pytest.raises(InvalidRunTransition, match="lacks successful checks"):
        coordinator.database.mark_awaiting_approval("run")
