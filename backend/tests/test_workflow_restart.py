from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from support import PassingRunner

import devflow.coordinator as coordinator_module
from devflow.coordinator import RunCoordinator
from devflow.database import Database
from devflow.errors import InvalidRunTransition
from devflow.mock_provider import DeterministicMockProvider
from devflow.models import DecisionKind, WorkflowState
from devflow.pipeline import RunPipeline
from devflow.workflow import build_graph, invoke_start, open_graph
from devflow.workspace import ManagedWorkspace

pytestmark = pytest.mark.usefixtures("mock_runner", "noop_coder")


def test_startup_recovery_renews_lease_during_slow_publication(tmp_path, monkeypatch):
    path = tmp_path / "slow-recovery.sqlite"
    coordinator = RunCoordinator(path, runner=PassingRunner())
    coordinator.start(run_id="run-slow", patch_id="patch-slow")
    coordinator.database.record_decision(
        run_id="run-slow", patch_revision=1, decision_id="slow-d",
        kind=DecisionKind.APPROVE, feedback=None,
    )
    monkeypatch.setattr(RunCoordinator, "DECISION_LEASE_SECONDS", 1)
    # A failed first attempt must not be hidden by an asynchronous retry.
    monkeypatch.setattr(RunCoordinator, "_schedule_decision_recovery", lambda *args: None)
    write_tree = ManagedWorkspace._write_tree

    def slow_write(workspace, *args):
        time.sleep(1.5)
        return write_tree(workspace, *args)

    monkeypatch.setattr(ManagedWorkspace, "_write_tree", slow_write)
    recovered = RunCoordinator(path, runner=PassingRunner())

    assert recovered.snapshot("run-slow")["status"] == "COMPLETE"
    assert recovered.snapshot("run-slow")["pending_decision"] is None
    assert recovered.snapshot("run-slow")["workspace_revision"] == 1
    events = [row["type"] for row in recovered.database.list_events("run-slow")]
    assert events.count("workspace.published") == events.count("run.completed") == 1


@pytest.mark.parametrize("recovery", ["restart", "retry"])
@pytest.mark.parametrize("damage", ["missing", "not-directory"])
def test_recorded_approval_requires_candidate_until_publication(tmp_path, monkeypatch, recovery, damage):
    path = tmp_path / "missing-candidate.sqlite"
    coordinator = RunCoordinator(path, runner=PassingRunner())
    coordinator.start(run_id="run-missing", patch_id="patch-missing")
    request = {"run_id": "run-missing", "patch_revision": 1,
               "decision_id": "missing-d", "kind": DecisionKind.APPROVE, "feedback": None}
    coordinator.database.record_decision(**request)
    candidate = coordinator.workspace.candidate_path("run-missing")
    candidate.rename(candidate.with_name("saved-candidate"))
    if damage == "not-directory":
        candidate.write_text("not a directory", encoding="utf-8")
    scheduled = []
    monkeypatch.setattr(RunCoordinator, "_schedule_decision_recovery", lambda *args: scheduled.append(args))

    if recovery == "restart":
        coordinator = RunCoordinator(path, runner=PassingRunner())
    else:
        with pytest.raises((FileNotFoundError, ValueError)):
            coordinator.decide(**request)

    assert coordinator.snapshot("run-missing")["status"] == "FAILED"
    assert coordinator.snapshot("run-missing")["pending_decision"] is None
    assert coordinator.snapshot("run-missing")["workspace_revision"] == 0
    assert coordinator.database.publication_for_decision("missing-d") is None
    assert scheduled == []
    assert coordinator.start(run_id="run-next", patch_id="patch-next")["status"] == "AWAITING_APPROVAL"


