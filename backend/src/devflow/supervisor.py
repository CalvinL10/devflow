"""Single-instance process supervision. SQLite remains the source of run state."""

from __future__ import annotations

import multiprocessing
import os
import threading
import uuid
from pathlib import Path
from time import monotonic

from devflow.coordinator import RunCoordinator
from devflow.errors import ActiveRunConflict, IdempotencyConflict, InvalidRunTransition
from devflow.mock_provider import DeterministicMockProvider
from devflow.models import RunStatus
from devflow.workspace import ManagedWorkspace

MODEL_STAGE_TIMEOUT_SECONDS = 130
PROCESS_JOIN_SECONDS = 3
MODEL_PHASES = frozenset({"plan", "code", "review"})


def _execute_child(database_path, workspace_root, settings_root, mode, run_id, provider, runner):
    # Production runs inside Linux Compose. Do not leave a model request alive if
    # its supervising backend dies; candidate containers are reconciled separately.
    if os.name == "posix":
        import ctypes

        parent = os.getppid()
        try:
            ctypes.CDLL(None).prctl(1, 9)
        except AttributeError:
            pass
        if os.getppid() != parent:
            return
    coordinator = None
    try:
        if provider is None:
            if mode == "mock":
                provider = DeterministicMockProvider()
            else:
                from devflow.provider import ProviderSettingsStore

                provider = ProviderSettingsStore(Path(settings_root)).provider()
        coordinator = RunCoordinator(
            database_path, workspace_root, provider=provider, runner=runner, recover=False
        )
        coordinator.execute_existing(run_id, defer_finish=True)
    except Exception:  # noqa: BLE001 - process/cleanup boundary never exposes worker data
        # Never serialize exceptions from model transport or candidate source.
        if coordinator:
            coordinator.database.execution_error(
                run_id,
                "execution_failed",
                "Execution failed; inspect stage reports and provider configuration.",
                finalize=False,
            )


