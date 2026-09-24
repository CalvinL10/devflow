from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import devflow.supervisor as supervisor_module
from devflow.coordinator import RunCoordinator
from devflow.errors import ActiveRunConflict, InvalidRunTransition
from devflow.events import EventStream
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


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class FakeProcess:
    def __init__(self, clock, on_poll=None, *, ignore_kill=False):
        self.clock = clock
        self.on_poll = on_poll
        self.ignore_kill = ignore_kill
        self.alive = True
        self.calls = []
        self.polls = 0

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")
        if not self.ignore_kill:
            self.alive = False

    def join(self, timeout=None):
        assert timeout is not None, "monitor must never wait indefinitely"
        self.calls.append(("join", timeout))
        if timeout == 0.1:
            self.polls += 1
            self.clock.now += 65
            if self.on_poll:
                self.on_poll(self)


def pending_events(database):
    return [event for event in EventStream(database).read_page("run-beta", None).events
            if event["type"] == "run.cleanup_pending"]


def assert_slot_held(supervisor):
    assert supervisor.database.active_run_snapshot()["run_id"] == "run-beta"
    with pytest.raises(InvalidRunTransition):
        supervisor.require_idle()
    with pytest.raises(ActiveRunConflict):
        supervisor.submit("another task", None, "another-request")


@pytest.mark.parametrize("phase", ["plan", "code", "review"])
def test_model_deadline_reaps_child_then_cleans_before_failure(tmp_path, monkeypatch, phase):
    runner = Runner()
    coordinator = make_run(tmp_path, runner)
    db = coordinator.database
    db.update_context("run-beta", phase=phase)
    supervisor = supervisor_for(coordinator, runner)
    clock = Clock()
    monkeypatch.setattr(supervisor_module, "monotonic", clock)
    process = FakeProcess(clock)

    def cleanup(run_id):
        assert not process.is_alive()
        assert process.calls[-1] == ("join", 3)
        assert db.snapshot(run_id)["status"] == "RUNNING"
        assert db.snapshot(run_id)["error"]["code"] == "model_stage_timeout"
        process.calls.append("cleanup")
        return True

    runner.stop = cleanup
    supervisor._watch("run-beta", process)
    assert clock.now == 130
    assert process.calls[-6:] == [
        "terminate", ("join", 3), "kill", ("join", 3), ("join", 3), "cleanup",
    ]
    assert process.calls.index("terminate") < process.calls.index("kill")
    assert process.calls.index("kill") < process.calls.index("cleanup")
    snapshot = db.snapshot("run-beta")
    assert snapshot["status"] == "FAILED"
    assert snapshot["error"]["phase"] == phase
    assert snapshot["error"]["code"] == "model_stage_timeout"
    assert not snapshot["cleanup_pending"]


def test_model_phase_change_gets_new_deadline_and_nonmodel_has_no_deadline(tmp_path, monkeypatch):
    runner = Runner()
    coordinator = make_run(tmp_path, runner)
    db = coordinator.database
    supervisor = supervisor_for(coordinator, runner)
    clock = Clock()
    monkeypatch.setattr(supervisor_module, "monotonic", clock)

    def change_phase(process):
        if process.polls == 1:
            db.update_context("run-beta", phase="code")
        elif process.polls == 3:
            db.update_context("run-beta", phase="dependencies")
        elif process.polls == 6:
            db.update_context("run-beta", phase="lint_test")
        elif process.polls == 10:
            db.update_context("run-beta", phase="review")

    process = FakeProcess(clock, change_phase)
    supervisor._watch("run-beta", process)
    assert clock.now == 780
    assert db.snapshot("run-beta")["error"]["phase"] == "review"


def test_timeout_cleanup_pending_replays_and_holds_slot_until_recovery(tmp_path, monkeypatch):
    runner = Runner(False)
    coordinator = make_run(tmp_path, runner)
    supervisor = supervisor_for(coordinator, runner)
    clock = Clock()
    monkeypatch.setattr(supervisor_module, "monotonic", clock)
    supervisor._watch("run-beta", FakeProcess(clock))
    db = coordinator.database
    assert db.snapshot("run-beta")["status"] == "RUNNING"
    assert db.snapshot("run-beta")["error"]["code"] == "model_stage_timeout"
    assert pending_events(db)[0]["node"] == "cleanup"
    assert_slot_held(supervisor)
    # Even a late child checkpoint must not promote a timed-out run to approval.
    db.update_context("run-beta", execution_outcome="approval")
    runner.stop_result = True
    supervisor.recover()
    assert db.snapshot("run-beta")["status"] == "FAILED"
    assert db.snapshot("run-beta")["error"]["code"] == "model_stage_timeout"
    assert not db.snapshot("run-beta")["cleanup_pending"]


