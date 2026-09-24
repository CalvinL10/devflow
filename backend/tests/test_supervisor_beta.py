from __future__ import annotations

from pathlib import Path

from devflow.coordinator import RunCoordinator
from devflow.models import RunStatus
from devflow.supervisor import RunSupervisor


class Runner:
    def __init__(self, stop_result=True):
        self.stop_result = stop_result
        self.calls = []

    def stop(self, run_id):
        self.calls.append(run_id)
        return self.stop_result


def make_run(tmp_path: Path, runner: Runner, *, stop_requested=False):
    coordinator = RunCoordinator(
        tmp_path / "data.sqlite",
        tmp_path / "workspace",
        runner=runner,
        recover=False,
    )
    coordinator.database.create_run(
        run_id="run-beta",
        thread_id="run-beta",
        patch_id="patch-beta",
        patch_revision=1,
        candidate_dir=str(coordinator.workspace.candidate_path("run-beta")),
        patch={"files": []},
        task="test recovery",
        context={"phase": "plan"},
    )
    if stop_requested:
        coordinator.database.request_stop("run-beta")
    return coordinator


def supervisor_for(coordinator, runner):
    supervisor = RunSupervisor(
        coordinator,
        Path("/tmp/devflow-settings"),
        "mock",
        runner=runner,
    )
    # Keep these tests about supervisor reconciliation, not graph checkpoint
    # recovery owned by RunCoordinator.
    coordinator._recover_incomplete_starts = lambda: None
    coordinator._recover_incomplete_decisions = lambda: None
    return supervisor


def test_restart_cleanup_success_marks_interrupted_run_failed_with_precise_error(tmp_path):
    runner = Runner()
    coordinator = make_run(tmp_path, runner)
    supervisor = supervisor_for(coordinator, runner)

    supervisor.recover()

    snapshot = coordinator.database.snapshot("run-beta")
    assert snapshot["status"] == RunStatus.FAILED.value
    assert snapshot["error"] == {
        "code": "worker_interrupted",
        "message": "Execution stopped before reaching approval. Create a new task to retry.",
        "phase": "plan",
    }
    assert not snapshot["cleanup_pending"]
    assert runner.calls == ["run-beta"]


def test_restart_cleanup_failure_keeps_run_active_until_cleanup_is_confirmed(tmp_path):
    failing = Runner(stop_result=False)
    coordinator = make_run(tmp_path, failing)
    supervisor = supervisor_for(coordinator, failing)

    supervisor.recover()
    pending = coordinator.database.snapshot("run-beta")
    assert pending["status"] == RunStatus.RUNNING.value
    assert pending["cleanup_pending"] is True
    assert pending["error"] is None

    succeeding = Runner()
    supervisor.runner = succeeding
    coordinator.pipeline.runner = succeeding
    supervisor.recover()
    recovered = coordinator.database.snapshot("run-beta")
    assert recovered["status"] == RunStatus.FAILED.value
    assert recovered["cleanup_pending"] is False
    assert recovered["error"]["code"] == "worker_interrupted"


def test_restart_stop_request_cancels_only_after_cleanup_and_clears_pending(tmp_path):
    runner = Runner()
    coordinator = make_run(tmp_path, runner, stop_requested=True)
    supervisor = supervisor_for(coordinator, runner)

    supervisor.recover()

    snapshot = coordinator.database.snapshot("run-beta")
    assert snapshot["status"] == RunStatus.CANCELED.value
    assert snapshot["cleanup_pending"] is False
    assert snapshot["error"] is None


def test_worker_interruption_does_not_replace_stage_error(tmp_path):
    runner = Runner()
    coordinator = make_run(tmp_path, runner)
    coordinator.database.execution_error(
        "run-beta",
        "stage_failed",
        "Stage failed; check supported project scope and stage reports.",
        finalize=False,
    )
    supervisor = supervisor_for(coordinator, runner)

    class DeadProcess:
        def is_alive(self):
            return False

        def join(self, *_args):
            return None

    supervisor._watch("run-beta", DeadProcess())

    snapshot = coordinator.database.snapshot("run-beta")
    assert snapshot["status"] == RunStatus.FAILED.value
    assert snapshot["error"]["code"] == "stage_failed"
    assert (
        snapshot["error"]["message"]
        != "Execution stopped before reaching approval. Create a new task to retry."
    )
    assert snapshot["cleanup_pending"] is False


def test_stop_cleanup_failure_is_retryable_and_does_not_report_canceled(tmp_path):
    runner = Runner(stop_result=False)
    coordinator = make_run(tmp_path, runner)
    supervisor = supervisor_for(coordinator, runner)

    snapshot = supervisor.stop("run-beta")

    assert snapshot["status"] == RunStatus.RUNNING.value
    assert snapshot["stop_requested"] is True
    assert snapshot["cleanup_pending"] is True
    assert snapshot["error"] is None