class RunSupervisor:
    def __init__(self, coordinator, settings_root, mode, importer=None, provider=None, runner=None):
        self.coordinator = coordinator
        self.database = coordinator.database
        self.settings_root, self.mode, self.importer = settings_root, mode, importer
        self.provider, self.runner = provider, runner
        self.lock = threading.RLock()
        self.process = None
        self.run_id = None
        self.monitor = None
        self.closing = False

    def recover(self):
        interrupted = []
        for run in self.database.list_runs_with_status(RunStatus.RUNNING):
            interrupted.append(run["id"])
            context = self.database.context(run["id"])
            if context:
                try:
                    self._cleanup(run["id"])
                except Exception:  # noqa: BLE001 - process/cleanup boundary never exposes worker data
                    self._cleanup_pending(run["id"])
                    continue
                self.database.update_context(run["id"], cleanup_pending=False)
                if self.database.context(run["id"]).get("stop_requested"):
                    self.database.execution_error(run["id"], "stopped", "", canceled=True)
                elif context.get("model_stage_timeout"):
                    self._finish_failed(run["id"])
        self.coordinator._recover_incomplete_starts()
        self.coordinator._recover_incomplete_decisions()
        # A process can disappear between checkpoints without leaving a
        # resumable approval interrupt.  The coordinator's legacy recovery
        # path marks that run FAILED but does not own the supervisor diagnosis.
        # Add it only after recovery has had a chance to promote valid
        # checkpoints, and never replace a stage-specific error.
        for run_id in interrupted:
            run = self.database.get_run(run_id)
            context = self.database.context(run_id)
            if (
                run["status"] == RunStatus.RUNNING.value
                and not context.get("stop_requested")
                and not context.get("cleanup_pending")
            ):
                self.database.execution_error(
                    run_id,
                    "worker_interrupted",
                    "Execution stopped before reaching approval. Create a new task to retry.",
                )
            elif run["status"] == RunStatus.FAILED.value and not context.get("error"):
                self.database.execution_error(
                    run_id,
                    "worker_interrupted",
                    "Execution stopped before reaching approval. Create a new task to retry.",
                    finalize=False,
                )

    def submit(self, task, import_id, request_id):
        with self.lock:
            existing = self.database.find_request(request_id)
            if existing:
                context = self.database.context(existing)
                if (
                    context.get("import_id") != import_id
                    or self.database.artifacts(existing).get("task") != task
                ):
                    raise IdempotencyConflict("request_id was already used for a different task")
                return self.database.snapshot(existing)
            if self.database.active_run_snapshot() or (self.process and self.process.is_alive()):
                raise ActiveRunConflict("another run is active")
            if self.mode != "mock" and not import_id:
                raise ValueError("Import a clean project before starting a real task.")
            if self.mode != "mock" and self.provider is None:
                from devflow.provider import ProviderSettingsStore

                ProviderSettingsStore(Path(self.settings_root)).provider()
            metadata = {
                "provider": self.mode,
                "request_id": request_id,
                "import_id": import_id,
                "phase": "start",
            }
            run_id = "run-" + uuid.uuid4().hex
            workspace = self.coordinator.workspace
            workspace_id = "default"
            if import_id:
                if self.importer is None:
                    raise ValueError("No project is mounted.")
                imported = self.importer.load(import_id)
                metadata.update({key: imported.get(key) for key in ("dependency_source", "extras")})
                metadata["source_commit"] = imported["commit"]
                workspace_id = run_id
                workspace = ManagedWorkspace(
                    self.coordinator.workspace.root.parent / run_id, self.database
                )
                workspace.initialize()
                workspace._write_tree(workspace.revision_path(0), self.importer.files(import_id))
                self.database.ensure_workspace(workspace_id)
            self.database.create_run(
                run_id=run_id,
                thread_id=run_id,
                patch_id="patch-" + uuid.uuid4().hex,
                patch_revision=1,
                candidate_dir=str(workspace.candidate_path(run_id)),
                patch={"files": []},
                task=task,
                workspace_id=workspace_id,
                context=metadata,
            )
            self.run_id = run_id
            self.process = multiprocessing.get_context("spawn").Process(
                target=_execute_child,
                args=(
                    str(self.database.path),
                    str(self.coordinator.workspace.root),
                    str(self.settings_root),
                    self.mode,
                    run_id,
                    self.provider,
                    self.runner,
                ),
                daemon=True,
            )
            try:
                self.process.start()
            except Exception:  # noqa: BLE001 - process/cleanup boundary never exposes worker data
                self.database.execution_error(
                    run_id, "worker_start_failed", "Could not start the execution process."
                )
                self.process = None
                return self.database.snapshot(run_id)
            self.monitor = threading.Thread(
                target=self._watch, args=(run_id, self.process), daemon=True
            )
            self.monitor.start()
            return self.database.snapshot(run_id)

    def _cleanup(self, run_id):
        runner = self.coordinator.pipeline.runner
        if hasattr(runner, "stop") and runner.stop(run_id) is False:
            raise RuntimeError("container cleanup not confirmed")

    def _cleanup_pending(self, run_id):
        # A separate event exposes cleanup failure to live/reconnecting SSE
        # clients without replacing the original stage error. Repeated failed
        # attempts may emit another event; all remain durable and replayable.
        self.database.update_context(run_id, cleanup_pending=True)
        self.database.append_node_event(
            run_id, "cleanup", "run.cleanup_pending",
            self.database.current_patch(run_id)["patch_revision"],
        )

    @staticmethod
    def _terminate_child(process):
        if process.is_alive():
            process.terminate()
            process.join(PROCESS_JOIN_SECONDS)
        if process.is_alive():
            process.kill()
        process.join(PROCESS_JOIN_SECONDS)
        return not process.is_alive()

    def _finish_failed(self, run_id):
        # Keep both the snapshot and the terminal event faithful to the first
        # stage error. The DB also refuses FAILED when a stop has won the race.
        error = self.database.snapshot(run_id).get("error") or {
            "code": "worker_interrupted",
            "message": "Execution stopped before reaching approval. Create a new task to retry.",
        }
        self.database.execution_error(run_id, error["code"], error["message"])
        if self.database.context(run_id).get("stop_requested"):
            self.database.execution_error(run_id, "stopped", "", canceled=True)

    def _watch(self, run_id, process):
        phase = None
        deadline = None
        while process.is_alive():
            context = self.database.context(run_id)
            now = monotonic()
            if context.get("phase") != phase:
                phase = context.get("phase")
                deadline = now + MODEL_STAGE_TIMEOUT_SECONDS if phase in MODEL_PHASES else None
            stopping = context.get("stop_requested")
            expired = deadline is not None and now >= deadline
            if stopping or expired:
                if expired and not stopping:
                    # Persist before cleanup so a failed cleanup/restart cannot
                    # promote a late approval checkpoint after the hard timeout.
                    self.database.update_context(run_id, model_stage_timeout=phase)
                    self.database.execution_error(
                        run_id, "model_stage_timeout",
                        f"Model stage {phase} exceeded the {MODEL_STAGE_TIMEOUT_SECONDS}-second deadline.",
                        finalize=False,
                    )
                if not self._terminate_child(process):
                    self._cleanup_pending(run_id)
                    return
                break
            process.join(0.1)
        process.join(PROCESS_JOIN_SECONDS)
        try:
            self._cleanup(run_id)
        except Exception:  # noqa: BLE001 - process/cleanup boundary never exposes worker data
            # Do not free the active slot when executable containers may remain.
            self._cleanup_pending(run_id)
            return
        self.database.update_context(run_id, cleanup_pending=False)
        with self.lock:
            context = self.database.context(run_id)
            if context.get("stop_requested"):
                self.database.execution_error(run_id, "stopped", "", canceled=True)
            elif context.get("model_stage_timeout"):
                self._finish_failed(run_id)
            elif context.get("execution_outcome") == "approval":
                try:
                    self.database.mark_awaiting_approval(run_id)
                except InvalidRunTransition:
                    if self.database.context(run_id).get("stop_requested"):
                        self.database.execution_error(run_id, "stopped", "", canceled=True)
                    else:
                        raise
            elif self.database.get_run(run_id)["status"] == "RUNNING":
                self._finish_failed(run_id)

    def stop(self, run_id):
        with self.lock:
            self.database.request_stop(run_id)
            if run_id != self.run_id or not self.monitor or not self.monitor.is_alive():
                if (
                    run_id == self.run_id and self.process
                    and not self._terminate_child(self.process)
                ):
                    self._cleanup_pending(run_id)
                    return self.database.snapshot(run_id)
                try:
                    self._cleanup(run_id)
                except Exception:  # noqa: BLE001 - cleanup remains restart-retriable
                    self._cleanup_pending(run_id)
                    return self.database.snapshot(run_id)
                self.database.update_context(run_id, cleanup_pending=False)
                self.database.execution_error(run_id, "stopped", "", canceled=True)
            return self.database.snapshot(run_id)

    def require_idle(self):
        if self.database.active_run_snapshot() or (self.process and self.process.is_alive()):
            raise InvalidRunTransition("settings cannot be changed during an active task")

    def close(self):
        self.closing = True
        if self.process and self.process.is_alive():
            self.process.terminate()
        if self.process:
            self.process.join(3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()
        if self.monitor:
            self.monitor.join()