def run_cli(*arguments: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment["LANGGRAPH_STRICT_MSGPACK"] = "true"
    completed = subprocess.run(
        [sys.executable, "-c", (
            f"import sys; sys.path.insert(0, {str(Path(__file__).parent)!r}); "
            "from support import PassingRunner, noop_code; "
            "from devflow.mock_provider import DeterministicMockProvider; "
            "DeterministicMockProvider.code = noop_code; "
            "from devflow.docker_runner import DockerCandidateRunner; "
            "DockerCandidateRunner.run = PassingRunner.run; "
            "from devflow.workflow_cli import main; main()"
        ), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [("approve", "COMPLETE"), ("reject", "REJECTED")],
)
def test_interrupt_resumes_in_a_new_process(tmp_path, kind: str, expected_status: str) -> None:
    database = tmp_path / f"{kind}.sqlite"
    started = run_cli(
        "start",
        "--database",
        str(database),
        "--run-id",
        f"run-{kind}",
        "--patch-id",
        f"patch-{kind}",
    )
    assert started["status"] == "AWAITING_APPROVAL"

    resumed = run_cli(
        "decide",
        "--database",
        str(database),
        "--run-id",
        f"run-{kind}",
        "--patch-revision",
        "1",
        "--decision-id",
        f"decision-{kind}",
        "--kind",
        kind,
    )
    assert resumed["status"] == expected_status
    assert resumed["event_seqs"] == list(range(1, len(resumed["event_seqs"]) + 1))

    with sqlite3.connect(database) as connection:
        checkpoint_count = connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        checkpoint_ref = connection.execute(
            "SELECT thread_id, checkpoint_id FROM run_checkpoint_refs"
        ).fetchone()
        decision_count = connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert checkpoint_count > 0
    assert checkpoint_ref[0] == f"run-{kind}"
    assert checkpoint_ref[1]
    assert decision_count == 1

    repeated = run_cli(
        "decide",
        "--database",
        str(database),
        "--run-id",
        f"run-{kind}",
        "--patch-revision",
        "1",
        "--decision-id",
        f"decision-{kind}",
        "--kind",
        kind,
    )
    assert repeated == resumed


def test_same_decision_concurrent_requests_resume_once(tmp_path, monkeypatch) -> None:
    coordinator = RunCoordinator(tmp_path / "concurrent.sqlite")
    coordinator.start(
        run_id="run-concurrent",
        patch_id="patch-concurrent",
    )
    original_invoke_resume = coordinator_module.invoke_resume
    release_resume = threading.Event()
    resume_call_count = 0
    count_lock = threading.Lock()

    def delayed_resume(*args, **kwargs):
        nonlocal resume_call_count
        with count_lock:
            resume_call_count += 1
        assert release_resume.wait(timeout=5)
        return original_invoke_resume(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "invoke_resume", delayed_resume)

    request = {
        "run_id": "run-concurrent",
        "patch_revision": 1,
        "decision_id": "decision-concurrent",
        "kind": DecisionKind.APPROVE,
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(coordinator.decide, **request)
        second = executor.submit(coordinator.decide, **request)
        time.sleep(0.2)
        release_resume.set()
        snapshots = [first.result(timeout=10), second.result(timeout=10)]

    events = coordinator.database.list_events("run-concurrent")
    assert resume_call_count == 1
    assert [snapshot["status"] for snapshot in snapshots] == ["COMPLETE", "COMPLETE"]
    assert [event["type"] for event in events].count("run.completed") == 1


def test_renewal_failure_aborts_approval_at_apply_boundary(tmp_path, monkeypatch) -> None:
    coordinator = RunCoordinator(tmp_path / "renewal-failure.sqlite")
    coordinator.DECISION_LEASE_SECONDS = 3
    coordinator.start(run_id="run-renewal", patch_id="patch-renewal")
    renewal_failed = threading.Event()
    original_invoke_resume = coordinator_module.invoke_resume

    def fail_renewal(**_kwargs):
        renewal_failed.set()
        return False

    def resume_after_renewal_failure(*args, **kwargs):
        assert renewal_failed.wait(timeout=2)
        return original_invoke_resume(*args, **kwargs)

    monkeypatch.setattr(coordinator.database, "renew_decision_resume_claim", fail_renewal)
    monkeypatch.setattr(coordinator_module, "invoke_resume", resume_after_renewal_failure)

    with pytest.raises(InvalidRunTransition, match="lease renewal failed"):
        coordinator.decide(
            run_id="run-renewal",
            patch_revision=1,
            decision_id="decision-renewal",
            kind=DecisionKind.APPROVE,
        )

    assert coordinator.snapshot("run-renewal")["status"] == "FAILED"
    assert coordinator.database.list_unfinished_decisions("run-renewal") == []


def test_missing_candidate_reject_still_completes_decision(tmp_path) -> None:
    coordinator = RunCoordinator(tmp_path / "reject-missing-candidate.sqlite")
    coordinator.start(run_id="run-reject", patch_id="patch-reject")
    shutil.rmtree(coordinator.workspace.candidate_path("run-reject"))

    snapshot = coordinator.decide(
        run_id="run-reject",
        patch_revision=1,
        decision_id="decision-reject",
        kind=DecisionKind.REJECT,
    )

    assert snapshot["status"] == "REJECTED"
    assert coordinator.database.list_unfinished_decisions("run-reject") == []


def test_missing_candidate_approve_is_rejected_before_recording_decision(tmp_path) -> None:
    coordinator = RunCoordinator(tmp_path / "claim-release.sqlite")
    coordinator.start(run_id="run-claim", patch_id="patch-claim")
    shutil.rmtree(coordinator.workspace.candidate_path("run-claim"))

    with pytest.raises(FileNotFoundError):
        coordinator.decide(
            run_id="run-claim",
            patch_revision=1,
            decision_id="decision-claim",
            kind=DecisionKind.APPROVE,
        )

    assert coordinator.snapshot("run-claim")["status"] == "AWAITING_APPROVAL"
    assert coordinator.database.list_unfinished_decisions("run-claim") == []
    with coordinator.database.connect() as connection:
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM decision_resume_claims WHERE decision_id = ?",
            ("decision-claim",),
        ).fetchone()[0]
    assert claim_count == 0


def test_candidate_loss_after_approve_record_fails_decision(tmp_path, monkeypatch) -> None:
    coordinator = RunCoordinator(tmp_path / "candidate-race.sqlite")
    coordinator.start(run_id="run-race", patch_id="patch-race")
    candidate = coordinator.workspace.candidate_path("run-race")
    original_validate = coordinator.workspace.require_materialized_candidate
    validation_count = 0

    def remove_candidate_after_preflight(run_id, candidate_dir):
        nonlocal validation_count
        validation_count += 1
        if validation_count == 2:
            shutil.rmtree(candidate)
        return original_validate(run_id, candidate_dir)

    monkeypatch.setattr(
        coordinator.workspace,
        "require_materialized_candidate",
        remove_candidate_after_preflight,
    )

    with pytest.raises(FileNotFoundError):
        coordinator.decide(
            run_id="run-race",
            patch_revision=1,
            decision_id="decision-race",
            kind=DecisionKind.APPROVE,
        )

    assert coordinator.snapshot("run-race")["status"] == "FAILED"
    assert coordinator.database.list_unfinished_decisions("run-race") == []
    assert coordinator.database.list_events("run-race")[-1]["type"] == "run.failed"


def test_start_failure_is_terminal_and_does_not_block_the_next_run(
    tmp_path, monkeypatch
) -> None:
    coordinator = RunCoordinator(tmp_path / "start-failure.sqlite")
    original_invoke_start = coordinator_module.invoke_start

    def fail_start(*_args, **_kwargs):
        raise RuntimeError("simulated start failure")

    monkeypatch.setattr(coordinator_module, "invoke_start", fail_start)
    with pytest.raises(RuntimeError, match="simulated start failure"):
        coordinator.start(
            run_id="run-failed",
            patch_id="patch-failed",
        )

    assert coordinator.database.get_run("run-failed")["status"] == "FAILED"
    assert [
        event["type"] for event in coordinator.database.list_events("run-failed")
    ] == ["run.created", "run.failed"]

    monkeypatch.setattr(coordinator_module, "invoke_start", original_invoke_start)
    result = coordinator.start(
        run_id="run-next",
        patch_id="patch-next",
    )
    assert result["status"] == "AWAITING_APPROVAL"


def test_new_coordinator_fails_stranded_start_without_checkpoint(tmp_path) -> None:
    database_path = tmp_path / "stranded.sqlite"
    database = Database(database_path)
    database.initialize()
    database.create_run(
        run_id="run-stranded",
        thread_id="run-stranded",
        patch_id="patch-stranded",
        patch_revision=1,
        candidate_dir=str(tmp_path / "candidate-stranded"),
        patch={"files": []},
    )

    RunCoordinator(database_path)

    assert database.get_run("run-stranded")["status"] == "FAILED"
    assert database.list_events("run-stranded")[-1]["type"] == "run.failed"


def test_new_coordinator_recovers_start_at_durable_approval_interrupt(tmp_path) -> None:
    database_path = tmp_path / "recoverable.sqlite"
    database = Database(database_path)
    database.initialize()
    workspace = ManagedWorkspace(tmp_path / "workspaces" / "default", database)
    workspace.initialize()
    candidate = workspace.candidate_path("run-recoverable")
    run = database.create_run(
        run_id="run-recoverable",
        thread_id="run-recoverable",
        patch_id="patch-recoverable",
        patch_revision=1,
        candidate_dir=str(candidate),
        patch={"files": []},
    )
    state = WorkflowState(
        run_id="run-recoverable",
        thread_id="run-recoverable",
        patch_id="patch-recoverable",
        patch_revision=1,
        base_workspace_revision=int(run["base_workspace_revision"]),
    )
    with open_graph(database.path) as checkpointer:
        graph = build_graph(checkpointer, RunPipeline(
            database, workspace, DeterministicMockProvider(), PassingRunner()
        ))
        result = invoke_start(graph, state, "run-recoverable")
    assert "__interrupt__" in result

    recovered = RunCoordinator(database_path)

    assert recovered.snapshot("run-recoverable")["status"] == "AWAITING_APPROVAL"
    with database.connect() as connection:
        checkpoint_ref = connection.execute(
            "SELECT checkpoint_id FROM run_checkpoint_refs WHERE run_id = ?",
            ("run-recoverable",),
        ).fetchone()
    assert checkpoint_ref is not None
    assert checkpoint_ref["checkpoint_id"]


def test_new_coordinator_recovers_incomplete_applying_decision_without_terminal_checkpoint(tmp_path) -> None:
    database_path = tmp_path / "stranded-approval.sqlite"
    coordinator = RunCoordinator(database_path)
    coordinator.start(run_id="run-applying", patch_id="patch-applying")
    coordinator.database.record_decision(
        decision_id="decision-applying",
        run_id="run-applying",
        patch_revision=1,
        kind=DecisionKind.APPROVE,
        feedback=None,
    )

    recovered = RunCoordinator(database_path)

    assert recovered.snapshot("run-applying")["status"] == "COMPLETE"
    assert recovered.database.list_events("run-applying")[-1]["type"] == "run.completed"
    next_run = recovered.start(run_id="run-next", patch_id="patch-next")
    assert next_run["status"] == "AWAITING_APPROVAL"


def test_new_coordinator_recovers_reject_recorded_before_resume(tmp_path) -> None:
    database_path = tmp_path / "reject-window.sqlite"
    coordinator = RunCoordinator(database_path)
    coordinator.start(run_id="run-reject-window", patch_id="patch-reject-window")
    coordinator.database.record_decision(
        decision_id="decision-reject-window",
        run_id="run-reject-window",
        patch_revision=1,
        kind=DecisionKind.REJECT,
        feedback="no",
    )

    recovered = RunCoordinator(database_path)

    assert recovered.snapshot("run-reject-window")["status"] == "REJECTED"
    assert recovered.database.list_unfinished_decisions("run-reject-window") == []
    assert recovered.database.list_events("run-reject-window")[-1]["type"] == "run.rejected"


def test_recovery_does_not_fail_live_decision_lease_after_terminal_checkpoint(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "live-lease.sqlite"
    coordinator = RunCoordinator(database_path)
    coordinator.start(run_id="run-live-lease", patch_id="patch-live-lease")
    original_finish = coordinator.database.finish_decision
    monkeypatch.setattr(coordinator.database, "finish_decision", lambda *args, **kwargs: False)
    coordinator.decide(
        run_id="run-live-lease",
        patch_revision=1,
        decision_id="decision-live-lease",
        kind=DecisionKind.APPROVE,
    )
    monkeypatch.setattr(coordinator.database, "finish_decision", original_finish)

    recovered = RunCoordinator(database_path)

    assert recovered.snapshot("run-live-lease")["status"] == "APPLYING"
    assert recovered.database.list_unfinished_decisions("run-live-lease")


def test_recovery_retries_after_a_live_lease_expires_without_restart(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "delayed-recovery.sqlite"
    coordinator = RunCoordinator(database_path, runner=PassingRunner())
    coordinator.start(run_id="run-delayed-recovery", patch_id="patch-delayed-recovery")
    coordinator.database.record_decision(
        run_id="run-delayed-recovery",
        patch_revision=1,
        decision_id="decision-delayed-recovery",
        kind=DecisionKind.APPROVE,
        feedback=None,
    )
    assert coordinator.database.claim_decision_resume(
        decision_id="decision-delayed-recovery", owner_id="abandoned", lease_seconds=1
    ) == "acquired"

    monkeypatch.setattr(RunCoordinator, "DECISION_LEASE_SECONDS", 1)
    recovered = RunCoordinator(database_path, runner=PassingRunner())
    assert recovered.snapshot("run-delayed-recovery")["status"] == "APPLYING"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if recovered.snapshot("run-delayed-recovery")["status"] == "COMPLETE":
            break
        time.sleep(0.05)
    assert recovered.snapshot("run-delayed-recovery")["status"] == "COMPLETE"
    assert recovered.database.list_unfinished_decisions("run-delayed-recovery") == []
    assert [event["type"] for event in recovered.database.list_events("run-delayed-recovery")].count(
        "run.completed"
    ) == 1


def test_recovery_retries_after_a_transient_failure_without_restart(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "retry-recovery.sqlite"
    coordinator = RunCoordinator(database_path, runner=PassingRunner())
    coordinator.start(run_id="run-retry-recovery", patch_id="patch-retry-recovery")
    coordinator.database.record_decision(
        run_id="run-retry-recovery",
        patch_revision=1,
        decision_id="decision-retry-recovery",
        kind=DecisionKind.APPROVE,
        feedback=None,
    )
    assert coordinator.database.claim_decision_resume(
        decision_id="decision-retry-recovery", owner_id="abandoned", lease_seconds=1
    ) == "acquired"

    attempts = 0
    original_invoke_resume = coordinator_module.invoke_resume

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient recovery failure")
        return original_invoke_resume(*args, **kwargs)

    monkeypatch.setattr(RunCoordinator, "DECISION_LEASE_SECONDS", 1)
    monkeypatch.setattr(coordinator_module, "invoke_resume", fail_once)
    recovered = RunCoordinator(database_path, runner=PassingRunner())

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if recovered.snapshot("run-retry-recovery")["status"] == "COMPLETE":
            break
        time.sleep(0.05)

    assert recovered.snapshot("run-retry-recovery")["status"] == "COMPLETE"
    assert attempts >= 2
    assert recovered.database.list_unfinished_decisions("run-retry-recovery") == []