@pytest.mark.parametrize("stop_during", ["before", "terminate", "cleanup", "finalize"])
def test_stop_wins_deadline_races_only_after_cleanup(tmp_path, monkeypatch, stop_during):
    runner = Runner()
    coordinator = make_run(tmp_path, runner, stop_requested=stop_during == "before")
    db = coordinator.database
    supervisor = supervisor_for(coordinator, runner)
    clock = Clock()
    monkeypatch.setattr(supervisor_module, "monotonic", clock)
    process = FakeProcess(clock)
    if stop_during == "terminate":
        original = process.terminate

        def terminate():
            original()
            db.request_stop("run-beta")

        process.terminate = terminate
    if stop_during == "finalize":
        original_error = db.execution_error

        def execution_error(run_id, code, message, **kwargs):
            if kwargs.get("finalize", True) and not kwargs.get("canceled"):
                db.request_stop(run_id)
            return original_error(run_id, code, message, **kwargs)

        db.execution_error = execution_error

    def cleanup(run_id):
        assert not process.is_alive()
        assert db.snapshot(run_id)["status"] == "RUNNING"
        if stop_during == "cleanup":
            db.request_stop(run_id)
        return True

    runner.stop = cleanup
    supervisor._watch("run-beta", process)
    snapshot = db.snapshot("run-beta")
    assert snapshot["status"] == "CANCELED"
    assert snapshot["error"] is None
    assert not snapshot["cleanup_pending"]


def test_unreaped_child_keeps_slot_and_stop_retries_before_container_cleanup(tmp_path, monkeypatch):
    runner = Runner()
    coordinator = make_run(tmp_path, runner)
    supervisor = supervisor_for(coordinator, runner)
    clock = Clock()
    monkeypatch.setattr(supervisor_module, "monotonic", clock)
    process = FakeProcess(clock, ignore_kill=True)
    supervisor.run_id, supervisor.process = "run-beta", process
    supervisor._watch("run-beta", process)
    assert runner.calls == []
    assert pending_events(coordinator.database)
    assert_slot_held(supervisor)
    assert supervisor.stop("run-beta")["status"] == "RUNNING"
    assert runner.calls == []
    process.ignore_kill = False
    assert supervisor.stop("run-beta")["status"] == "CANCELED"
    assert runner.calls == ["run-beta"]


@pytest.mark.parametrize("entry", ["watch", "recover", "stop"])
def test_cleanup_event_preserves_original_error_and_active_slot(tmp_path, entry):
    runner = Runner(False)
    coordinator = make_run(tmp_path, runner)
    db = coordinator.database
    db.execution_error("run-beta", "stage_failed", "Safe original error.", finalize=False)
    supervisor = supervisor_for(coordinator, runner)
    if entry == "watch":
        process = FakeProcess(Clock())
        process.alive = False
        supervisor._watch("run-beta", process)
    elif entry == "recover":
        supervisor.recover()
    else:
        supervisor.stop("run-beta")
    assert db.snapshot("run-beta")["error"]["message"] == "Safe original error."
    assert db.snapshot("run-beta")["cleanup_pending"]
    assert pending_events(db)
    assert_slot_held(supervisor)


def test_cleanup_pending_is_in_sse_frames_and_stop_waits_for_cleanup(tmp_path, monkeypatch):
    runner = Runner(False)
    coordinator = make_run(tmp_path, runner)
    supervisor = supervisor_for(coordinator, runner)
    clock = Clock()
    monkeypatch.setattr(supervisor_module, "monotonic", clock)
    supervisor._watch("run-beta", FakeProcess(clock))
    db = coordinator.database
    stream = EventStream(db)
    page = stream.read_page("run-beta", None)

    async def read_frames():
        frames = stream.frames("run-beta", page)
        try:
            return [await anext(frames) for _ in range(len(page.events) + 1)]
        finally:
            await frames.aclose()

    frames = asyncio.run(read_frames())
    assert any('"type": "run.cleanup_pending"' in frame
               and "event: run.event" in frame for frame in frames)
    pending = supervisor.stop("run-beta")
    assert pending["status"] == "RUNNING"
    assert pending["error"]["code"] == "model_stage_timeout"
    assert_slot_held(supervisor)
    runner.stop_result = True
    canceled = supervisor.stop("run-beta")
    assert canceled["status"] == "CANCELED"
    assert canceled["error"] is None
    assert not canceled["cleanup_pending"]
